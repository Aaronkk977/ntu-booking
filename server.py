"""Web app for NTU venue booking, for a few people, opened from their phones.

Listens on 127.0.0.1 only; the outside world reaches it through a Cloudflare Tunnel (see deploy/).
People log in with their own 計中 account (only accounts on the allow list in data/config.json); the
password is checked by really logging in to the school's site through 計中 SSO, then kept encrypted so
the server can log in again on its own when the school's login expires.

Run:  python server.py        (PORT, PUBLIC_URL and NTU_BOOKING_DATA can be set in the environment)
"""
import hashlib
import json
import os
import secrets
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import booker
import config
import credentials
import history
import notify
import orders
import robot
import scanner
import session

HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", 8765))
STATIC = config.ROOT / "static"
LOCAL_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

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

config.init_dirs()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # data/logs/server.log is read as UTF-8

# ---------- logs ----------
# Entries with an account are shown to that person; entries without one (robot rounds, server events)
# only to the admins.

_lock = threading.Lock()
_logs: list[dict] = []


def log(level: str, msg: str, account: str | None = None) -> None:
    with _lock:
        _logs.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "msg": msg,
                      "account": account})
        del _logs[:-1000]
    try:  # the console may not take every character (cp950 on a Windows console); logging must never throw
        print(f"[{level}] {account or '-'}: {msg}", flush=True)
    except (UnicodeError, OSError):
        pass


def _err(e: Exception) -> str:
    return f"{e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}"


def slot_label(venue: str, day: str, hour: int) -> str:
    return f"{VENUE_NAMES.get(venue, venue)} {day} {hour:02d}:00–{hour + 1:02d}:00"


def _discord_id(acct: session.Account) -> str:
    return robot.load_config(acct).get("discord_id", "")


# ---------- logins to this web app ----------

WEB_SESSIONS_FILE = config.DATA / "web_sessions.json"
WEB_SESSION_DAYS = 30
COOKIE = "nb_session"
_web_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _web_sessions() -> dict:
    return json.loads(WEB_SESSIONS_FILE.read_text(encoding="utf-8")) if WEB_SESSIONS_FILE.exists() else {}


def new_web_session(account: str) -> str:
    token = secrets.token_urlsafe(32)
    with _web_lock:
        data = _web_sessions()
        cutoff = (datetime.now() - timedelta(days=WEB_SESSION_DAYS)).isoformat()
        data = {k: v for k, v in data.items() if v["created"] > cutoff}
        data[_hash(token)] = {"account": account, "created": datetime.now().isoformat(timespec="seconds")}
        config.write_json(WEB_SESSIONS_FILE, data)
    return token


def web_session_account(token: str | None) -> str | None:
    if not token:
        return None
    with _web_lock:
        entry = _web_sessions().get(_hash(token))
    if not entry or datetime.fromisoformat(entry["created"]) < datetime.now() - timedelta(days=WEB_SESSION_DAYS):
        return None
    return entry["account"] if config.is_allowed(entry["account"]) else None


def end_web_sessions(token: str | None = None, account: str | None = None) -> None:
    """Drop one login (token), or every login of an account."""
    with _web_lock:
        data = _web_sessions()
        data = {k: v for k, v in data.items()
                if not ((token and k == _hash(token)) or (account and v["account"] == account))}
        config.write_json(WEB_SESSIONS_FILE, data)


# Failed logins are limited per account and per IP, so nobody can use this page to try passwords on a
# 計中 account until it locks.
# Per IP the limit is higher: friends on the same campus Wi-Fi share one address.
LOGIN_FAILS_MAX = {"a": 5, "i": 20}
LOGIN_FAILS_WINDOW_S = 15 * 60
_fails: dict[str, list[float]] = {}


def _too_many_fails(*keys: str) -> bool:
    now = time.monotonic()
    for k in keys:
        _fails[k] = [t for t in _fails.get(k, []) if now - t < LOGIN_FAILS_WINDOW_S]
        if len(_fails[k]) >= LOGIN_FAILS_MAX[k[0]]:
            return True
    return False


def _add_fail(*keys: str) -> None:
    for k in keys:
        _fails.setdefault(k, []).append(time.monotonic())


# ---------- orders (per person) ----------

ORDERS_INTERVAL_S = 30 * 60
_orders: dict[str, dict] = {}


def orders_state(acct: session.Account) -> dict:
    if acct.name not in _orders:
        _orders[acct.name] = {"data": orders.load_saved(acct), "running": False, "error": None}
    return _orders[acct.name]


def _run_orders(acct: session.Account) -> None:
    st = orders_state(acct)
    try:
        data = acct.with_relogin(lambda: orders.fetch_orders(acct))
        st.update(data=data, error=None)
        _deadline_reminders(acct, data)
    except session.NotLoggedIn as e:
        st.update(error=acct.login_error or str(e))
    except Exception as e:  # offline, page changed, ...
        st.update(error=_err(e))
        log("error", f"讀取訂單失敗：{_err(e)}", acct.name)
    finally:
        st.update(running=False)


def start_orders(acct: session.Account) -> bool:
    st = orders_state(acct)
    if st["running"]:
        return False
    st.update(running=True)
    threading.Thread(target=_run_orders, args=(acct,), daemon=True).start()
    return True


def _deadline_reminders(acct: session.Account, data: dict) -> None:
    """DM once per order when its payment deadline is a day or less away."""
    cfg = robot.load_config(acct)
    told = set(cfg.get("deadline_told", []))
    soon = date.today() + timedelta(days=1)
    new = [o for o in data["orders"] if o["kind"] == "unpaid" and o["deadline"] and o["id"] not in told
           and date.fromisoformat(o["deadline"][:10]) <= soon and any(s["date"] >= date.today().isoformat() for s in o["slots"])]
    for o in new:
        when = "、".join(f"{s['date']} {s['start']}" for s in o["slots"])
        notify.send(_discord_id(acct), f"⏰ 繳費期限快到了：{o['venue']} {when}，截止 {o['deadline']}，金額 {o['amount']}",
                    ("打開訂單", notify.url("#orders")))
    if new:
        cfg["deadline_told"] = sorted(told | {o["id"] for o in new})[-50:]
        robot.save_config(acct, cfg)


def _orders_loop() -> None:
    while True:
        for name in config.users():
            acct = session.account(name)
            data = orders_state(acct)["data"]
            age = (datetime.now() - datetime.fromisoformat(data["fetched"])).total_seconds() if data else None
            if acct.has_session() and (age is None or age >= ORDERS_INTERVAL_S):
                start_orders(acct)
        time.sleep(60)


# ---------- booking ----------

def _on_prep_change(prep: booker.Prep) -> None:
    acct = prep.acct
    log({"failed": "error", "expired": "warn"}.get(prep.status, "info"), f"預訂 {prep.label}：{prep.message}", acct.name)
    did = _discord_id(acct)
    if prep.status == "ready":
        notify.send(did, f"🏸 {prep.label}\n{prep.message}", ("看確認頁截圖", notify.url(f"#prep/{prep.id}")),
                    buttons=[notify.button("確認送出", f"prep:confirm:{prep.id}", 3),
                             notify.button("取消", f"prep:cancel:{prep.id}", 4)])
    elif prep.status == "done":
        notify.send(did, f"✅ {prep.label}\n{prep.message}", ("打開訂單", notify.url("#orders")))
        start_orders(acct)
    elif prep.status in ("failed", "expired"):
        notify.send(did, f"⚠️ {prep.label}\n{prep.message}")
        if prep.status == "failed" and "沒有可訂" in prep.message:
            robot.mark_gone(acct, prep.venue, prep.day, prep.hour)
        if prep.booked:
            start_orders(acct)


def start_prep(acct: session.Account, venue: str, day: str, hour: int, quantity: int) -> booker.Prep:
    members = robot.load_config(acct).get("members", robot.DEFAULT_MEMBERS)
    return booker.prepare(acct, venue, slot_label(venue, day, hour), day, hour, quantity, members, _on_prep_change)


def _on_new_hits(acct: session.Account, found: list[tuple[dict, list[dict]]]) -> None:
    """The robot found slots for this person: prepare the first auto-prepare one, tell them about the rest."""
    did = _discord_id(acct)
    for hit, filters in found:
        label = slot_label(hit["venue"], hit["date"], hit["hour"])
        names = "、".join(f["name"] for f in filters)
        log("warn", f"機器人找到：［{names}］{label}（{hit['count']} 面）", acct.name)
        auto = any(f.get("auto_prepare", True) for f in filters)
        if auto and not booker.busy(acct) and credentials.load(acct.name):
            quantity = robot.clamp_want(hit.get("want"), hit.get("count", 1))
            start_prep(acct, hit["venue"], hit["date"], hit["hour"], quantity)
            notify.send(did, f"🔎 找到空位：［{names}］{label}（{hit['count']} 面），正在幫你填到學校確認頁…")
        else:
            notify.send(did, f"🔎 找到空位：［{names}］{label}（{hit['count']} 面）", ("打開清單", notify.url(f"#hit/{hit['id']}")))


def _on_robot_error(msg: str) -> None:
    for name in config.load()["admins"]:
        notify.send(_discord_id(session.account(name)), f"🤖 機器人掃描失敗：{msg}")


# ---------- Discord interactions (/link, /unlink, the Confirm / Cancel buttons) ----------

LINK_CODE_S = 10 * 60
_link_codes: dict[str, tuple[str, float]] = {}  # one-time code from /link -> (discord user id, expiry)
EPHEMERAL = 64  # only the person who typed the command sees the reply


def _account_by_discord(discord_id: str) -> session.Account | None:
    for name in config.users():
        acct = session.account(name)
        if _discord_id(acct) == discord_id:
            return acct
    return None


def set_discord(acct: session.Account, discord_id: str) -> None:
    """Link one Discord account to one site account (dropping it from anybody else who had it)."""
    if discord_id:
        other = _account_by_discord(discord_id)
        if other and other is not acct:
            cfg = robot.load_config(other)
            cfg["discord_id"] = ""
            robot.save_config(other, cfg)
    cfg = robot.load_config(acct)
    cfg["discord_id"] = discord_id
    robot.save_config(acct, cfg)


def _reply(text: str, buttons: list[dict] | None = None) -> dict:
    return {"type": 4, "data": {"content": text, "flags": EPHEMERAL, "components": notify.row(buttons or []),
                                "allowed_mentions": {"parse": []}}}


def handle_interaction(data: dict) -> dict:
    """The response to one verified interaction from Discord."""
    kind = data.get("type")
    if kind == 1:  # PING, when Discord checks the endpoint
        return {"type": 1}
    user = (data.get("member") or {}).get("user") or data.get("user") or {}
    discord_id = str(user.get("id") or "")
    if kind == 2:
        name = (data.get("data") or {}).get("name")
        if name == "link":
            now = time.monotonic()
            for c in [c for c, (_, exp) in _link_codes.items() if exp < now]:
                del _link_codes[c]
            code = secrets.token_urlsafe(16)
            _link_codes[code] = (discord_id, now + LINK_CODE_S)
            url = notify.url(f"#link/{code}")
            log("info", f"Discord 使用者 {user.get('username', discord_id)} 要求連結")
            if not url.startswith("https://"):  # link buttons need https (local testing)
                return _reply(f"打開這個網址完成連結（10 分鐘內有效）：{url}")
            return _reply("點下面的按鈕，在預約網站登入後按「連結」，之後的通知就會私訊到這裡（10 分鐘內有效）。",
                          [notify.link_button("連結到預約網站", url)])
        if name == "unlink":
            acct = _account_by_discord(discord_id)
            if not acct:
                return _reply("這個 Discord 帳號沒有連結任何預約網站帳號。")
            set_discord(acct, "")
            log("info", "已用 /unlink 取消 Discord 連結", acct.name)
            return _reply(f"已取消和 {acct.name} 的連結，不會再收到通知。")
        return _reply("不認得這個指令。")
    if kind == 3:  # a button
        parts = str((data.get("data") or {}).get("custom_id", "")).split(":")
        if len(parts) == 3 and parts[0] == "prep":
            _, decision, prep_id = parts
            prep = booker.get(prep_id)
            if not prep or _discord_id(prep.acct) != discord_id:
                return _reply("找不到這筆預訂（可能已過期，或伺服器重新啟動過）。")
            if not booker.decide(prep.id, decision):
                return _reply(f"這筆現在是「{prep.message}」，不能再{'送出' if decision == 'confirm' else '取消'}。")
            log("info", f"預訂 {prep.label}：在 Discord 按了{'確認送出' if decision == 'confirm' else '取消'}", prep.acct.name)
            # Replace the message's buttons, so nobody presses them twice; the result comes as a new DM.
            text = f"🏸 {prep.label}\n" + ("已按「確認送出」，送出中…" if decision == "confirm" else "已取消，沒有送出。")
            return {"type": 7, "data": {"content": text, "components": []}}
        return _reply("這個按鈕已經失效。")
    return _reply("不支援的操作。")


# ---------- search & history (shared results) ----------

_search_job = {"running": False, "venues": [], "venue": None, "step": 0, "done": 0, "total": 0, "by": None}
_search_results: dict[str, dict] = {}  # latest per-venue result, from a manual search or a robot round
_history_job = {"running": False, "venue": None, "done": 0, "total": 0}

ROBOT = robot.Robot(log, VENUE_NAMES, VENUE_IDS_ORDERED, on_result=_search_results.__setitem__,
                    on_new_hits=_on_new_hits, on_error=_on_robot_error)


def _sync_search_from_cache() -> None:
    """Rebuild results for venues whose cache (from a search or a robot scan) is newer than what we hold."""
    for venue in VENUE_IDS_ORDERED:
        cached = scanner.cache_get(venue, 24 * 3600)
        if cached and (venue not in _search_results or cached[1].isoformat(timespec="seconds") > _search_results[venue]["generated"]):
            _search_results[venue] = scanner.summarize(venue, cached[0], cached[1], from_cache=True)


def _shown_results() -> dict[str, dict]:
    return {**scanner.partial_results(), **_search_results}


def _run_search(acct: session.Account, venues: list[str], force: bool) -> None:
    def progress(i, n):
        _search_job.update(done=i, total=n)

    try:
        for step, venue in enumerate(venues, start=1):
            _search_job.update(venue=venue, step=step, done=0, total=0)
            max_age = 0 if force else scanner.CACHE_MAX_AGE_S
            result = acct.with_relogin(lambda: scanner.search(acct, venue, progress, max_age_s=max_age))
            _search_results[venue] = result
    except session.NotLoggedIn as e:
        log("error", f"搜尋失敗：{acct.login_error or e}", acct.name)
    except session.Preempted:
        log("warn", "搜尋被預訂中斷，預訂完成後請再搜尋一次", acct.name)
    except Exception as e:
        log("error", f"搜尋失敗：{_err(e)}", acct.name)
    finally:
        _search_job.update(running=False)


def _run_history(acct: session.Account, venue: str, since) -> None:
    def progress(i, n):
        _history_job.update(done=i, total=n)

    try:
        acct.with_relogin(lambda: history.update(acct, venue, since, progress))
        log("info", f"歷史資料已更新：{venue} 自 {since.isoformat()}", acct.name)
    except session.Preempted:
        log("warn", "歷史資料更新被預訂中斷，請再按一次「更新歷史資料」", acct.name)
    except Exception as e:
        log("error", f"歷史資料更新失敗：{acct.login_error or _err(e)}", acct.name)
    finally:
        _history_job.update(running=False)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # --- helpers ---

    def _public(self) -> tuple[str, str] | None:
        """(origin, host) of the configured public URL, e.g. ("https://x.example.com", "x.example.com")."""
        pub = config.load()["public_url"]
        if not pub:
            return None
        u = urlparse(pub)
        return f"{u.scheme}://{u.netloc}", u.netloc

    def _via_public(self) -> bool:
        pub = self._public()
        return bool(pub) and self.headers.get("Host") == pub[1]

    def _trusted(self, write: bool) -> bool:
        """Only requests addressed to this app (its public host, or 127.0.0.1 for testing). Writes must also
        come from its own pages (Origin), so another site can't drive the API with someone's cookie."""
        local_hosts = {o.removeprefix("http://") for o in LOCAL_ORIGINS}
        pub = self._public()
        host = self.headers.get("Host")
        if host not in local_hosts and not (pub and host == pub[1]):
            return False
        if not write:
            return True
        origin = self.headers.get("Origin")
        return origin in LOCAL_ORIGINS or bool(pub and origin == pub[0])

    def _client_ip(self) -> str:
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def _token(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c[COOKIE].value if COOKIE in c else None

    def _account(self) -> session.Account | None:
        name = web_session_account(self._token())
        return session.account(name) if name else None

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200, headers=None):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", headers)

    def _error(self, status: int, msg: str = ""):
        self._json({"error": msg or {401: "請先登入", 403: "不允許", 404: "找不到"}.get(status, "錯誤")}, status)

    def _raw(self) -> bytes:
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        return self.rfile.read(length) if length else b""

    @staticmethod
    def _parse(raw: bytes) -> dict:
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _interaction(self, raw: bytes):
        """Discord's interactions: no cookie and no Origin; the Ed25519 signature is the proof instead."""
        if not notify.verify(self.headers.get("X-Signature-Ed25519", ""), self.headers.get("X-Signature-Timestamp", ""), raw):
            return self._send(401, b"invalid request signature", "text/plain")
        return self._json(handle_interaction(self._parse(raw)))

    def _cookie_header(self, token: str, max_age: int) -> dict:
        secure = "; Secure" if self._via_public() and config.load()["public_url"].startswith("https://") else ""
        return {"Set-Cookie": f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"}

    def _static(self, path: str):
        target = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._error(404)
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
                 ".webmanifest": "application/manifest+json", ".png": "image/png"}.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), f"{ctype}; charset=utf-8")

    # --- dispatch ---

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        path = self.path.split("?")[0]
        raw = self._raw() if method != "GET" else b""  # read it first: the connection is kept alive
        if (method, path) == ("POST", "/discord/interactions"):
            try:
                return self._interaction(raw)
            except Exception as e:
                log("error", f"Discord 互動處理失敗：{_err(e)}")
                return self._json(_reply("伺服器錯誤，請稍後再試。"))
        if not self._trusted(write=method != "GET"):
            self.close_connection = True
            return self._error(403)
        try:
            body = self._parse(raw)
            if not path.startswith("/api/"):
                return self._static(path) if method == "GET" else self._error(404)
            if (method, path) == ("POST", "/api/login"):
                return self._login(body)
            acct = self._account()
            if acct is None:
                return self._error(401)
            return getattr(self, f"_{method.lower()}")(acct, path, body)
        except BrokenPipeError:
            pass
        except Exception as e:  # never show a traceback page
            log("error", f"{method} {path}：{_err(e)}")
            try:
                self._error(500, "伺服器錯誤")
            except Exception:
                pass

    # --- login ---

    def _login(self, body: dict):
        name = config.normalize_account(str(body.get("account", "")))
        password = str(body.get("password") or "")
        if not name or not password:
            return self._error(400, "請輸入計中帳號和密碼")
        ip = self._client_ip()
        if _too_many_fails(f"a:{name}", f"i:{ip}"):
            return self._error(429, "失敗次數太多，請 15 分鐘後再試")
        if not config.is_allowed(name):
            _add_fail(f"i:{ip}")
            log("warn", f"不在允許名單的帳號嘗試登入：{name}（{ip}）")
            return self._error(403, "這個帳號不在允許名單裡，請找管理者加入")
        acct = session.account(name)
        acct.dir.mkdir(parents=True, exist_ok=True)
        try:
            acct.sso_login(password)
        except Exception as e:
            _add_fail(f"a:{name}", f"i:{ip}")
            log("warn", f"登入失敗：{_err(e)}", name)
            return self._error(401, "計中帳號登入沒有成功，請確認帳號密碼" if isinstance(e, session.NotLoggedIn)
                               else f"無法連到學校網站（{_err(e)}）")
        credentials.save(name, password)
        token = new_web_session(name)
        log("info", "已登入", name)
        start_orders(acct)
        if name == config.scan_account():
            ROBOT.logged_in()
        return self._json({"ok": True, "account": name}, headers=self._cookie_header(token, WEB_SESSION_DAYS * 86400))

    # --- GET ---

    def _get(self, acct: session.Account, path: str, body: dict):
        admin = config.is_admin(acct.name)
        if path == "/api/me":
            cfg = robot.load_config(acct)
            gcfg = config.load()
            return self._json({
                "account": acct.name, "admin": admin, "discord_id": cfg.get("discord_id", ""),
                "discord_ready": bool(gcfg["discord_token"]), "school_login": acct.has_session(),
                "login_error": acct.login_error, "has_password": credentials.load(acct.name) is not None,
                "members": cfg.get("members", robot.DEFAULT_MEMBERS), "member_types": booker.MEMBER_TYPES,
                "public_url": gcfg["public_url"],
                **({"allowed": gcfg["allowed"], "admins": gcfg["admins"], "scan_account": config.scan_account()} if admin else {}),
            })
        if path == "/api/venues":
            return self._json(VENUES)
        if path == "/api/logs":
            with _lock:
                return self._json([e for e in _logs if e["account"] == acct.name or (admin and e["account"] is None)])
        if path == "/api/robot":
            cfg = robot.load_config(acct)
            all_hits = sorted(robot.load_hits(acct).values(), key=lambda h: (h["date"], h["hour"], h["venue"]))
            today = date.today()
            return self._json({
                "state": ROBOT.snapshot(), "interval_min": config.load()["interval_min"], "filters": cfg["filters"],
                "hits": [h for h in all_hits if h["status"] != "dismissed"],
                "dismissed": [h for h in all_hits if h["status"] == "dismissed"],
                "hours": scanner.HOURS, "min_interval": robot.MIN_INTERVAL_MIN, "can_set_interval": admin,
                "online_dates": [(today + timedelta(days=n)).isoformat()
                                 for n in range(scanner.BOOK_WINDOW[0], scanner.BOOK_WINDOW[1] + 1)],
                "members": cfg.get("members", robot.DEFAULT_MEMBERS), "member_types": booker.MEMBER_TYPES,
            })
        if path == "/api/preps":
            return self._json([p.public() for p in booker.for_account(acct)])
        if path.startswith("/api/preps/") and path.endswith("/shot"):
            prep = booker.get(path.split("/")[3])
            if not prep or prep.acct is not acct or not prep.screenshot:
                return self._error(404)
            return self._send(200, prep.screenshot, "image/jpeg")
        if path.startswith("/api/preps/"):
            prep = booker.get(path.split("/")[3])
            if not prep or prep.acct is not acct:
                return self._error(404, "找不到這筆預訂（可能已過期或伺服器重新啟動過）")
            return self._json(prep.public())
        if path == "/api/history":
            return self._json({"stats": history.load_all(), "job": _history_job, "can_update": admin})
        if path == "/api/orders":
            st = orders_state(acct)
            data = st["data"]
            if data:
                data = {**data, "orders": [{**o, "pay_url": session.BASE + orders.pay_path(o["id"]),
                                            "detail_url": session.BASE + orders.detail_path(o["id"])} for o in data["orders"]]}
            return self._json({**st, "data": data})
        if path == "/api/search":
            if not _search_job["running"]:
                _sync_search_from_cache()
            return self._json({"results": _shown_results(), "job": _search_job,
                               "cache_minutes": scanner.CACHE_MAX_AGE_S // 60})
        return self._error(404)

    # --- POST ---

    def _post(self, acct: session.Account, path: str, body: dict):
        admin = config.is_admin(acct.name)
        if path == "/api/logout":
            end_web_sessions(token=self._token())
            return self._json({"ok": True}, headers=self._cookie_header("", 0))
        if path == "/api/me/discord":
            did = str(body.get("discord_id") or "").strip()
            if did and not (did.isdigit() and 15 <= len(did) <= 21):
                return self._error(400, "Discord 使用者 ID 是一串 17–20 位數字")
            set_discord(acct, did)
            return self._json({"ok": True})
        if path == "/api/me/discord/link":  # the page opened from /link's button
            entry = _link_codes.pop(str(body.get("code") or ""), None)
            if not entry or entry[1] < time.monotonic():
                return self._error(400, "這個連結已經用過或過期了，請在 Discord 再打一次 /link")
            set_discord(acct, entry[0])
            log("info", "已用 /link 連結 Discord", acct.name)
            notify.send(entry[0], f"✅ 已連結到 {acct.name}，之後的空位和預訂通知會私訊到這裡。")
            return self._json({"ok": True})
        if path == "/api/me/discord/test":
            try:
                notify.send_now(_discord_id(acct), f"👋 {acct.name}，這是場地預約的測試訊息", ("打開網站", notify.url()))
            except Exception as e:
                return self._error(400, f"送不出去：{e}（bot 和你要在同一個 Discord 伺服器）")
            return self._json({"ok": True})
        if path == "/api/me/members":
            try:
                members = {t: max(0, min(10, int((body.get("members") or {}).get(t) or 0))) for t in booker.MEMBER_TYPES}
            except (TypeError, ValueError, AttributeError):
                return self._error(400, "人數格式錯誤")
            if not sum(members.values()):
                return self._error(400, "人數至少要 1 人")
            cfg = robot.load_config(acct)
            cfg["members"] = members
            robot.save_config(acct, cfg)
            return self._json({"ok": True})
        if path == "/api/robot/settings":
            if not admin:
                return self._error(403, "只有管理者能改掃描間隔")
            try:
                gcfg = config.load()
                gcfg["interval_min"] = max(robot.MIN_INTERVAL_MIN, min(60, int(body["interval_min"])))
                config.save(gcfg)
            except (KeyError, TypeError, ValueError):
                return self._error(400, "設定格式錯誤")
            return self._json({"ok": True})
        if path == "/api/robot/filters" or path.startswith("/api/robot/filters/"):
            try:
                cleaned = robot.clean_filter(body, VENUE_IDS_ORDERED)
            except (TypeError, ValueError):
                return self._error(400, "濾網格式錯誤")
            cfg = robot.load_config(acct)
            if path == "/api/robot/filters":
                if len(cfg["filters"]) >= 20:
                    return self._error(400, "濾網最多 20 個")
                f = {"id": robot._new_id(), **cleaned}
                cfg["filters"].append(f)
            else:
                f = next((f for f in cfg["filters"] if f["id"] == path.rsplit("/", 1)[1]), None)
                if f is None:
                    return self._error(404, "找不到濾網")
                f.update(cleaned)
            robot.save_config(acct, cfg)
            ROBOT.reschedule()
            return self._json(f)
        if path == "/api/robot/scan":
            if not admin:
                return self._error(403, "只有管理者能手動掃描")
            ROBOT.scan_now()
            return self._json({"ok": True})
        if path.startswith("/api/robot/hits/"):
            parts = path.split("/")
            if len(parts) != 6:
                return self._error(404)
            hit_id, action = parts[4], parts[5]
            hits = robot.load_hits(acct)
            hit = robot.find_hit(hits, hit_id)
            if hit is None:
                return self._error(404, "這筆已經不在了")
            if action == "book":
                if hit["status"] != "available":
                    return self._error(404, "這筆已經不能訂了")
                return self._book(acct, hit["venue"], hit["date"], hit["hour"],
                                  robot.clamp_want(hit.get("want"), hit.get("count", 1)))
            if action == "want":
                try:
                    want = int(body.get("want"))
                except (TypeError, ValueError):
                    return self._error(400, "數量格式錯誤")
                if not 1 <= want <= hit.get("count", 1):
                    return self._error(400, f"數量需介於 1 到 {hit.get('count', 1)} 面")
                hit["want"] = want
            elif action == "dismiss":
                hit["status"] = "dismissed"
            elif action == "restore":
                if hit["status"] != "dismissed":
                    return self._error(404, "這筆已經不在了")
                hit["status"] = "available"
            else:
                return self._error(404)
            robot.save_hits(acct, hits)
            return self._json({"ok": True, "want": hit.get("want")})
        if path == "/api/book":  # straight from the search table
            venue = body.get("venue")
            if venue not in VENUE_IDS:
                return self._error(400, "請選擇場地")
            try:
                day, hour = date.fromisoformat(body["date"]), int(body["hour"])
                quantity = int(body.get("quantity") or 1)
            except (KeyError, TypeError, ValueError):
                return self._error(400, "預訂資料格式錯誤")
            today = date.today()
            if not (today + timedelta(days=scanner.BOOK_WINDOW[0]) <= day <= today + timedelta(days=scanner.BOOK_WINDOW[1])):
                return self._error(400, "只能線上預訂租借日前 3–7 天的時段")
            cell = (_shown_results().get(venue) or {}).get("cells", {}).get(f"{day.isoformat()}|{hour}")
            if not cell or not cell["open"]:
                return self._error(404, "這個時段目前沒有線上空場，請重新搜尋")
            if not 1 <= quantity <= cell["open"]:
                return self._error(400, f"面數需介於 1 到 {cell['open']} 面")
            return self._book(acct, venue, day.isoformat(), hour, quantity)
        if path.startswith("/api/preps/") and path.rsplit("/", 1)[1] in ("confirm", "cancel"):
            prep = booker.get(path.split("/")[3])
            if not prep or prep.acct is not acct:
                return self._error(404, "找不到這筆預訂")
            decision = path.rsplit("/", 1)[1]
            if not booker.decide(prep.id, decision):
                return self._error(409, "這筆現在不能" + ("送出" if decision == "confirm" else "取消"))
            log("info", f"預訂 {prep.label}：{'按下確認送出' if decision == 'confirm' else '取消'}", acct.name)
            return self._json({"ok": True})
        if path == "/api/search":
            if _search_job["running"]:
                return self._error(409, "有人正在搜尋，結果大家共用，請稍等")
            venues = [v for v in VENUE_IDS_ORDERED if v in (body.get("venues") or [])]
            if not venues:
                return self._error(400, "請至少選一個場地")
            force = bool(body.get("force"))
            _search_job.update(running=True, venues=venues, venue=venues[0], step=0, done=0, total=0, by=acct.name)
            threading.Thread(target=_run_search, args=(acct, venues, force), daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/history":
            if not admin:
                return self._error(403, "只有管理者能更新歷史資料")
            if _history_job["running"]:
                return self._error(409, "歷史資料正在更新中")
            venue = body.get("venue")
            if venue not in VENUE_IDS:
                return self._error(400, "請選擇場地")
            try:
                since = datetime.strptime(body.get("since", ""), "%Y-%m-%d").date()
            except ValueError:
                return self._error(400, "起始日期格式錯誤")
            if not (date.today() - timedelta(days=200) <= since < date.today()):
                return self._error(400, "起始日期需在今天之前、200 天以內")
            _history_job.update(running=True, venue=venue, done=0, total=0)
            threading.Thread(target=_run_history, args=(acct, venue, since), daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/orders/refresh":
            start_orders(acct)
            return self._json({"ok": True})
        if path == "/api/admin/allow" and admin:
            name = config.normalize_account(str(body.get("account", "")))
            if not name:
                return self._error(400, "帳號格式錯誤")
            gcfg = config.load()
            if body.get("allow", True):
                gcfg["allowed"] = sorted(set(gcfg["allowed"]) | {name})
            else:
                if name in gcfg["admins"]:
                    return self._error(400, "不能移除管理者")
                gcfg["allowed"] = [a for a in gcfg["allowed"] if a != name]
                end_web_sessions(account=name)
            config.save(gcfg)
            log("info", f"允許名單{'加入' if body.get('allow', True) else '移除'} {name}", acct.name)
            return self._json({"ok": True, "allowed": gcfg["allowed"]})
        return self._error(404)

    def _book(self, acct: session.Account, venue: str, day: str, hour: int, quantity: int):
        current = booker.busy(acct)
        if current:
            return self._json({"error": "你已經有一筆預訂在進行中", "prep": current.id}, 409)
        if not credentials.load(acct.name):
            return self._error(400, "伺服器上沒有你的密碼，請登出再登入")
        prep = start_prep(acct, venue, day, hour, quantity)
        return self._json({"ok": True, "prep": prep.id})

    # --- DELETE ---

    def _delete(self, acct: session.Account, path: str, body: dict):
        if path.startswith("/api/robot/filters/"):
            cfg = robot.load_config(acct)
            keep = [f for f in cfg["filters"] if f["id"] != path.rsplit("/", 1)[1]]
            if len(keep) == len(cfg["filters"]):
                return self._error(404, "找不到濾網")
            cfg["filters"] = keep
            robot.save_config(acct, cfg)
            ROBOT.reschedule()
            return self._json({"ok": True})
        if path == "/api/robot/hits":
            robot.save_hits(acct, {k: h for k, h in robot.load_hits(acct).items() if h["status"] != "gone"})
            return self._json({"ok": True})
        if path == "/api/me":
            # Forget everything about this person: stored password, school login, filters, hits, orders.
            if booker.busy(acct):
                return self._error(409, "有預訂進行中，請先取消")
            end_web_sessions(account=acct.name)
            shutil.rmtree(acct.dir, ignore_errors=True)
            _orders.pop(acct.name, None)
            log("info", f"{acct.name} 刪除了自己在伺服器上的所有資料")
            ROBOT.reschedule()
            return self._json({"ok": True}, headers=self._cookie_header("", 0))
        return self._error(404)


def main() -> None:
    cfg = config.load()
    if not cfg["admins"]:
        print("還沒有管理者：先執行  python manage.py set-admin <你的計中帳號>")
    notify.start(log)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log("info", f"伺服器已啟動：http://{HOST}:{PORT}  公開網址：{cfg['public_url'] or '（未設定）'}")
    threading.Thread(target=_orders_loop, daemon=True, name="orders").start()
    ROBOT.reschedule()  # start scanning right away if any filter is enabled
    for name in config.users():  # a login saved hours ago has probably expired; find out now
        threading.Thread(target=session.account(name).check, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
