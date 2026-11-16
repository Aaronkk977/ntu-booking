"""The member's orders, read from 常租紀錄 (/member/rental/) on rent.pe.ntu.edu.tw.

Online bookings of ball courts show up there (租借紀錄 stays empty for them). Each order has its rental
times, venue, amount, 訂單狀態 (已核准, 會員取消, ...) and 繳費狀態 (待繳費 with a deadline, 已繳費 with
the time paid). The list may be paged on the site; only its first page is read. Reading the list does
not change the venue chosen in the server-side session, so it does not need the account's site_lock.
"""
import html as htmllib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
import config
import session

ORDERS_PATH = "/member/rental/"

_ROW_RE = re.compile(r"<tbody>\s*<tr class=\"I\">(.*?)</tr>\s*</tbody>", re.S)
_CELL_RE = re.compile(r'<div class="(?:MC )?(\w+)"[^>]*>(.*?)</div>', re.S)
_RENT_RE = re.compile(r'<span class="RD">([\d-]+)</span>\s*<span class="RT">([\d:]+)\s*~\s*([\d:]+)</span>')
_ID_RE = re.compile(r"\?K=(\d+)")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_orders(page: str) -> list[dict]:
    """Every order row of the 常租紀錄 page, newest first as the site lists them."""
    orders = []
    for row in _ROW_RE.findall(page):
        cells = {}
        for cls, inner in _CELL_RE.findall(row):
            cells.setdefault(cls, inner)
        k = _ID_RE.search(row)
        # 「繳費截止日 2026-11-01」 while unpaid, 「繳費時間 2025-10-08 11:19:58」 once paid
        payment_note = _text(cells.get("PaymentDeadline", ""))
        deadline = _DATE_RE.search(payment_note) if "截止" in payment_note else None
        orders.append({
            "id": k.group(1) if k else "",
            "order_no": _text(cells.get("OrderNo", "")),
            "created": _text(cells.get("CreateDate", "")),
            "slots": [{"date": d, "start": s, "end": e} for d, s, e in _RENT_RE.findall(row)],
            "venue": _text(cells.get("VenuesName", "")),
            "amount": _text(cells.get("TotalAmount", "")),
            "order_status": _text(cells.get("OrderStatus", "")),
            "payment_status": _text(cells.get("PaymentStatus", "")),
            "payment_note": payment_note,
            "deadline": deadline.group(0) if deadline else None,
            "can_pay": "線上付款" in row,
            "can_cancel": "CancelOrderFun" in row,  # the row's 「取消訂單」 button
        })
    return orders


def classify(order: dict) -> str:
    """"cancelled", "paid" or "unpaid"."""
    if "取消" in order["order_status"] or "取消" in order["payment_status"]:
        return "cancelled"
    if "已繳" in order["payment_status"]:
        return "paid"
    return "unpaid"


def _file(acct: session.Account):
    return acct.dir / "orders.json"  # the last list read, shown at once after a restart


def fetch_orders(acct: session.Account) -> dict:
    """Read acct's order list from the site. Raises session.NotLoggedIn when the login expired."""
    page = acct.fetch(ORDERS_PATH)
    if "Logout.php" not in page:  # the member pages show a login form instead of the tiny logged-out page
        acct.clear()
        raise session.NotLoggedIn("登入已過期")
    session.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (session.SNAPSHOT_DIR / f"_orders_{acct.name}.html").write_text(page, encoding="utf-8")
    orders = parse_orders(page)
    if not orders and "無符合資料紀錄" not in page and "OrderNo" not in page:
        raise ValueError("訂單頁面格式可能改了（頁面已存到 data/snapshots/）")
    for o in orders:
        o["kind"] = classify(o)
    data = {"fetched": datetime.now().isoformat(timespec="seconds"), "orders": orders}
    config.write_json(_file(acct), data)
    return data


def load_saved(acct: session.Account) -> dict | None:
    f = _file(acct)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def cancel_order(acct: session.Account, order_id: str) -> tuple[bool | None, dict]:
    """Cancel one order the way the site's 「取消訂單」 button does after its 「您確定取消訂單嗎？」 prompt:
    open /order/?Cancel=O:<id>&S=rental. Then read the list again; returns (cancelled, fresh list), where
    cancelled is None when the order is no longer listed (can't tell). Raises session.NotLoggedIn."""
    if not order_id.isdigit():
        raise ValueError("bad order id")
    # /order/ keeps state in the server-side session (the chosen venue), so don't run under a venue walk
    # of the same account: let a walk stop at its next court, as a booking does.
    acct.booking_waiting.set()
    try:
        acct.site_lock.acquire()
    finally:
        acct.booking_waiting.clear()
    try:
        page = acct.fetch(f"/order/?Cancel=O:{order_id}&S=rental", referer=session.BASE + ORDERS_PATH)
        session.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (session.SNAPSHOT_DIR / f"_cancel_{acct.name}.html").write_text(page, encoding="utf-8")
    finally:
        acct.site_lock.release()
    data = fetch_orders(acct)
    order = next((o for o in data["orders"] if o["id"] == order_id), None)
    return (order["kind"] == "cancelled" if order else None), data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # hand the 3xx back instead of following it
        return None


_FORM_RE = re.compile(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>', re.S | re.I)
_INPUT_RE = re.compile(r'<input([^>]*)>', re.I)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _fields(form_html: str) -> list[tuple[str, str]]:
    """The values a browser would submit: hidden/text inputs, and the first radio of each name."""
    out, radios = [], set()
    for attrs in _INPUT_RE.findall(form_html):
        a = {k.lower(): htmllib.unescape(v) for k, v in _ATTR_RE.findall(attrs)}
        name, kind = a.get("name"), a.get("type", "text").lower()
        if not name or kind in ("submit", "button", "image", "file"):
            continue
        if kind in ("radio", "checkbox"):
            if name in radios:
                continue
            radios.add(name)
        out.append((name, a.get("value", "")))
    return out


def start_payment(acct: session.Account, order_id: str) -> dict:
    """Do what the site's 「線上付款」 → 信用卡 → 「付款」 does with the server's login, up to the moment the
    site hands over to the bank, and return that hand-over for the person's own browser:
    {"url": ...} for a redirect, or {"action", "method", "fields"} for a form to post. The bank's pages
    need no login of the school site, so a phone can carry on from there.
    Raises NotLoggedIn, or ValueError when the site answers something unexpected (saved to snapshots)."""
    if not order_id.isdigit():
        raise ValueError("bad order id")
    page = acct.fetch(pay_path(order_id), referer=session.BASE + ORDERS_PATH)
    session.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (session.SNAPSHOT_DIR / f"_pay_{acct.name}.html").write_text(page, encoding="utf-8")
    form = next(((a, f) for a, f in _FORM_RE.findall(page) if "GoPay" in a), None)
    if not form:
        raise ValueError("付款頁上找不到付款表單（可能已經繳費，或網站改版）")
    action, body = form
    fields = _fields(body) + [("PayGo", "OK")]
    ua, cookies = acct.saved()
    req = urllib.request.Request(
        urllib.parse.urljoin(session.BASE + pay_path(order_id), htmllib.unescape(action)),
        data=urllib.parse.urlencode(fields).encode(), method="POST", headers={
            "User-Agent": ua, "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
            "Content-Type": "application/x-www-form-urlencoded", "Referer": session.BASE + pay_path(order_id)})
    # Not following redirects here: urllib would carry the school site's Cookie header to the bank.
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=session._SSL_CONTEXT))
    try:
        with opener.open(req, timeout=20) as res:
            status, location, html = res.status, res.headers.get("Location"), res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:  # a 3xx comes back as an "error" when not followed
        status, location, html = e.code, e.headers.get("Location"), e.read().decode("utf-8", errors="replace")
    (session.SNAPSHOT_DIR / f"_gopay_{acct.name}.html").write_text(
        f"<!-- HTTP {status} Location: {location} -->\n{html}", encoding="utf-8")
    if location:
        url = urllib.parse.urljoin(session.BASE + "/member/GoPay.php", location)
        if urllib.parse.urlparse(url).hostname != urllib.parse.urlparse(session.BASE).hostname:
            return {"url": url}
        raise ValueError(f"付款時學校網站轉到 {location}，不是銀行的付款頁")
    if session.is_logged_out_page(html):
        acct.clear()
        raise session.NotLoggedIn("學校網站的登入已過期")
    for action, body in _FORM_RE.findall(html):  # an auto-submitting form to the bank
        target = urllib.parse.urljoin(session.BASE + "/member/GoPay.php", htmllib.unescape(action))
        if urllib.parse.urlparse(target).hostname != urllib.parse.urlparse(session.BASE).hostname:
            method = "get" if re.search(r'method\s*=\s*"get"', body + action, re.I) else "post"
            return {"action": target, "method": method, "fields": _fields(body)}
    m = re.search(r'(?:location(?:\.href)?\s*=|url=)\s*["\']?(https?://[^"\'\s;>]+)', html, re.I)
    if m and urllib.parse.urlparse(m.group(1)).hostname != urllib.parse.urlparse(session.BASE).hostname:
        return {"url": htmllib.unescape(m.group(1))}
    raise ValueError(f"學校網站沒有轉到銀行的付款頁（HTTP {status}，頁面已存到 data/snapshots/）")


def pay_path(order_id: str) -> str:
    """The site's own 「線上付款」 link of an order."""
    return f"{ORDERS_PATH}?K={order_id}&BUrl=%2Fmember%2Frental%2F&P=Y"


def detail_path(order_id: str) -> str:
    return f"{ORDERS_PATH}?K={order_id}&BUrl=%2Fmember%2Frental%2F"
