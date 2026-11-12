"""Background robot, shared by everybody.

Each person has filters: a named combination of venues plus a weekly template (weekday -> start hours).
Every few minutes one shared round walks the union of the venues of everybody's enabled filters, through
the scan account's login and the same scanner.search() + cache as the 可租時段搜尋 tab. Then each
person's online-bookable slots that match one of their filters go onto their own hit list
(data/users/<account>/hits.json). Hits that are no longer open are marked "gone"; hits whose date has
passed are dropped. New hits go to `on_new_hits`, which notifies and may prepare a booking.

One walk for everybody keeps the load on the school's site the same however many people use it.
"""
import json
import random
import threading
import uuid
from datetime import date, datetime, timedelta

import config
import scanner
import session

MIN_INTERVAL_MIN = 3  # keep the load on the school's server low
REUSE_CACHE_S = 60  # a search that ran in the last minute is fresh enough for the robot
DEFAULT_MEMBERS = {"1": 2, "2": 0, "3": 0, "4": 0}  # head count per member type; most venues need >= 2

_file_lock = threading.Lock()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------- per-person config ----------

def _config_file(acct: session.Account):
    return acct.dir / "robot.json"


def _hits_file(acct: session.Account):
    return acct.dir / "hits.json"


def load_config(acct: session.Account) -> dict:
    with _file_lock:
        f = _config_file(acct)
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    return {"filters": [], "members": dict(DEFAULT_MEMBERS), "discord_id": "", **data}


def save_config(acct: session.Account, cfg: dict) -> None:
    with _file_lock:
        config.write_json(_config_file(acct), cfg)


def clean_filter(body: dict, venue_ids: list[str]) -> dict:
    slots = {}
    for wd, hours in (body.get("slots") or {}).items():
        if str(wd) in {str(i) for i in range(7)}:
            valid = sorted({int(h) for h in hours if int(h) in scanner.HOURS})
            if valid:
                slots[str(wd)] = valid
    return {
        "name": (str(body.get("name") or "").strip() or "未命名濾網")[:30],
        "enabled": bool(body.get("enabled")),
        "venues": [v for v in venue_ids if v in (body.get("venues") or [])],
        "slots": slots,
        "mode": filter_mode(body),
    }


MODES = ("notify", "prepare", "auto")


def filter_mode(f: dict) -> str:
    """What happens when a filter finds a slot:
    notify  - a message with 預訂 buttons; nothing is filled until a button is pressed
    prepare - fill up to the school's confirmation page at once; 確認送出 is left to the person
    auto    - fill and send at once (server.AUTO_DAILY_LIMIT courts a day), then report
    Filters from before modes have only auto_prepare (True -> prepare)."""
    mode = f.get("mode")
    if mode in MODES:
        return mode
    return "prepare" if f.get("auto_prepare", True) else "notify"


def is_active(f: dict) -> bool:
    return f["enabled"] and bool(f["venues"]) and bool(f["slots"])


# ---------- hits ----------

# One hit per (venue, date, hour), keyed "venue|date|hour". `filters` lists every filter that matched it,
# `count` is how many courts are open, and `want` is how many the person wants to book (1..count).

def load_hits(acct: session.Account) -> dict:
    with _file_lock:
        f = _hits_file(acct)
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def find_hit(hits: dict, hit_id: str) -> dict | None:
    return next((h for h in hits.values() if h["id"] == hit_id), None)


def save_hits(acct: session.Account, hits: dict) -> None:
    with _file_lock:
        config.write_json(_hits_file(acct), hits)


def clamp_want(want, count: int) -> int:
    return max(1, min(int(want or 1), max(1, count)))


def update_hits(acct: session.Account, filters: list[dict], results: dict[str, dict]) -> list[dict]:
    """Merge one round of search results into acct's hit list. Returns the newly added hits."""
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

    hits = load_hits(acct)
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
            added.append(hit)  # open again: worth telling
        hit.update(count=cell["open"], last_seen=now, filters=sorted(set(fids)))
        hit["want"] = clamp_want(hit.get("want"), hit["count"])

    filter_ids = {f["id"] for f in load_config(acct)["filters"]}
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
    save_hits(acct, hits)
    return added


def mark_gone(acct: session.Account, venue: str, day: str, hour: int) -> None:
    hits = load_hits(acct)
    hit = hits.get(f"{venue}|{day}|{hour}")
    if hit:
        hit.update(status="gone", gone_at=datetime.now().isoformat(timespec="seconds"))
        save_hits(acct, hits)


# ---------- the shared loop ----------

def _active_by_user() -> dict[str, list[dict]]:
    out = {}
    for name in config.users():
        active = [f for f in load_config(session.account(name))["filters"] if is_active(f)]
        if active:
            out[name] = active
    return out


class Robot:
    def __init__(self, log, venue_names: dict[str, str], venue_order: list[str], on_result, on_new_hits,
                 on_error=lambda msg: None):
        self.log, self.venue_names, self.venue_order = log, venue_names, venue_order
        self.on_result = on_result  # (venue, result) after each venue of a round
        self.on_new_hits = on_new_hits  # (account, [(hit, [filter])]) after a round
        self.on_error = on_error  # (message) when a round fails, for the admins
        self.lock = threading.Lock()
        self.state = {"running": False, "last_scan": None, "next_scan": None, "error": None,
                      "venue": None, "done": 0, "total": 0}
        self._wake = threading.Event()
        self._failures = 0
        threading.Thread(target=self._loop, daemon=True, name="robot").start()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def scan_now(self) -> None:
        with self.lock:
            self.state["next_scan"] = datetime.now().isoformat(timespec="seconds")
        self._wake.set()

    def reschedule(self) -> None:
        """Start scanning as soon as anybody's filter is active; stop scheduling when none is."""
        active = bool(_active_by_user())
        with self.lock:
            if not active:
                self.state["next_scan"] = None
            elif not self.state["next_scan"]:
                self.state["next_scan"] = datetime.now().isoformat(timespec="seconds")
        self._wake.set()

    def logged_in(self) -> None:
        """A fresh login of the scan account: drop the stale error and scan right away if anything is active."""
        with self.lock:
            self.state["error"] = None
        if _active_by_user():
            self.scan_now()

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=5)
            self._wake.clear()
            with self.lock:
                due = self.state["next_scan"] and datetime.fromisoformat(self.state["next_scan"]) <= datetime.now()
            if not due:
                continue
            preempted = False
            try:
                preempted = self._scan()
            except Exception as e:  # never let one bad round kill the thread
                self.log("error", f"機器人掃描發生錯誤：{e.__class__.__name__}: {e}")
                with self.lock:
                    self.state.update(running=False, error=str(e))
            interval = max(MIN_INTERVAL_MIN, int(config.load()["interval_min"]))
            with self.lock:
                if preempted:  # the scan account is booking; try again in a minute
                    self.state["next_scan"] = (datetime.now() + timedelta(minutes=1)).isoformat(timespec="seconds")
                elif _active_by_user():
                    # A little jitter so requests don't land on exact minute boundaries.
                    nxt = datetime.now() + timedelta(minutes=interval, seconds=random.randint(0, 30))
                    self.state["next_scan"] = nxt.isoformat(timespec="seconds")
                else:
                    self.state["next_scan"] = None

    def _scan(self) -> bool:
        """One round over the venues of everybody's active filters. Returns True when a booking cut it short."""
        by_user = _active_by_user()
        venues = [v for v in self.venue_order if any(v in f["venues"] for fs in by_user.values() for f in fs)]
        scan_name = config.scan_account()
        with self.lock:
            self.state.update(running=True, error=None)
        results, error, preempted = {}, None, False
        if not scan_name:
            error = "還沒設定掃描帳號（manage.py set-admin）"
            venues = []
        scan = session.account(scan_name) if scan_name else None

        for venue in venues:
            def progress(i, n, venue=venue):
                with self.lock:
                    self.state.update(venue=venue, done=i, total=n)
            try:
                res = scan.with_relogin(lambda: scanner.search(scan, venue, progress, max_age_s=REUSE_CACHE_S))
            except session.NotLoggedIn as e:
                error = f"掃描帳號 {scan_name}：{scan.login_error or e}"
                break
            except session.Preempted:
                preempted = True
                break
            except (OSError, ValueError) as e:
                error = f"讀取失敗：{e}"
                break
            results[venue] = res
            self.on_result(venue, res)

        n_added = 0
        for name, filters in by_user.items():
            acct = session.account(name)
            added = update_hits(acct, filters, results)
            n_added += len(added)
            if added:
                names = {f["id"]: f for f in filters}
                self.on_new_hits(acct, [(h, [names[fid] for fid in h["filters"] if fid in names]) for h in added])
        if error:
            self._failures += 1
            self.log("error", f"機器人掃描失敗：{error}")
            if self._failures in (1, 5):  # tell the admins once, and again if it keeps failing
                self.on_error(error)
        else:
            self._failures = 0
            if preempted:
                self.log("info", "機器人掃描先暫停讓預訂使用網站，1 分鐘後重新掃描")
            else:
                self.log("info", f"機器人掃描完成：{len(venues)} 個場地、{len(by_user)} 人，新增 {n_added} 筆")
        with self.lock:
            self.state.update(running=False, error=error, venue=None,
                              last_scan=datetime.now().isoformat(timespec="seconds"))
        return preempted
