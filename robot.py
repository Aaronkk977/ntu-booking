"""Background robot.

Each filter is a named combination of venues plus a weekly template (weekday -> start hours).
Every few minutes the robot looks up the venues used by enabled filters through the same
scanner.search() + cache as the 可租時段搜尋 tab, and every online-bookable slot that matches a
filter goes onto the hit list (hits.json). Hits that are no longer open are marked "gone";
hits whose date has passed are dropped. Nothing is ever booked.
"""
import json
import random
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import scanner
import session

CONFIG_FILE = Path(__file__).with_name("robot.json")
HITS_FILE = Path(__file__).with_name("hits.json")
LEGACY_WATCH_FILE = Path(__file__).with_name("watch.json")
MIN_INTERVAL_MIN = 3  # keep the load on the school's server low
REUSE_CACHE_S = 60  # a search that ran in the last minute is fresh enough for the robot
DEFAULT_MEMBERS = {"1": 2, "2": 0, "3": 0, "4": 0}  # head count per member type; most venues need >= 2

_file_lock = threading.Lock()


# ---------- config ----------

def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def load_config() -> dict:
    with _file_lock:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg = {"interval_min": 5, "filters": [], "members": dict(DEFAULT_MEMBERS)}
        # Carry over the single filter from the old watch.json.
        if LEGACY_WATCH_FILE.exists():
            w = json.loads(LEGACY_WATCH_FILE.read_text(encoding="utf-8"))
            cfg["interval_min"] = w.get("interval_min", 5)
            if w.get("slots"):
                cfg["filters"].append({"id": _new_id(), "name": "我的濾網", "enabled": bool(w.get("enabled")),
                                       "venues": w.get("venues", []), "slots": w["slots"]})
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg


def save_config(cfg: dict) -> None:
    with _file_lock:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_filter(body: dict, venue_ids: list[str]) -> dict:
    slots = {}
    for wd, hours in (body.get("slots") or {}).items():
        if str(wd) in {str(i) for i in range(7)}:
            valid = sorted({int(h) for h in hours if int(h) in scanner.OPEN_HOURS[int(wd)]})
            if valid:
                slots[str(wd)] = valid
    return {
        "name": (str(body.get("name") or "").strip() or "未命名濾網")[:30],
        "enabled": bool(body.get("enabled")),
        "venues": [v for v in venue_ids if v in (body.get("venues") or [])],
        "slots": slots,
    }


def is_active(f: dict) -> bool:
    return f["enabled"] and bool(f["venues"]) and bool(f["slots"])


# ---------- hits ----------

# One hit per (venue, date, hour), keyed "venue|date|hour". `filters` lists every filter that matched it,
# `count` is how many courts are open, and `want` is how many the user wants to book (1..count).

def load_hits() -> dict:
    with _file_lock:
        hits = json.loads(HITS_FILE.read_text(encoding="utf-8")) if HITS_FILE.exists() else {}
    # Older files had one hit per filter ("filter|venue|date|hour"); fold them together.
    if any("filter" in h for h in hits.values()):
        merged = {}
        for h in hits.values():
            key = f"{h['venue']}|{h['date']}|{h['hour']}"
            fids = h.get("filters") or [h.pop("filter")]
            h.pop("filter", None)
            if key in merged:
                merged[key]["filters"] = sorted(set(merged[key]["filters"]) | set(fids))
            else:
                merged[key] = {**h, "filters": fids, "want": h.get("want", 1)}
        hits = merged
    return hits


def save_hits(hits: dict) -> None:
    with _file_lock:
        HITS_FILE.write_text(json.dumps(hits, ensure_ascii=False, indent=2), encoding="utf-8")


def clamp_want(want, count: int) -> int:
    return max(1, min(int(want or 1), max(1, count)))


def update_hits(filters: list[dict], results: dict[str, dict]) -> list[dict]:
    """Merge one round of search results into the hit list. Returns the newly added hits."""
    matched: dict[str, tuple[dict, list[str]]] = {}  # key -> (cell, filter ids)
    for f in filters:
        for venue in f["venues"]:
            res = results.get(venue)
            if not res:
                continue
            for day in res["dates"]:
                if day < res["online_from"]:  # walk-in only, the robot can't use those
                    continue
                wd = date.fromisoformat(day).weekday()
                for h in f["slots"].get(str(wd), []):
                    cell = res["cells"].get(f"{day}|{h}")
                    if cell and cell["open"]:
                        key = f"{venue}|{day}|{h}"
                        matched.setdefault(key, (cell, []))[1].append(f["id"])

    hits = load_hits()
    now = datetime.now().isoformat(timespec="seconds")
    added = []
    for key, (cell, fids) in matched.items():
        venue, day, h = key.split("|")
        hit = hits.get(key)
        if hit is None:
            hit = hits[key] = {"id": _new_id(), "venue": venue, "date": day, "hour": int(h),
                               "first_seen": now, "status": "available", "want": 1}
            added.append(hit)
        elif hit["status"] == "gone":
            hit["status"] = "available"
        hit.update(count=cell["open"], last_seen=now, filters=sorted(set(fids)))
        hit["want"] = clamp_want(hit.get("want"), hit["count"])

    filter_ids = {f["id"] for f in load_config()["filters"]}
    today = date.today().isoformat()
    for key, hit in list(hits.items()):
        hit["filters"] = [fid for fid in hit.get("filters", []) if fid in filter_ids]
        if hit["date"] < today or not hit["filters"]:
            del hits[key]
        elif key not in matched and hit["venue"] in results:
            if hit["status"] == "dismissed":
                del hits[key]  # it went away, so a later reopening counts as new again
            elif hit["status"] == "available":
                hit.update(status="gone", gone_at=now)
    save_hits(hits)
    return added


# ---------- loop ----------

class Robot:
    def __init__(self, log, venue_names: dict[str, str], venue_order: list[str], on_result, is_paused=lambda: False):
        self.log, self.venue_names, self.venue_order, self.on_result = log, venue_names, venue_order, on_result
        self.is_paused = is_paused  # e.g. a booking window holds the site; the due round runs once it closes
        self.lock = threading.Lock()
        self.state = {"running": False, "last_scan": None, "next_scan": None, "error": None,
                      "venue": None, "done": 0, "total": 0}
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def scan_now(self) -> None:
        with self.lock:
            self.state["next_scan"] = datetime.now().isoformat(timespec="seconds")
        self._wake.set()

    def reschedule(self) -> None:
        """Start scanning as soon as any filter is active; stop scheduling when none is."""
        active = any(is_active(f) for f in load_config()["filters"])
        with self.lock:
            if not active:
                self.state["next_scan"] = None
            elif not self.state["next_scan"]:
                self.state["next_scan"] = datetime.now().isoformat(timespec="seconds")
        self._wake.set()

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=5)
            self._wake.clear()
            with self.lock:
                due = self.state["next_scan"] and datetime.fromisoformat(self.state["next_scan"]) <= datetime.now()
            if not due or self.is_paused():
                continue
            try:
                self._scan()
            except Exception as e:  # never let one bad round kill the thread
                self.log("error", f"機器人掃描發生錯誤：{e.__class__.__name__}: {e}")
                with self.lock:
                    self.state.update(running=False, error=str(e))
            cfg = load_config()
            with self.lock:
                if any(is_active(f) for f in cfg["filters"]):
                    # A little jitter so requests don't land on exact minute boundaries.
                    nxt = datetime.now() + timedelta(minutes=cfg["interval_min"], seconds=random.randint(0, 30))
                    self.state["next_scan"] = nxt.isoformat(timespec="seconds")
                else:
                    self.state["next_scan"] = None

    def _scan(self) -> None:
        active = [f for f in load_config()["filters"] if is_active(f)]
        venues = [v for v in self.venue_order if any(v in f["venues"] for f in active)]
        with self.lock:
            self.state.update(running=True, error=None)
        results, error = {}, None

        for venue in venues:
            def progress(i, n, venue=venue):
                with self.lock:
                    self.state.update(venue=venue, done=i, total=n)
            try:
                res = scanner.search(venue, progress, max_age_s=REUSE_CACHE_S)
            except session.NotLoggedIn as e:
                error = f"{e}，請按「登入網站」"
                break
            except (OSError, ValueError) as e:
                error = f"讀取失敗：{e}"
                break
            results[venue] = res
            self.on_result(venue, res)

        added = update_hits(active, results)
        for hit in added:
            names = "、".join(f["name"] for f in active if f["id"] in hit["filters"])
            self.log("warn", f"自動加入清單：［{names}］{self.venue_names.get(hit['venue'], hit['venue'])} "
                             f"{hit['date']} {hit['hour']:02d}:00–{hit['hour'] + 1:02d}:00（{hit['count']} 面）")
        if error:
            self.log("error", f"機器人掃描失敗：{error}")
        else:
            self.log("info", f"機器人掃描完成：{len(venues)} 個場地，新增 {len(added)} 筆到清單")
        with self.lock:
            self.state.update(running=False, error=error, venue=None,
                              last_scan=datetime.now().isoformat(timespec="seconds"))
