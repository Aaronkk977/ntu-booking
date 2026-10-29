"""Data layer shared by 可租時段搜尋 and the robot.

Walks every court of a venue on rent.pe.ntu.edu.tw (today .. BOOK_WINDOW[1] days ahead), parses each
hour as open / onsite (walk-in only) / full, caches the walk per venue, and turns it into per-hour counts.
"""
import json
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import session

BOOK_WINDOW = (3, 7)  # bookable days ahead, inclusive

# Every start hour any venue's timetable has. Each venue's own rows come from the site (06-21 for
# 多功能球場, 08-21 for others), and per-day closures from its 「未開放租借」 labels, so nothing here is
# hardcoded per weekday.
HOURS = list(range(6, 22))


# Schedule markup (from /order/A/ and /__/f/Schedule.php):
#   <div class="D" d="2026-10-28" v="...">            one day column
#     <a class="S" v="12" s="Y" r="" ...>             bookable hour (12:00-13:00)
#     <div class="S" s="N"><div class="SText">13 ~ 14  not bookable (event, closed, already rented)
#     <div class="N ..." style="height: calc( ... * 3 - 2px );"><div class="EventName" title="請至現場租借">
#                                                      an event label; it covers this and the next 2 hours
DAY_RE = re.compile(r'<div class="D" d="(\d{4}-\d{2}-\d{2})"')
SLOT_RE = re.compile(
    r'<(a|div)\b([^>]*\bclass="S"[^>]*)>\s*<div class="SText">\s*(\d{1,2})\s*~(.*?)(?=<(?:a|div)\b[^>]*\bclass="S"|\Z)', re.S
)
EVENT_RE = re.compile(r'\* (\d+) - 2px \);"><div class="EventName"[^>]*title="([^"]*)"')
ONSITE_LABEL = "現場租借"  # "請至現場租借": free, but only bookable at the counter
CLOSED_LABEL = "未開放"  # "未開放租借": the venue does not rent this hour at all
_COURT_RE = re.compile(r'<a [^>]*\bv="(\d+)"[^>]*class="VSMenu[^"]*"[^>]*title="([^"]*)"')
_SK_RE = re.compile(r'<textarea name="SK"[^>]*>([^<]*)</textarea>')


def parse_schedule(html: str) -> dict[tuple[str, int], str]:
    """Return {(iso_date, start_hour): "open" | "onsite" | "closed" | "full"} for every slot in a schedule fragment."""
    out = {}
    parts = DAY_RE.split(html)
    for day, body in zip(parts[1::2], parts[2::2]):
        covered_until, label = -1, ""
        for tag, attrs, hour_s, inner in SLOT_RE.findall(body):
            hour = int(hour_s)
            event = EVENT_RE.search(inner)
            if event:
                covered_until, label = hour + int(event.group(1)), event.group(2)
            covered = hour < covered_until
            if tag == "a" and 's="Y"' in attrs and 'r="Y"' not in attrs:
                out[(day, hour)] = "open"
            elif covered and ONSITE_LABEL in label:
                out[(day, hour)] = "onsite"
            elif covered and CLOSED_LABEL in label:
                out[(day, hour)] = "closed"
            else:
                out[(day, hour)] = "full"
    return out


def parse_order_page(html: str) -> tuple[list[tuple[str, str]], str]:
    """Return ([(court_id, court_name), ...], SK token) from /order/A/."""
    courts = _COURT_RE.findall(html)
    sk = _SK_RE.search(html)
    return courts, (sk.group(1) if sk else "")


def _epoch(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time()).timestamp())


def open_venue(venue: str) -> tuple[list[tuple[str, str]], str, str]:
    """Select `venue` in the server-side session. Returns (courts, SK token, order page HTML); the order page
    already shows the first court's schedule. Callers must hold session.SITE_LOCK."""
    # ?Add= selects the venue in the server-side session, then /order/A/ shows its first court.
    session.fetch(f"/order/?Add={venue}")
    session.pause(1)
    page = session.fetch("/order/A/")
    session.SNAPSHOT_DIR.mkdir(exist_ok=True)
    (session.SNAPSHOT_DIR / f"{venue.replace(':', '_')}_order.html").write_text(page, encoding="utf-8")
    courts, sk = parse_order_page(page)
    if not courts:
        raise ValueError("訂場頁面找不到場地面號，網站格式可能改了（頁面已存到 snapshots/）")
    return courts, sk, page


def fetch_schedule(court_id: str, sk: str, start: date, end: date) -> str:
    """Schedule markup of one court of the venue opened by open_venue(), from `start` to `end`."""
    form = urlencode({
        "VenuesSN": court_id, "OrderSource": "Order", "BookingType": "", "Booking": "Y",
        "OrderSN": "", "OrderType": "A", "SDMK": _epoch(start), "EDMK": _epoch(end),
        "ViewStatus": "undefined",
    }) + "&EnV=" + sk  # SK is already URL-encoded in the page, send it as-is like the site's JS
    return json.loads(session.fetch("/__/f/Schedule.php", form)).get("ScheduleList", "")


# Cache of the last full walk per venue (all courts, today .. BOOK_WINDOW[1]), shared by search and
# the robot, persisted so a restart doesn't force a re-fetch. Court names stay in here for a future
# order step; the UI only ever receives counts.
CACHE_FILE = Path(__file__).with_name("search_cache.json")
CACHE_MAX_AGE_S = 10 * 60
_cache_lock = threading.Lock()


def cache_put(venue: str, courts: list[tuple[str, dict]]) -> None:
    with _cache_lock:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        data[venue] = {
            "fetched": datetime.now().isoformat(timespec="seconds"),
            "courts": [[name, {f"{d}|{h}": st for (d, h), st in slots.items()}] for name, slots in courts],
        }
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def cache_get(venue: str, max_age_s: float) -> tuple[list[tuple[str, dict]], datetime] | None:
    with _cache_lock:
        if not CACHE_FILE.exists():
            return None
        entry = json.loads(CACHE_FILE.read_text(encoding="utf-8")).get(venue)
    if not entry:
        return None
    fetched = datetime.fromisoformat(entry["fetched"])
    # A walk from an earlier day covers a different booking window, so it is never reused.
    if fetched.date() != date.today() or (datetime.now() - fetched).total_seconds() > max_age_s:
        return None
    courts = [(name, {(k.split("|")[0], int(k.split("|")[1])): st for k, st in slots.items()}) for name, slots in entry["courts"]]
    return courts, fetched


def search(venue: str, progress=None, max_age_s: float = CACHE_MAX_AGE_S) -> dict:
    """Every hour from today to BOOK_WINDOW[1] days ahead: how many courts are open online / walk-in only /
    not rented at all. `hours` are the rows of this venue's timetable on the site."""
    cached = cache_get(venue, max_age_s) if max_age_s > 0 else None
    if not cached:
        with session.SITE_LOCK:
            # Another walk (the robot, or a search) may have fetched this venue while we waited for the site.
            cached = cache_get(venue, max_age_s) if max_age_s > 0 else None
            if not cached:
                courts, fetched = scan_venue(venue, progress), datetime.now()
                cache_put(venue, courts)
                _partial.pop(venue, None)  # only after the cache has it, so the UI never sees neither
    if cached:
        courts, fetched = cached
    return summarize(venue, courts, fetched, from_cache=bool(cached))


def summarize(venue: str, courts: list[tuple[str, dict]], fetched: datetime, from_cache: bool = False) -> dict:
    """Per-hour counts over `courts` (a whole walk, or the courts read so far)."""
    today = date.today()
    hours = sorted({h for _, slots in courts for _, h in slots})
    cells = {}
    for n in range(BOOK_WINDOW[1] + 1):
        d = today + timedelta(days=n)
        for h in hours:
            key = (d.isoformat(), h)
            cells[f"{key[0]}|{h}"] = {
                st: sum(slots.get(key) == st for _, slots in courts) for st in ("open", "onsite", "closed")
            }
    return {
        "venue": venue,
        "generated": fetched.isoformat(timespec="seconds"),
        "from_cache": from_cache,
        "dates": [(today + timedelta(days=n)).isoformat() for n in range(BOOK_WINDOW[1] + 1)],
        "online_from": (today + timedelta(days=BOOK_WINDOW[0])).isoformat(),
        "courts": len(courts),
        "hours": hours,
        "cells": cells,
    }


# The courts read so far by the walk in progress, per venue, so the UI can show them before the walk ends.
# Kept after a walk stops early (booking, logout, error) until that venue's next walk; dropped once a
# walk is in the cache.
_partial: dict[str, dict] = {}


def partial_results() -> dict[str, dict]:
    """summarize() of each venue's unfinished walk from today, plus "partial": {done, total, running}."""
    out = {}
    for venue, p in list(_partial.items()):
        if p["started"].date() == date.today() and p["courts"]:
            out[venue] = {**summarize(venue, list(p["courts"]), p["started"]),
                          "partial": {"done": len(p["courts"]), "total": p["total"], "running": p["running"]}}
    return out


def partial_courts(venue: str) -> list[tuple[str, dict]]:
    p = _partial.get(venue)
    return list(p["courts"]) if p else []


def scan_venue(venue: str, progress=None) -> list[tuple[str, dict]]:
    """Return [(court_name, {(iso_date, hour): status}), ...] for every court of one venue,
    covering today through BOOK_WINDOW[1] days ahead. Callers must hold session.SITE_LOCK."""
    courts, sk, page = open_venue(venue)
    start = date.today()
    result = [(courts[0][1], parse_schedule(page))]
    _partial[venue] = p = {"started": datetime.now(), "courts": result, "total": len(courts), "running": True}
    try:
        if progress:
            progress(1, len(courts))
        for i, (court_id, court_name) in enumerate(courts[1:], start=2):
            session.pause(1.2)
            html = fetch_schedule(court_id, sk, start, start + timedelta(days=BOOK_WINDOW[1]))
            result.append((court_name, parse_schedule(html)))
            if progress:
                progress(i, len(courts))
    finally:
        p["running"] = False
    return result
