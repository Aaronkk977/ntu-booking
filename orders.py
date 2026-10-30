"""The member's orders, read from 常租紀錄 (/member/rental/) on rent.pe.ntu.edu.tw.

Online bookings of ball courts show up there (租借紀錄 stays empty for them). Each order has its rental
times, venue, amount, 訂單狀態 (已核准, 會員取消, ...) and 繳費狀態 (待繳費 with a deadline, 已繳費 with
the time paid). The list may be paged on the site; only its first page is read. Reading the list does
not change the venue chosen in the server-side session, so it does not need session.SITE_LOCK.
"""
import html as htmllib
import json
import re
from datetime import datetime
from pathlib import Path

import session

ORDERS_PATH = "/member/rental/"
ORDERS_FILE = Path(__file__).with_name("orders.json")  # the last list read, shown at once after a restart

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
        })
    return orders


def classify(order: dict) -> str:
    """"cancelled", "paid" or "unpaid"."""
    if "取消" in order["order_status"] or "取消" in order["payment_status"]:
        return "cancelled"
    if "已繳" in order["payment_status"]:
        return "paid"
    return "unpaid"


def fetch_orders() -> dict:
    """Read the order list from the site. Raises session.NotLoggedIn when the login expired."""
    page = session.fetch(ORDERS_PATH)
    if "Logout.php" not in page:  # the member pages show a login form instead of the tiny logged-out page
        session.clear()
        raise session.NotLoggedIn("登入已過期")
    session.SNAPSHOT_DIR.mkdir(exist_ok=True)
    (session.SNAPSHOT_DIR / "_orders.html").write_text(page, encoding="utf-8")
    orders = parse_orders(page)
    if not orders and "無符合資料紀錄" not in page and "OrderNo" not in page:
        raise ValueError("訂單頁面格式可能改了（頁面已存到 snapshots/_orders.html）")
    for o in orders:
        o["kind"] = classify(o)
    data = {"fetched": datetime.now().isoformat(timespec="seconds"), "orders": orders}
    ORDERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def load_saved() -> dict | None:
    return json.loads(ORDERS_FILE.read_text(encoding="utf-8")) if ORDERS_FILE.exists() else None


def pay_path(order_id: str) -> str:
    """The site's own 「線上付款」 link of an order."""
    return f"{ORDERS_PATH}?K={order_id}&BUrl=%2Fmember%2Frental%2F&P=Y"


def detail_path(order_id: str) -> str:
    return f"{ORDERS_PATH}?K={order_id}&BUrl=%2Fmember%2Frental%2F"
