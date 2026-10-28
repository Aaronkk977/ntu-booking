"""Look back at past schedules and count, per weekday x hour, how each court-slot was used.

Past cells are labelled by the site: "常租" (booked online), "已過期" (was open, nobody booked),
"未開放租借", holidays, classes, teams, events. Only aggregate counts are kept; labels that are
people's names fall into "其他" and are not stored.

Usage: python history.py [venue_id] [since YYYY-MM-DD]
       python history.py A:2 2026-09-07
"""
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import scanner
import session

STATS_FILE = Path(__file__).with_name("history_stats.json")
CATEGORIES = ["常租", "已過期", "未開放", "假日", "其他"]
HOLIDAY_WORDS = ("節", "國慶", "補假", "紀念日", "春節", "除夕", "連假", "放假")

_EVENT_RE = re.compile(r'height: calc\( var\( --ScheduleHeight \) \* (\d+) - 2px \);"><div class="EventName"[^>]*title="([^"]*)"')
_CELL_RE = re.compile(r'<(?:a|div)\b[^>]*\bclass="S"[^>]*>\s*<div class="SText">\s*(\d{1,2})\s*~(.*?)(?=<(?:a|div)\b[^>]*\bclass="S"|\Z)', re.S)


def categorize(label: str) -> str:
    if label == "常租":
        return "常租"
    if label == "已過期":
        return "已過期"
    if label == "未開放租借":
        return "未開放"
    if any(w in label for w in HOLIDAY_WORDS):
        return "假日"
    return "其他"


def label_cells(schedule_html: str) -> dict[tuple[str, int], str]:
    """{(iso_date, hour): category}. A multi-hour event sits in its first cell and covers the next ones."""
    out = {}
    parts = scanner._DAY_RE.split(schedule_html)
    for day, body in zip(parts[1::2], parts[2::2]):
        covered_until, current = -1, None
        for hour_s, inner in _CELL_RE.findall(body):
            hour = int(hour_s)
            m = _EVENT_RE.search(inner)
            if m:
                current, covered_until = categorize(m.group(2)), hour + int(m.group(1))
            if hour < covered_until and current:
                out[(day, hour)] = current
    return out


def collect(venue: str, since: date, until: date, progress=None) -> dict:
    with session.SITE_LOCK:
        return _collect(venue, since, until, progress)


def _collect(venue: str, since: date, until: date, progress=None) -> dict:
    session.fetch(f"/order/?Add={venue}")
    time.sleep(1)
    courts, sk = scanner.parse_order_page(session.fetch("/order/A/"))
    if not courts:
        raise ValueError("找不到場地面號")

    # counts[weekday][hour][category] = number of court-slots
    counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for i, (court_id, court_name) in enumerate(courts):
        if i:
            time.sleep(1.2)
        form = urlencode({
            "VenuesSN": court_id, "OrderSource": "Order", "BookingType": "", "Booking": "Y",
            "OrderSN": "", "OrderType": "A", "SDMK": scanner._epoch(since), "EDMK": scanner._epoch(until),
            "ViewStatus": "undefined",
        }) + "&EnV=" + sk
        cells = label_cells(json.loads(session.fetch("/__/f/Schedule.php", form)).get("ScheduleList", ""))
        for (day, hour), cat in cells.items():
            d = date.fromisoformat(day)
            if since <= d < until:
                counts[d.weekday()][hour][cat] += 1
        if progress:
            progress(i + 1, len(courts))

    return {
        "venue": venue, "since": since.isoformat(), "until": until.isoformat(),
        "courts": len(courts), "generated": datetime.now().isoformat(timespec="seconds"),
        "counts": {wd: {h: dict(c) for h, c in hours.items()} for wd, hours in counts.items()},
    }


def print_table(stats: dict) -> None:
    days = "一二三四五六日"
    print(f"\n{stats['venue']}　{stats['since']} ~ {stats['until']}　{stats['courts']} 面")
    print("每格：常租(被訂走) / 已過期(有開放但沒人訂) 的「面次」\n")
    print("時段   " + "".join(f"  週{d}    " for d in days))
    for h in range(6, 22):
        row = []
        for wd in range(7):
            c = stats["counts"].get(wd, {}).get(h, {})
            row.append(f"{c.get('常租', 0):>3}/{c.get('已過期', 0):<3}  " if c else "   -     ")
        print(f"{h:02d}:00  " + "".join(row))


def load_all() -> dict:
    """{venue_id: stats}. Older files held a single venue's stats at the top level."""
    if not STATS_FILE.exists():
        return {}
    data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    return {data["venue"]: data} if "venue" in data else data


def update(venue: str, since: date, progress=None) -> dict:
    stats = collect(venue, since, date.today(), progress)
    all_stats = load_all()
    all_stats[venue] = stats
    STATS_FILE.write_text(json.dumps(all_stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


if __name__ == "__main__":
    venue = sys.argv[1] if len(sys.argv) > 1 else "A:2"
    since = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() - timedelta(weeks=8)
    print_table(update(venue, since, lambda i, n: print(f"  {i}/{n} 面", flush=True)))
