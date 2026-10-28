"""Local control panel for NTU venue booking.

Serves static/index.html and a small JSON API on 127.0.0.1 only.
Run:  python server.py   then open http://127.0.0.1:8765
"""
import json
import os
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import booker
import credentials
import history
import robot
import scanner
import session

HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", 8765))
ROOT = Path(__file__).parent
STATIC = ROOT / "static"

# Venue IDs come from the "常租場地" links on rent.pe.ntu.edu.tw (/order/?Add=A:<id>).
VENUES = [
    {"id": "A:2", "name": "多功能球場", "sport": "羽球"},
    {"id": "A:86", "name": "三樓主球場", "sport": "羽球"},
    {"id": "A:4", "name": "桌球室", "sport": "桌球"},
    {"id": "A:97", "name": "教室 B109", "sport": "桌球"},
    {"id": "A:11", "name": "壁球室", "sport": "壁球"},
]
VENUE_IDS = {v["id"] for v in VENUES}
VENUE_IDS_ORDERED = [v["id"] for v in VENUES]
VENUE_NAMES = {v["id"]: f"{v['name']}（{v['sport']}）" for v in VENUES}

_lock = threading.Lock()
_logs: list[dict] = []


def log(level: str, msg: str) -> None:
    with _lock:
        _logs.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "msg": msg})
        del _logs[:-500]


_login = {"in_progress": False}


def _err(e: Exception) -> str:
    return f"{e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}"


def _run_login(username: str, password: str) -> None:
    try:
        sso = session.sso_username(username)
        if sso:
            try:
                session.sso_login(sso, password)
                log("info", "已用計中帳號登入，已儲存登入狀態")
                ROBOT.logged_in()
                return
            except Exception as e:  # wrong password, an extra verification step, Edge missing, ...
                log("warn", f"計中帳號自動登入沒有成功（{_err(e)}），改開 Edge 視窗讓你手動登入")
        session.interactive_login(username, password)
        log("info", "登入成功，已儲存登入狀態")
        ROBOT.logged_in()
    except Exception as e:  # Playwright timeout, closed window, missing Edge, ...
        log("error", f"登入未完成：{_err(e)}")
    finally:
        _login["in_progress"] = False


# Unattended re-login through 計中 SSO when a request finds the login expired. After a failure it waits
# RELOGIN_COOLDOWN_S before trying again, so a wrong password is not retried until the account locks.
RELOGIN_COOLDOWN_S = 10 * 60
_relogin_lock = threading.Lock()
_relogin_failed_at = [0.0]


def _auto_relogin() -> bool:
    with _relogin_lock:
        if session.has_session():  # another thread logged in again while this one waited
            return True
        if _login["in_progress"] or time.monotonic() - _relogin_failed_at[0] < RELOGIN_COOLDOWN_S:
            return False
        saved = credentials.load_or_none()
        sso = session.sso_username(saved[0]) if saved else None
        if not sso:
            return False
        _login["in_progress"] = True
        try:
            session.sso_login(sso, saved[1])
            log("info", "登入已過期，已用計中帳號自動重新登入")
            return True
        except Exception as e:
            _relogin_failed_at[0] = time.monotonic()
            log("error", f"自動重新登入失敗（{_err(e)}），{RELOGIN_COOLDOWN_S // 60} 分鐘內不再自動嘗試，請按「登入網站」")
            return False
        finally:
            _login["in_progress"] = False


def _with_relogin(fn):
    """fn(), retried once after an automatic re-login if it found the login expired."""
    try:
        return fn()
    except session.NotLoggedIn:
        if not _auto_relogin():
            raise
        return fn()


_history_job = {"running": False, "venue": None, "done": 0, "total": 0}


def _run_history(venue: str, since) -> None:
    def progress(i, n):
        _history_job.update(done=i, total=n)

    try:
        _with_relogin(lambda: history.update(venue, since, progress))
        log("info", f"歷史資料已更新：{venue} 自 {since.isoformat()}")
    except session.NotLoggedIn as e:
        log("error", f"歷史資料更新失敗：{e}，請按「登入網站」")
    except session.Preempted:
        log("warn", "歷史資料更新被預訂中斷，預訂完成後請再按一次「更新歷史資料」")
    except Exception as e:
        log("error", f"歷史資料更新失敗：{e.__class__.__name__}: {e}")
    finally:
        _history_job.update(running=False)


_search_job = {"running": False, "venues": [], "venue": None, "step": 0, "done": 0, "total": 0}
_search_results: dict[str, dict] = {}  # latest per-venue result, from a manual search or a robot round


_book_job = {"running": False, "hit": None, "message": "", "error": None}
BOOKING_BUSY = "Edge 預訂視窗開著時無法讀取網站，請先完成預訂並關閉視窗"

ROBOT = robot.Robot(log, VENUE_NAMES, VENUE_IDS_ORDERED, on_result=_search_results.__setitem__,
                    is_paused=lambda: _book_job["running"], relogin=_auto_relogin)


def _run_book(venue: str, day: str, hour: int, quantity: int) -> None:
    def status(msg):
        _book_job.update(message=msg)
        log("info", f"預訂：{msg}")

    label = f"{VENUE_NAMES[venue]} {day} {hour:02d}:00–{hour + 1:02d}:00"
    members = robot.load_config().get("members", robot.DEFAULT_MEMBERS)
    try:
        status(f"準備預訂 {label}，共 {quantity} 面")
        done = _with_relogin(lambda: booker.open_booking(venue, day, hour, members, status, quantity=quantity))
        status(f"Edge 視窗已關閉（{len(done)}/{quantity} 面到過確認頁）")
    except booker.SlotGone as e:
        _book_job.update(error=str(e))
        log("warn", f"預訂失敗：{label} {e}")
        hits = robot.load_hits()  # the robot's list may hold the same slot
        gone = hits.get(f"{venue}|{day}|{hour}")
        if gone:
            gone.update(status="gone", gone_at=datetime.now().isoformat(timespec="seconds"))
        robot.save_hits(hits)
    except session.NotLoggedIn as e:
        _book_job.update(error=f"{e}，請先按「登入網站」")
        log("error", f"預訂失敗：{e}，請先按「登入網站」")
    except booker.Stuck as e:
        _book_job.update(error=str(e))
        log("warn", f"預訂：{e}；細節存在 snapshots/_booking_failed.txt")
    except Exception as e:  # closed window early, Edge missing, page changed, ...
        _book_job.update(error=_err(e))
        log("error", f"預訂中斷：{_book_job['error']}")
    finally:
        _book_job.update(running=False)


def _start_book(venue: str, day: str, hour: int, quantity: int, hit_id: str | None = None) -> None:
    _book_job.update(running=True, hit=hit_id, message="準備中…", error=None)
    threading.Thread(target=_run_book, args=(venue, day, hour, quantity), daemon=True).start()


def _sync_search_from_cache() -> None:
    """Rebuild results for venues whose cache (from a search or a robot scan) is newer than what we hold."""
    for venue in VENUE_IDS_ORDERED:
        cached = scanner.cache_get(venue, 24 * 3600)
        if cached and (venue not in _search_results or cached[1].isoformat(timespec="seconds") > _search_results[venue]["generated"]):
            _search_results[venue] = scanner.search(venue, max_age_s=24 * 3600)


def _run_search(venues: list[str], force: bool) -> None:
    def progress(i, n):
        _search_job.update(done=i, total=n)

    try:
        for step, venue in enumerate(venues, start=1):
            _search_job.update(venue=venue, step=step, done=0, total=0)
            result = _with_relogin(lambda: scanner.search(venue, progress, max_age_s=0 if force else scanner.CACHE_MAX_AGE_S))
            _search_results[venue] = result
            n_open = sum(bool(c["open"]) for c in result["cells"].values())
            n_onsite = sum(bool(c["onsite"]) for c in result["cells"].values())
            src = f"（使用 {result['generated'][11:16]} 的快取）" if result["from_cache"] else ""
            log("info", f"可租時段搜尋完成：{VENUE_NAMES[venue]} 線上可租 {n_open} 個時段、僅現場 {n_onsite} 個時段{src}")
    except session.NotLoggedIn as e:
        log("error", f"搜尋失敗：{e}，請按「登入網站」")
    except session.Preempted:
        log("warn", "搜尋被預訂中斷，預訂完成後請再搜尋一次")
    except Exception as e:
        log("error", f"搜尋失敗：{e.__class__.__name__}: {e}")
    finally:
        _search_job.update(running=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # Only accept requests addressed to this local server, so other websites
    # open in the same browser cannot drive the API.
    def _trusted(self) -> bool:
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.removeprefix("http://") in allowed

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _static(self, path: str):
        target = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(
            target.suffix, "application/octet-stream"
        )
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._trusted():
            return self.send_error(403)
        path = self.path.split("?")[0]
        if path == "/api/credentials":
            try:
                saved = credentials.load()
            except OSError:
                return self._json({"username": "", "has_password": False, "error": "已存資料無法解密"})
            return self._json({"username": saved[0] if saved else "", "has_password": bool(saved)})
        if path == "/api/venues":
            return self._json(VENUES)
        if path == "/api/logs":
            with _lock:
                return self._json(list(_logs))
        if path == "/api/robot":
            cfg = robot.load_config()
            all_hits = sorted(robot.load_hits().values(), key=lambda h: (h["date"], h["hour"], h["venue"]))
            hits = [h for h in all_hits if h["status"] != "dismissed"]
            today = date.today()
            return self._json({
                "state": ROBOT.snapshot(), "interval_min": cfg["interval_min"], "filters": cfg["filters"], "hits": hits,
                "dismissed": [h for h in all_hits if h["status"] == "dismissed"],
                "hours": scanner.HOURS, "min_interval": robot.MIN_INTERVAL_MIN,
                "online_dates": [(today + timedelta(days=n)).isoformat()
                                 for n in range(scanner.BOOK_WINDOW[0], scanner.BOOK_WINDOW[1] + 1)],
                "has_session": session.has_session(), "login_in_progress": _login["in_progress"],
                "members": cfg.get("members", robot.DEFAULT_MEMBERS), "member_types": booker.MEMBER_TYPES,
                "book_job": _book_job,
            })
        if path == "/api/history":
            return self._json({"stats": history.load_all(), "job": _history_job})
        if path == "/api/search":
            if not _search_job["running"]:
                _sync_search_from_cache()
            return self._json({
                "results": _search_results, "job": _search_job, "has_session": session.has_session(),
                "cache_minutes": scanner.CACHE_MAX_AGE_S // 60,
            })
        return self._static(path)

    def do_POST(self):
        if not self._trusted():
            return self.send_error(403)
        path, body = self.path.split("?")[0], self._body()
        if path == "/api/credentials":
            username = str(body.get("username", "")).strip()
            password = body.get("password") or ""
            if not password:  # keep the stored password when only the username changes
                saved = credentials.load_or_none()
                password = saved[1] if saved else ""
            if not username or not password:
                return self._json({"error": "請輸入帳號和密碼"}, 400)
            credentials.save(username, password)
            log("info", f"已儲存帳號 {username}")
            return self._json({"ok": True})
        if path == "/api/robot/settings":
            cfg = robot.load_config()
            try:
                if "interval_min" in body:
                    cfg["interval_min"] = max(robot.MIN_INTERVAL_MIN, min(60, int(body["interval_min"])))
                if "members" in body:
                    members = {t: max(0, min(10, int((body["members"] or {}).get(t) or 0))) for t in booker.MEMBER_TYPES}
                    if not sum(members.values()):
                        return self._json({"error": "人數至少要 1 人"}, 400)
                    cfg["members"] = members
            except (TypeError, ValueError, AttributeError):
                return self._json({"error": "設定格式錯誤"}, 400)
            robot.save_config(cfg)
            return self._json({"ok": True})
        if path.startswith("/api/robot/hits/") and path.endswith("/book"):
            if _book_job["running"]:
                return self._json({"error": "已經有一筆預訂視窗開著，請先完成或關閉它"}, 409)
            hit_id = path.split("/")[4]
            hit = robot.find_hit(robot.load_hits(), hit_id)
            if hit is None or hit["status"] != "available":
                return self._json({"error": "這筆已經不能訂了"}, 404)
            if not session.has_session():
                return self._json({"error": "請先按「登入網站」"}, 400)
            quantity = robot.clamp_want(hit.get("want"), hit.get("count", 1))
            _start_book(hit["venue"], hit["date"], hit["hour"], quantity, hit_id)
            return self._json({"ok": True})
        if path == "/api/book":  # straight from the search table
            if _book_job["running"]:
                return self._json({"error": "已經有一筆預訂視窗開著，請先完成或關閉它"}, 409)
            venue = body.get("venue")
            if venue not in VENUE_IDS:
                return self._json({"error": "請選擇場地"}, 400)
            try:
                day, hour = date.fromisoformat(body["date"]), int(body["hour"])
                quantity = int(body.get("quantity") or 1)
            except (KeyError, TypeError, ValueError):
                return self._json({"error": "預訂資料格式錯誤"}, 400)
            today = date.today()
            if not (today + timedelta(days=scanner.BOOK_WINDOW[0]) <= day <= today + timedelta(days=scanner.BOOK_WINDOW[1])):
                return self._json({"error": "只能線上預訂租借日前 3–7 天的時段"}, 400)
            cell = (_search_results.get(venue) or {}).get("cells", {}).get(f"{day.isoformat()}|{hour}")
            if not cell or not cell["open"]:
                return self._json({"error": "這個時段目前沒有線上空場，請重新搜尋"}, 404)
            if not 1 <= quantity <= cell["open"]:
                return self._json({"error": f"面數需介於 1 到 {cell['open']} 面（目前可借場數）"}, 400)
            if not session.has_session():
                return self._json({"error": "請先按「登入網站」"}, 400)
            _start_book(venue, day.isoformat(), hour, quantity)
            return self._json({"ok": True})
        if path.startswith("/api/robot/hits/") and path.endswith("/want"):
            hit_id = path.split("/")[4]
            hits = robot.load_hits()
            hit = robot.find_hit(hits, hit_id)
            if hit is None:
                return self._json({"error": "找不到這筆"}, 404)
            try:
                want = int(body.get("want"))
            except (TypeError, ValueError):
                return self._json({"error": "數量格式錯誤"}, 400)
            if not 1 <= want <= hit.get("count", 1):
                return self._json({"error": f"數量需介於 1 到 {hit.get('count', 1)} 面（目前可借場數）"}, 400)
            hit["want"] = want
            robot.save_hits(hits)
            return self._json({"ok": True, "want": want})
        if path == "/api/robot/filters" or path.startswith("/api/robot/filters/"):
            try:
                cleaned = robot.clean_filter(body, VENUE_IDS_ORDERED)
            except (TypeError, ValueError):
                return self._json({"error": "濾網格式錯誤"}, 400)
            cfg = robot.load_config()
            if path == "/api/robot/filters":
                f = {"id": robot._new_id(), **cleaned}
                cfg["filters"].append(f)
                log("info", f"新增濾網［{f['name']}］")
            else:
                f = next((f for f in cfg["filters"] if f["id"] == path.rsplit("/", 1)[1]), None)
                if f is None:
                    return self._json({"error": "找不到濾網"}, 404)
                if cleaned["enabled"] != f["enabled"]:
                    log("info", f"濾網［{cleaned['name']}］已{'啟用' if cleaned['enabled'] else '停用'}")
                f.update(cleaned)
            robot.save_config(cfg)
            ROBOT.reschedule()
            return self._json(f)
        if path == "/api/robot/scan":
            if _book_job["running"]:
                return self._json({"error": BOOKING_BUSY}, 409)
            ROBOT.scan_now()
            log("info", "手動觸發機器人掃描")
            return self._json({"ok": True})
        if path.startswith("/api/robot/hits/") and path.endswith("/restore"):
            hits = robot.load_hits()
            hit = robot.find_hit(hits, path.split("/")[4])
            if hit is None or hit["status"] != "dismissed":
                return self._json({"error": "這筆已經不在了（空位關閉後會自動清掉）"}, 404)
            hit["status"] = "available"  # a dismissed hit is only kept while the slot is still open
            robot.save_hits(hits)
            return self._json({"ok": True})
        if path.startswith("/api/robot/hits/") and path.endswith("/dismiss"):
            hit_id = path.split("/")[4]
            hits = robot.load_hits()
            hit = robot.find_hit(hits, hit_id)
            if hit is None:
                return self._json({"error": "找不到這筆"}, 404)
            hit["status"] = "dismissed"
            robot.save_hits(hits)
            return self._json({"ok": True})
        if path == "/api/search":
            if _search_job["running"]:
                return self._json({"error": "搜尋進行中"}, 409)
            if _book_job["running"]:
                return self._json({"error": BOOKING_BUSY}, 409)
            venues = [v for v in VENUE_IDS_ORDERED if v in (body.get("venues") or [])]
            if not venues:
                return self._json({"error": "請至少選一個場地"}, 400)
            force = bool(body.get("force"))
            _search_job.update(running=True, venues=venues, venue=venues[0], step=0, done=0, total=0)
            threading.Thread(target=_run_search, args=(venues, force), daemon=True).start()
            log("info", f"開始{'重新抓取' if force else '搜尋'}可租時段：{'、'.join(VENUE_NAMES[v] for v in venues)}")
            return self._json({"ok": True})
        if path == "/api/history":
            if _history_job["running"]:
                return self._json({"error": "歷史資料正在更新中"}, 409)
            if _book_job["running"]:
                return self._json({"error": BOOKING_BUSY}, 409)
            venue = body.get("venue")
            if venue not in VENUE_IDS:
                return self._json({"error": "請選擇場地"}, 400)
            try:
                since = datetime.strptime(body.get("since", ""), "%Y-%m-%d").date()
            except ValueError:
                return self._json({"error": "起始日期格式錯誤"}, 400)
            if not (date.today() - timedelta(days=200) <= since < date.today()):
                return self._json({"error": "起始日期需在今天之前、200 天以內"}, 400)
            _history_job.update(running=True, venue=venue, done=0, total=0)
            threading.Thread(target=_run_history, args=(venue, since), daemon=True).start()
            log("info", f"開始更新歷史資料：{venue} 自 {since.isoformat()}")
            return self._json({"ok": True})
        if path == "/api/session/login":
            if _login["in_progress"]:
                return self._json({"error": "登入視窗已開啟"}, 409)
            saved = credentials.load_or_none()
            if not saved:
                return self._json({"error": "請先在設定儲存帳號密碼"}, 400)
            _login["in_progress"] = True
            threading.Thread(target=_run_login, args=saved, daemon=True).start()
            log("info", "登入中：先用計中帳號自動登入，不行的話會開 Edge 視窗讓你手動登入（5 分鐘內）")
            return self._json({"ok": True})
        return self.send_error(404)

    def do_DELETE(self):
        if not self._trusted():
            return self.send_error(403)
        path = self.path.split("?")[0]
        if path == "/api/credentials":
            credentials.delete()
            log("info", "已刪除儲存的帳號密碼")
            return self._json({"ok": True})
        if path.startswith("/api/robot/filters/"):
            cfg = robot.load_config()
            keep = [f for f in cfg["filters"] if f["id"] != path.rsplit("/", 1)[1]]
            if len(keep) == len(cfg["filters"]):
                return self._json({"error": "找不到濾網"}, 404)
            cfg["filters"] = keep
            robot.save_config(cfg)
            ROBOT.reschedule()
            log("info", "已刪除濾網")
            return self._json({"ok": True})
        if path == "/api/robot/hits":
            hits = {k: h for k, h in robot.load_hits().items() if h["status"] != "gone"}
            robot.save_hits(hits)
            return self._json({"ok": True})
        if path == "/api/session":
            session.clear()
            log("info", "已清除登入狀態")
            return self._json({"ok": True})
        return self.send_error(404)


if __name__ == "__main__":
    import sys

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    log("info", "控制台已啟動")
    ROBOT.reschedule()  # start scanning right away if any filter is enabled
    if session.has_session():  # a login saved hours ago has probably expired; find out before showing "logged in"
        threading.Thread(target=session.check, daemon=True).start()
    print(f"臺大場地預約控制台：{url}  (Ctrl+C 結束)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
