"""Web app for NTU venue booking, for a few people, opened from their phones.

Listens on 127.0.0.1 only; the outside world reaches it through a Cloudflare Tunnel (see deploy/).
People log in with Discord (/login to the bot, for anybody in the bot's Discord server) or with a 計中
account on the allow list. A Discord-login account gets vacancy alerts and books on the school's site
itself; once it links a 計中 account (checked by really logging in through 計中 SSO), the server can
prepare bookings for it too. The 計中 password is kept encrypted, unless the person chose not to keep it,
so the server can log in again on its own when the school's login expires.

Run:  python server.py        (PORT, PUBLIC_URL and NTU_BOOKING_DATA can be set in the environment)
"""
import hashlib
import json
import os
import secrets
import shutil
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
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
    # "short" is what the phone page shows (grouped under the sport), "name" the school's own name
    {"id": "A:2", "name": "多功能球場", "short": "多功能", "sport": "羽球"},
    {"id": "A:86", "name": "三樓主球場", "short": "三樓", "sport": "羽球"},
    {"id": "A:4", "name": "桌球室", "short": "桌球室", "sport": "桌球"},
    {"id": "A:97", "name": "教室 B109", "short": "B109", "sport": "桌球"},
    {"id": "A:11", "name": "壁球室", "short": "壁球室", "sport": "壁球"},
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
    now = datetime.now()
    with _lock:  # printing inside the lock too, so lines from different threads don't run into each other
        _logs.append({"time": now.strftime("%Y-%m-%d %H:%M:%S"), "level": level, "msg": msg, "account": account})
        del _logs[:-1000]
        try:  # the console may not take every character (cp950 on a Windows console); logging must never throw
            print(f"{now:%m-%d %H:%M:%S} [{level}] {account or '-'}: {msg}", flush=True)
        except (UnicodeError, OSError):
            pass


def _err(e: Exception) -> str:
    return f"{e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}"


def can_book(acct: session.Account) -> bool:
    """The server can use this person's school login: a linked 計中 account with a stored password, or
    (password not kept) a school login that hasn't expired yet."""
    return bool(acct.school) and (credentials.load(acct.name) is not None or acct.has_session())


def school_url(venue: str) -> str:
    """The school's own order page of a venue, for booking there by hand."""
    return f"{session.BASE}/order/?Add={venue}"


def display_name(acct: session.Account) -> str:
    return robot.load_config(acct).get("display") or acct.school or acct.name


WEEKDAYS = "一二三四五六日"


def day_label(day: str) -> str:
    """'2026-11-18' -> '11/18（三）', the way people read it in a message."""
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        return day
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）{day[10:]}"


def slot_label(venue: str, day: str, hour: int) -> str:
    return f"{VENUE_NAMES.get(venue, venue)} {day_label(day)} {hour:02d}:00–{hour + 1:02d}:00"


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


REMIND_DAY_OF_HOUR = 8  # the deadline-day reminder waits until morning


def order_label(o: dict) -> str:
    when = "、".join(f"{day_label(s['date'])} {s['start']}–{s['end']}" for s in o["slots"]) or "—"
    return f"{o['venue']} {when}"


def _deadline_reminders(acct: session.Account, data: dict) -> None:
    """Unpaid orders: DM the day before the payment deadline and again on the day (from 08:00), each with
    「線上付款」 and 「取消訂單」, so every order ends up paid or cancelled rather than lapsing."""
    cfg = robot.load_config(acct)
    told = set(cfg.get("deadline_told", []))  # "<order id>:d1" / ":d0"; a bare id is a d1 from before
    today, now = date.today(), datetime.now()
    sent = []
    for o in data["orders"]:
        if o["kind"] != "unpaid" or not o["deadline"] or not any(s["date"] >= today.isoformat() for s in o["slots"]):
            continue
        deadline = date.fromisoformat(o["deadline"][:10])
        if deadline == today + timedelta(days=1):
            stage, when = "d1", "明天"
        elif deadline == today and now.hour >= REMIND_DAY_OF_HOUR:
            stage, when = "d0", "今天"
        else:
            continue
        if f"{o['id']}:{stage}" in told or (stage == "d1" and o["id"] in told):
            continue
        buttons = []
        if o.get("can_pay", True):
            buttons.append(notify.link_button("線上付款", session.BASE + orders.pay_path(o["id"])))
        if o.get("can_cancel", True):
            buttons.append(notify.button("取消訂單", f"order:cancel:{o['id']}", 4))
        notify.send(_discord_id(acct), f"⏰ {when}是繳費期限：{order_label(o)}，金額 {o['amount']}\n"
                                      "要去就繳費；不去的話請取消，別讓它逾期。",
                    ("打開訂單", notify.url("#orders")), buttons=buttons)
        sent.append(f"{o['id']}:{stage}")
    if sent:
        cfg["deadline_told"] = (sorted(told) + sent)[-100:]
        robot.save_config(acct, cfg)


def cancel_for(acct: session.Account, order_id: str) -> tuple[bool | None, str]:
    """Cancel one of acct's orders on the school site and check the result. (ok, message for the person);
    ok is None when it can't be told whether the cancel went through."""
    st = orders_state(acct)
    o = next((x for x in ((st["data"] or {}).get("orders") or []) if x["id"] == order_id), None)
    if o is None:
        return False, "找不到這筆訂單，請重新整理訂單"
    label = order_label(o)
    if o["kind"] == "cancelled":
        return True, f"這筆已經是取消狀態：{label}"
    if not o.get("can_cancel", True):
        return False, f"學校網站沒有提供這筆的取消功能，請到學校網站處理：{label}"
    try:
        cancelled, data = acct.with_relogin(lambda: orders.cancel_order(acct, order_id))
    except session.NotLoggedIn as e:
        return False, f"沒有取消：{acct.login_error or e}"
    except Exception as e:  # offline, page changed, ...
        log("error", f"取消訂單失敗：{_err(e)}", acct.name)
        return None, f"取消時發生錯誤（{_err(e)}），請到學校網站確認這筆的狀態：{label}"
    st.update(data=data, error=None)
    if cancelled:
        log("info", f"已取消訂單：{label}", acct.name)
        return True, f"已取消：{label}"
    if cancelled is None:
        log("warn", f"取消後訂單列表裡找不到這筆：{label}", acct.name)
        return None, f"送出取消後，訂單列表裡找不到這筆了，請到學校網站確認：{label}"
    log("warn", f"取消沒有生效：{label}", acct.name)
    return False, f"學校網站沒有把這筆改成已取消，請到學校網站自己取消：{label}"


def _cancel_and_report(acct: session.Account, order_id: str, app_id: str, token: str) -> None:
    ok, msg = cancel_for(acct, order_id)
    try:
        notify.edit_original(app_id, token, ("✅ " if ok else "⚠️ ") + msg)
    except Exception as e:
        log("warn", f"Discord 回覆取消結果失敗：{_err(e)}", acct.name)
        notify.send(_discord_id(acct), ("✅ " if ok else "⚠️ ") + msg)


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
        _record_auto(prep)
        saved = orders.load_saved(acct)  # just read by the booking: lets the cancel button find the order
        if saved:
            orders_state(acct).update(data=saved, error=None)
        buttons = []
        for oid in prep.order_ids[-2:]:
            buttons += [notify.link_button("線上付款", session.BASE + orders.pay_path(oid)),
                        notify.button("取消訂單", f"order:cancel:{oid}", 4)]
        head = "🤖 自動訂到了" if prep.auto and not prep.by_tap else "✅"
        notify.send(did, f"{head} {prep.label}\n{prep.message}", ("打開訂單", notify.url("#orders")), buttons=buttons)
        start_orders(acct)
    elif prep.status == "expired" and not booker.slot_passed(prep):
        notify.send(did, f"⏸️ {prep.label}\n{prep.message}", ("打開預訂", notify.url(f"#prep/{prep.id}")),
                    buttons=[notify.button("確認送出", f"prep:confirm:{prep.id}", 3),
                             notify.button("不要了", f"prep:cancel:{prep.id}", 4)])
    elif prep.status in ("failed", "expired"):
        _record_auto(prep)
        notify.send(did, f"⚠️ {prep.label}\n{prep.message}")
        if prep.status == "failed" and "沒有可訂" in prep.message:
            robot.mark_gone(acct, prep.venue, prep.day, prep.hour)
        if prep.booked:
            start_orders(acct)


def start_prep(acct: session.Account, venue: str, day: str, hour: int, quantity: int,
               auto: bool = False, by_tap: bool = False) -> booker.Prep:
    members = robot.load_config(acct).get("members", robot.DEFAULT_MEMBERS)
    return booker.prepare(acct, venue, slot_label(venue, day, hour), day, hour, quantity, members, _on_prep_change,
                          auto=auto, by_tap=by_tap)


AUTO_DAILY_LIMIT = 3  # courts a day the robot may book without asking, per person
_auto_lock = threading.Lock()


def _record_auto(prep: booker.Prep) -> None:
    """Count the courts an auto prep booked against the day's limit (once, when it finishes)."""
    with _auto_lock:
        if not prep.auto or prep.by_tap or getattr(prep, "_counted", False):
            return
        if prep.booked:
            cfg = robot.load_config(prep.acct)
            log_ = [e for e in cfg.get("auto_booked", []) if e["date"] >= (date.today() - timedelta(days=7)).isoformat()]
            log_.append({"date": date.today().isoformat(), "courts": len(prep.booked), "label": prep.label})
            cfg["auto_booked"] = log_
            robot.save_config(prep.acct, cfg)
        prep._counted = True  # only once it is in the log, so auto_left never sees it in neither place


def auto_left(acct: session.Account) -> int:
    """Courts the robot may still book on its own today: the limit minus what was booked today and what a
    running auto booking may still book."""
    today = date.today().isoformat()
    done = sum(e["courts"] for e in robot.load_config(acct).get("auto_booked", []) if e["date"] == today)
    # auto preps not yet written to the log: a running one may still book all it asked for
    running = sum(len(p.booked) if p.finished else p.quantity for p in booker.for_account(acct)
                  if p.auto and not p.by_tap and not getattr(p, "_counted", False)
                  and p.created.date().isoformat() == today)
    return max(0, AUTO_DAILY_LIMIT - done - running)


_MODE_RANK = {"notify": 0, "auto": 1}


def _on_new_hits(acct: session.Account, found: list[tuple[dict, list[dict]]]) -> None:
    """The robot found slots for this person. Depending on the filters' mode, the first slot is sent at once
    (auto, within the daily limit) or filled up to the confirmation page (prepare); every other slot gets a
    預訂 button. All of it goes out as one message, grouped by venue and day."""
    did = _discord_id(acct)
    bookable = can_book(acct)
    groups: dict[tuple, list[str]] = {}
    started, book_buttons, full_noted = None, [], False
    venues = list(dict.fromkeys(hit["venue"] for hit, _ in found))
    for hit, filters in found:
        label = slot_label(hit["venue"], hit["date"], hit["hour"])
        names = "、".join(f["name"] for f in filters)
        log("warn", f"機器人找到：［{names}］{label}（{hit['count']} 面）", acct.name)
        line = f"{hit['hour']:02d}:00–{hit['hour'] + 1:02d}:00（{hit['count']} 面）"
        mode = max((robot.filter_mode(f) for f in filters), key=_MODE_RANK.get, default="notify")
        quantity = robot.clamp_want(hit.get("want"), hit.get("count", 1))
        if started is None and mode == "auto" and bookable and not booker.busy(acct):
            left = auto_left(acct)
            if left:
                start_prep(acct, hit["venue"], hit["date"], hit["hour"], min(quantity, left), auto=True)
                line += " ← 🤖 自動預訂中…"
                started = hit
            elif not full_noted:
                full_noted = True
                line += f"（今天自動預訂已滿 {AUTO_DAILY_LIMIT} 面，要的話按「預訂」）"
        if hit is not started and bookable:
            where = f"{VENUE_NAMES.get(hit['venue'], hit['venue'])} " if len(venues) > 1 else ""
            book_buttons.append(notify.button(f"預訂 {where}{day_label(hit['date'])} {hit['hour']:02d}:00",
                                              f"hit:book:{hit['id']}", 1))
        groups.setdefault((hit["venue"], hit["date"][:10]), []).append(line)
    if not groups:
        return
    parts = [f"🔎 找到 {len(found)} 個空位：" if len(found) > 1 else "🔎 找到空位："]
    for (venue, day), lines in groups.items():
        parts.append(f"**{VENUE_NAMES.get(venue, venue)} {day_label(day)}**")
        parts += [f"• {x}" for x in lines]
    school = [notify.link_button("到學校網站預訂" if len(venues) == 1 else f"學校網站：{VENUE_NAMES.get(v, v)}",
                                 school_url(v)) for v in venues[:4]]
    room = 25 - len(school) - 1  # the 打開清單 link comes last
    if len(book_buttons) > room:
        parts.append(f"（還有 {len(book_buttons) - room} 個時段的預訂按鈕放不下，請打開清單）")
    first = started or found[0][0]
    notify.send(did, "\n".join(parts), ("打開清單", notify.url(f"#hit/{first['id']}")),
                buttons=book_buttons[:room] + school)


def _on_robot_error(msg: str) -> None:
    if any(w in msg for w in ("No route to host", "timed out", "Name or service not known", "Connection refused")):
        msg = f"連不到學校網站（{msg}），恢復後會再通知你"
    for name in config.load()["admins"]:
        notify.send(_discord_id(session.account(name)), f"🤖 機器人掃描失敗：{msg}")


def _on_robot_recover(since: datetime) -> None:
    """Scans work again after the admins were told they failed."""
    minutes = max(1, round((datetime.now() - since).total_seconds() / 60))
    span = f"{minutes} 分鐘" if minutes < 120 else f"{round(minutes / 60)} 小時"
    log("info", f"機器人掃描恢復正常（中斷約 {span}）")
    for name in config.load()["admins"]:
        notify.send(_discord_id(session.account(name)), f"✅ 機器人掃描恢復正常（中斷約 {span}）")


# ---------- Discord interactions (/link, /unlink, the Confirm / Cancel buttons) ----------

LINK_CODE_S = 10 * 60
_link_codes: dict[str, tuple[str, float]] = {}  # one-time code from /link -> (discord user id, expiry)
_login_codes: dict[str, tuple[str, str, float]] = {}  # from /login -> (discord user id, name, expiry)
_oauth_states: dict[str, tuple[str, float]] = {}  # 「用 Discord 登入」 -> ("", expiry), used once
GUEST_PATHS = ("/api/venues", "/api/history", "/api/search")


def oauth_redirect() -> str:
    """Where Discord sends people back after 「用 Discord 登入」: the fixed address page (REDIRECT_URL from
    deploy/env, or "discord_redirect" in config.json), which forwards to the current tunnel. It must also
    be listed under OAuth2 → Redirects in the Discord Developer Portal."""
    return (config.load().get("discord_redirect") or os.environ.get("REDIRECT_URL", "")).strip()


def discord_user(access_token: str) -> dict:
    """The Discord user an OAuth access token belongs to (scope identify)."""
    req = urllib.request.Request(notify.API + "/users/@me", headers={
        "Authorization": f"Bearer {access_token}", "User-Agent": notify.USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read())


def _new_code(codes: dict, *value) -> str:
    now = time.monotonic()
    for c in [c for c, v in codes.items() if v[-1] < now]:
        del codes[c]
    code = secrets.token_urlsafe(16)
    codes[code] = (*value, now + LINK_CODE_S)
    return code


def _home_guild() -> str | None:
    """The Discord server whose members may log in: the configured one, or the bot's only server."""
    guild = config.load()["discord_guild"]
    if guild:
        return str(guild)
    guilds = notify._call("GET", "/users/@me/guilds")
    return guilds[0]["id"] if len(guilds) == 1 else None


def _is_member(data: dict, discord_id: str) -> bool:
    guild = _home_guild()
    if not guild:
        return False
    if str(data.get("guild_id") or "") == guild and data.get("member"):
        return True
    try:
        notify._call("GET", f"/guilds/{guild}/members/{discord_id}")
        return True
    except OSError:  # 404: not a member
        return False
EPHEMERAL = 64  # only the person who typed the command sees the reply


def _account_by_discord(discord_id: str) -> session.Account | None:
    for name in config.users():
        acct = session.account(name)
        if _discord_id(acct) == discord_id:
            return acct
    return None


def _account_by_school(school: str) -> session.Account | None:
    for name in config.users():
        acct = session.account(name)
        if acct.school == school:
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
        if name == "login":
            if config.discord_account(discord_id) in config.load()["blocked"]:
                return _reply("管理者已停用你的帳號。")
            if not _is_member(data, discord_id):
                return _reply("只有在預約系統的 Discord 伺服器裡的人才能登入，請先請管理者邀你加入。")
            uname = str(user.get("global_name") or user.get("username") or discord_id)
            url = notify.url(f"#login/{_new_code(_login_codes, discord_id, uname)}")
            if not url.startswith("https://"):
                return _reply(f"打開這個網址登入（10 分鐘內有效，只能用一次）：{url}")
            return _reply("點下面的按鈕登入預約網站（10 分鐘內有效，只能用一次，不要轉傳）。",
                          [notify.link_button("登入預約網站", url)])
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
        if len(parts) == 3 and parts[0] == "order":
            _, action, order_id = parts
            acct = _account_by_discord(discord_id)
            o = next((x for x in ((orders_state(acct)["data"] or {}).get("orders") or []) if x["id"] == order_id),
                     None) if acct else None
            if not o:
                return _reply("找不到這筆訂單（只能取消自己的訂單）。")
            if action == "keep":
                return {"type": 7, "data": {"content": "好，沒有取消。", "components": []}}
            if o["kind"] == "cancelled":
                return _reply(f"這筆已經是取消狀態：{order_label(o)}")
            if action == "cancel":  # ask once more, privately
                paid = "\n這筆已經繳費，退費依學校規定處理。" if o["kind"] == "paid" else ""
                return _reply(f"確定要取消 {order_label(o)}？取消後不能復原。{paid}",
                              [notify.button("確定取消", f"order:cancelyes:{order_id}", 4),
                               notify.button("不要取消", f"order:keep:{order_id}", 2)])
            if action == "cancelyes":
                # Cancelling takes a few seconds and Discord wants an answer within 3: answer now, report later.
                threading.Thread(target=_cancel_and_report, daemon=True,
                                 args=(acct, order_id, str(data.get("application_id")), str(data.get("token")))).start()
                log("info", f"在 Discord 按了取消訂單：{order_label(o)}", acct.name)
                return {"type": 7, "data": {"content": f"正在取消 {order_label(o)}…", "components": []}}
        if len(parts) == 3 and parts[0] == "hit" and parts[1] == "book":
            acct = _account_by_discord(discord_id)
            hit = robot.find_hit(robot.load_hits(acct), parts[2]) if acct else None
            if not hit:
                return _reply("找不到這個時段（可能已經過了，或濾網被刪掉了）。")
            label = slot_label(hit["venue"], hit["date"], hit["hour"])
            if hit["status"] != "available":
                return _reply(f"{label} 上次掃描時已經沒有空場了。")
            current = booker.busy(acct)
            if current:
                return _reply(f"你已經有一筆預訂在進行中：{current.label}（{current.message}），完成或取消後再按。")
            if not can_book(acct):
                return _reply("伺服器沒辦法幫你訂：要先在網站「設定」連結計中帳號（或重新登入）。也可以直接到學校網站訂。",
                              [notify.link_button("到學校網站預訂", school_url(hit["venue"]))])
            # Pressing 預訂 is the go-ahead: book it straight away (the daily limit is only for the robot).
            quantity = robot.clamp_want(hit.get("want"), hit.get("count", 1))
            start_prep(acct, hit["venue"], hit["date"], hit["hour"], quantity, auto=True, by_tap=True)
            log("info", f"在 Discord 按了預訂：{label}", acct.name)
            return _reply(f"正在幫你訂 {label}（{quantity} 面），訂好會私訊你繳費和取消的按鈕。")
        if len(parts) == 3 and parts[0] == "prep":
            _, decision, prep_id = parts
            prep = booker.get(prep_id)
            if not prep or _discord_id(prep.acct) != discord_id:
                return _reply("找不到這筆預訂（可能已過期，或伺服器重新啟動過）。")
            was_expired = prep.status == "expired"
            if not booker.decide(prep.id, decision):
                if was_expired and booker.slot_passed(prep):
                    return _reply("這個時段已經過了，不能再訂。")
                return _reply(f"這筆現在是「{prep.message}」，不能再{'送出' if decision == 'confirm' else '取消'}。")
            log("info", f"預訂 {prep.label}：在 Discord 按了{'確認送出' if decision == 'confirm' else '取消'}", prep.acct.name)
            # Replace the message's buttons, so nobody presses them twice; the result comes as a new DM.
            text = f"🏸 {prep.label}\n" + (
                ("已按「確認送出」，重新填寫並送出中…" if was_expired else "已按「確認送出」，送出中…")
                if decision == "confirm" else "已取消，沒有送出。")
            return {"type": 7, "data": {"content": text, "components": []}}
        return _reply("這個按鈕已經失效。")
    return _reply("不支援的操作。")


# ---------- search & history (shared results) ----------

_search_job = {"running": False, "venues": [], "venue": None, "step": 0, "done": 0, "total": 0, "by": None}
_search_results: dict[str, dict] = {}  # latest per-venue result, from a manual search or a robot round
_history_job = {"running": False, "venue": None, "done": 0, "total": 0}

ROBOT = robot.Robot(log, VENUE_NAMES, VENUE_IDS_ORDERED, on_result=_search_results.__setitem__,
                    on_new_hits=_on_new_hits, on_error=_on_robot_error,
                    on_recover=_on_robot_recover)


def _sync_search_from_cache() -> None:
    """Rebuild results for venues whose cache (from a search or a robot scan) is newer than what we hold."""
    for venue in VENUE_IDS_ORDERED:
        cached = scanner.cache_get(venue, 24 * 3600)
        if cached and (venue not in _search_results or cached[1].isoformat(timespec="seconds") > _search_results[venue]["generated"]):
            _search_results[venue] = scanner.summarize(venue, cached[0], cached[1], from_cache=True)


def _shown_results() -> dict[str, dict]:
    return {**scanner.partial_results(), **_search_results}


def _run_search(by: session.Account, venues: list[str], force: bool) -> None:
    def progress(i, n):
        _search_job.update(done=i, total=n)

    # Without a school login of their own, people search through the scan account (results are shared anyway).
    scan = config.scan_account()
    acct = by if can_book(by) or not scan else session.account(scan)
    try:
        for step, venue in enumerate(venues, start=1):
            _search_job.update(venue=venue, step=step, done=0, total=0)
            max_age = 0 if force else scanner.CACHE_MAX_AGE_S
            result = acct.with_relogin(lambda: scanner.search(acct, venue, progress, max_age_s=max_age))
            _search_results[venue] = result
    except session.NotLoggedIn as e:
        log("error", f"搜尋失敗：{acct.login_error or e}", by.name)
    except session.Preempted:
        log("warn", "搜尋被預訂中斷，預訂完成後請再搜尋一次", by.name)
    except Exception as e:
        log("error", f"搜尋失敗：{_err(e)}", by.name)
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
            if (method, path) == ("POST", "/api/login/discord"):
                return self._login_discord(body)
            if (method, path) == ("GET", "/api/login/options"):
                return self._json({"discord_oauth": bool(oauth_redirect() and config.load()["discord_token"])})
            if (method, path) == ("POST", "/api/login/discord/start"):
                return self._oauth_start()
            if (method, path) == ("POST", "/api/login/oauth"):
                return self._oauth_finish(body)
            acct = self._account()
            if acct is None:
                if method == "GET" and path in GUEST_PATHS:
                    return self._guest_get(path)
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
        # A Discord-login account that linked this 計中 account is the one to log in to.
        acct = _account_by_school(name) or session.account(name)
        if not config.is_allowed(acct.name):
            _add_fail(f"i:{ip}")
            log("warn", f"不在允許名單的帳號嘗試登入：{name}（{ip}）")
            return self._error(403, "這個計中帳號不在允許名單裡。請在 Discord 對 bot 打 /login 登入，或找管理者加入名單")
        acct.dir.mkdir(parents=True, exist_ok=True)
        try:
            acct.sso_login(password, school=name)
        except Exception as e:
            _add_fail(f"a:{name}", f"i:{ip}")
            log("warn", f"登入失敗：{_err(e)}", name)
            return self._error(401, "計中帳號登入沒有成功，請確認帳號密碼" if isinstance(e, session.NotLoggedIn)
                               else f"無法連到學校網站（{_err(e)}）")
        credentials.save(acct.name, name, password if body.get("remember", True) else None)
        token = new_web_session(acct.name)
        log("info", "已用計中帳號登入", acct.name)
        start_orders(acct)
        if acct.name == config.scan_account():
            ROBOT.logged_in()
        return self._json({"ok": True, "account": acct.name}, headers=self._cookie_header(token, WEB_SESSION_DAYS * 86400))

    def _login_discord(self, body: dict):
        """The page opened from /login's button: log in as the Discord user who typed /login."""
        entry = _login_codes.pop(str(body.get("code") or ""), None)
        if not entry or entry[2] < time.monotonic():
            return self._error(400, "這個登入連結已經用過或過期了，請在 Discord 再打一次 /login")
        discord_id, uname, _ = entry
        return self._web_login_discord(discord_id, uname)

    def _web_login_discord(self, discord_id: str, uname: str):
        """Log this browser in as a Discord user (already checked: a member of the server, not blocked)."""
        acct = _account_by_discord(discord_id)
        if acct is None:
            name = config.discord_account(discord_id)
            if not config.is_allowed(name):
                return self._error(403, "管理者已停用你的帳號")
            acct = session.account(name)
            acct.dir.mkdir(parents=True, exist_ok=True)
            set_discord(acct, discord_id)
            log("info", f"新的 Discord 帳號：{uname}", name)
        cfg = robot.load_config(acct)
        cfg["display"] = uname
        robot.save_config(acct, cfg)
        token = new_web_session(acct.name)
        log("info", "已用 Discord 登入", acct.name)
        return self._json({"ok": True, "account": acct.name}, headers=self._cookie_header(token, WEB_SESSION_DAYS * 86400))

    def _oauth_start(self):
        """「用 Discord 登入」: the address of Discord's authorize page (implicit grant, scope identify)."""
        redirect = oauth_redirect()
        if not redirect or not config.load()["discord_token"]:
            return self._error(400, "伺服器還沒設定 Discord 登入，請在 Discord 打 /login")
        try:
            app_id = notify.application()["id"]
        except Exception as e:
            return self._error(502, f"連不到 Discord（{_err(e)}）")
        state = _new_code(_oauth_states, "")
        query = urllib.parse.urlencode({"client_id": app_id, "response_type": "token", "scope": "identify",
                                        "redirect_uri": redirect, "state": state, "prompt": "none"})
        return self._json({"url": f"https://discord.com/oauth2/authorize?{query}"})

    def _oauth_finish(self, body: dict):
        """Back from Discord with an access token (through the fixed address page): find out who it is."""
        ip = self._client_ip()
        if _too_many_fails(f"i:{ip}"):
            return self._error(429, "失敗次數太多，請 15 分鐘後再試")
        entry = _oauth_states.pop(str(body.get("state") or ""), None)
        token = str(body.get("access_token") or "")
        if not entry or entry[-1] < time.monotonic() or not token:
            _add_fail(f"i:{ip}")
            return self._error(400, "登入逾時或連結不對，請再按一次「用 Discord 登入」")
        try:
            user = discord_user(token)
        except Exception as e:
            log("warn", f"Discord 登入失敗：{_err(e)}")
            return self._error(502, "Discord 沒有確認你的身分，請再試一次，或在 Discord 打 /login")
        discord_id = str(user.get("id") or "")
        uname = str(user.get("global_name") or user.get("username") or discord_id)
        if not discord_id:
            return self._error(502, "Discord 沒有回傳帳號")
        if config.discord_account(discord_id) in config.load()["blocked"]:
            return self._error(403, "管理者已停用你的帳號")
        if not _is_member({}, discord_id):
            return self._error(403, "只有在預約系統的 Discord 伺服器裡的人才能登入，請先請管理者邀你加入")
        return self._web_login_discord(discord_id, uname)

    def _guest_get(self, path: str):
        """Not logged in: the shared search results and the heat map, read only."""
        if path == "/api/venues":
            return self._json(VENUES)
        if path == "/api/history":
            return self._json({"stats": history.load_all(), "job": _history_job, "can_update": False})
        if not _search_job["running"]:
            _sync_search_from_cache()
        job = {k: v for k, v in _search_job.items() if k != "by"}  # who searched is nobody else's business
        return self._json({"results": _shown_results(), "job": job, "cache_minutes": scanner.CACHE_MAX_AGE_S // 60,
                           "guest": True})

    # --- GET ---

    def _get(self, acct: session.Account, path: str, body: dict):
        admin = config.is_admin(acct.name)
        if path == "/api/me":
            cfg = robot.load_config(acct)
            gcfg = config.load()
            return self._json({
                "account": acct.name, "display": display_name(acct), "admin": admin,
                "discord_account": config.is_discord_account(acct.name), "school": acct.school,
                "can_book": can_book(acct), "discord_id": cfg.get("discord_id", ""),
                "discord_ready": bool(gcfg["discord_token"]), "school_login": acct.has_session(),
                "login_error": acct.login_error, "has_password": credentials.load(acct.name) is not None,
                "members": cfg.get("members", robot.DEFAULT_MEMBERS), "member_types": booker.MEMBER_TYPES,
                "public_url": gcfg["public_url"], "home_url": oauth_redirect(),
                **({"allowed": gcfg["allowed"], "admins": gcfg["admins"], "scan_account": config.scan_account(),
                    "discord_users": [{"account": n, "display": display_name(session.account(n))}
                                      for n in config.users() if config.is_discord_account(n)]} if admin else {}),
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
                "auto_limit": AUTO_DAILY_LIMIT, "auto_left": auto_left(acct),
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
            if not acct.school:
                return self._json({"data": None, "running": False, "error": "連結計中帳號後才能在這裡看訂單"})
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
        if path == "/api/me/school":  # link a 計中 account to this account (or log in to it again)
            school = config.normalize_account(str(body.get("account", "")))
            password = str(body.get("password") or "")
            if not school or not password:
                return self._error(400, "請輸入計中帳號和密碼")
            if acct.school and school != acct.school:
                return self._error(400, f"你已經連結 {acct.school}，要換帳號請先取消連結")
            other = _account_by_school(school)
            if other and other is not acct:
                return self._error(409, "這個計中帳號已經連到另一個預約網站帳號，請用那個帳號登入")
            ip = self._client_ip()
            if _too_many_fails(f"a:{school}", f"i:{ip}"):
                return self._error(429, "失敗次數太多，請 15 分鐘後再試")
            try:
                acct.sso_login(password, school=school)
            except Exception as e:
                _add_fail(f"a:{school}", f"i:{ip}")
                log("warn", f"連結計中帳號失敗：{_err(e)}", acct.name)
                return self._error(401, "計中帳號登入沒有成功，請確認帳號密碼" if isinstance(e, session.NotLoggedIn)
                                   else f"無法連到學校網站（{_err(e)}）")
            credentials.save(acct.name, school, password if body.get("remember", True) else None)
            log("info", f"已連結計中帳號 {school}{'' if body.get('remember', True) else '（不儲存密碼）'}", acct.name)
            start_orders(acct)
            return self._json({"ok": True})
        if path == "/api/me/school/forget":  # keep the link, drop the stored password
            if acct.school:
                credentials.save(acct.name, acct.school, None)
                log("info", "已刪除儲存的計中密碼", acct.name)
            return self._json({"ok": True})
        if path == "/api/me/school/unlink":  # Discord-login accounts only: back to alerts only
            if not config.is_discord_account(acct.name):
                return self._error(400, "用計中帳號登入的帳號不能取消連結；可以改成不儲存密碼")
            if booker.busy(acct):
                return self._error(409, "有預訂進行中，請先取消")
            credentials.delete(acct.name)
            acct.clear()
            _orders.pop(acct.name, None)
            (acct.dir / "orders.json").unlink(missing_ok=True)
            log("info", "已取消連結計中帳號", acct.name)
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
        if path.startswith("/api/orders/") and path.endswith("/cancel"):
            if not acct.school:
                return self._error(400, "連結計中帳號後才能在這裡取消訂單")
            ok, msg = cancel_for(acct, path.split("/")[3])
            if ok:
                return self._json({"ok": True, "message": msg})
            return self._error(502 if ok is None else 400, msg)
        if path == "/api/admin/block" and admin:  # turn a Discord-login account away
            name = str(body.get("account", ""))
            if not config.is_discord_account(name):
                return self._error(400, "只能停用 Discord 登入的帳號")
            gcfg = config.load()
            gcfg["blocked"] = sorted(set(gcfg["blocked"]) | {name})
            config.save(gcfg)
            end_web_sessions(account=name)
            log("info", f"已停用 {name}", acct.name)
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
        if not can_book(acct):
            return self._error(400, "要先在「設定」連結計中帳號，伺服器才能幫你預訂；也可以直接到學校網站訂"
                               if not acct.school else "學校網站的登入已過期，請在「設定」重新登入計中帳號")
        # Pressing 預訂 on the page is the go-ahead: book straight away, nothing waits at the confirmation page.
        prep = start_prep(acct, venue, day, hour, quantity, auto=True, by_tap=True)
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

    def stop(signum, _frame):
        _tell_pending_on_shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)  # deploy/run.sh --stop, a restart, the machine shutting down
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _tell_pending_on_shutdown()


def _tell_pending_on_shutdown() -> None:
    """Bookings live in memory: say so before they are lost, instead of leaving dead buttons in Discord."""
    for prep in list(booker._preps.values()):
        if prep.finished or prep.status == "submitting":
            continue
        msg = f"⚠️ {prep.label}\n伺服器重新啟動，這筆預訂中斷了，沒有送出。空位還在的話請重新準備，或到學校網站自己訂。"
        log("warn", "伺服器停止，中斷了一筆還沒送出的預訂", prep.acct.name)
        try:
            notify.send_now(_discord_id(prep.acct), msg, buttons=[notify.link_button("到學校網站預訂", school_url(prep.venue))])
        except Exception:
            pass


if __name__ == "__main__":
    main()
