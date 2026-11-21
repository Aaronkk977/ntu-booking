"""Book a court on the server, with the person's last click on their phone.

prepare() starts a headless browser with the person's saved login, opens the venue, picks a free court for
the slot, fills the head count and presses 「下一步 確認租借內容」, so the school's confirmation page is
showing. That page is held open for HOLD_S while the person looks at it on their phone (a screenshot and
the details). Only when they press 「確認送出」 does decide(prep_id, "confirm") press the school's final
button; "cancel", or nobody answering in time, closes the page and sends nothing. Payment is always left
to the person on the school's site.

The site allows one court per order, so `quantity` courts are done one after another, each with its own
confirmation.
"""
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta

import orders
import scanner
import session

MEMBER_TYPES = {"1": "學生", "2": "教職", "3": "眷屬校友", "4": "校外人士"}
HOLD_S = 10 * 60  # how long a filled confirmation page waits for the person
VIEWPORT = {"width": 1280, "height": 900}
# Not needed to fill the form: skipped while filling, loaded normally once the confirmation page is shown.
SKIP_TYPES = {"image", "font", "media"}
SKIP_HOSTS = {"www.googletagmanager.com", "www.google-analytics.com", "ssllogo.twca.com.tw"}
# Words on the confirmation page's buttons that must never be pressed as the final one.
NOT_FINAL = ("取消", "返回", "上一步", "修改", "重新", "回上")
FINAL_WORDS = ("確認", "送出", "完成", "租借")
# The confirmation page gets an invisible reCAPTCHA v3 token from Google after it loads; sent without one,
# the order goes back to step 1. Tokens expire after 2 minutes.
TOKEN_WAIT_S = 15
TOKEN_FRESH_S = 90
_TOKEN_INPUTS = 'form#RentForm input[name="RecaptchaToken"]'


class SlotGone(Exception):
    pass


class Stuck(Exception):
    """Automatic filling stopped part-way."""


def open_courts(venue: str, day: str, hour: int) -> list[str]:
    """Court names that the latest walk saw open for this slot (tried first, in order)."""
    cached = scanner.cache_get(venue, 24 * 3600)
    courts = cached[0] if cached else scanner.partial_courts(venue)  # booked from a walk still in progress
    return [name for name, slots in courts if slots.get((day, hour)) == "open"]


def _skip_extras(route) -> None:
    from urllib.parse import urlparse

    req = route.request
    if req.resource_type in SKIP_TYPES or urlparse(req.url).hostname in SKIP_HOSTS:
        route.abort()
    else:
        route.continue_()


def _submit(page) -> None:
    """Press 「下一步 確認租借內容」 and wait for the confirmation page."""
    with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
        page.eval_on_selector('button[name="Send"]', "b => b.click()")


def _save_page(page, name: str, note: str = "") -> None:
    """Keep what the page looked like, for finding out where it got stuck or what changed."""
    session.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (session.SNAPSHOT_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
        url = page.url
    except Exception:
        url = "(page closed)"
    (session.SNAPSHOT_DIR / f"{name}.txt").write_text(f"{url}\n\n{note}", encoding="utf-8")


def _fill_one(page, venue: str, day: str, hour: int, preferred: list[str], exclude: list[str],
              members: dict[str, int]) -> tuple[str, str] | None:
    """On a fresh tab: open the venue, pick the first court (cached-open ones first, skipping `exclude`)
    that still has the slot free, fill the head count and wait for the cost. Returns (court, amount)."""
    page.goto(f"{session.BASE}/order/?Add={venue}", wait_until="domcontentloaded")
    page.wait_for_url(lambda u: "/order/A/" in u or "U=login" in u, timeout=20000, wait_until="domcontentloaded")
    if "U=login" in page.url:
        raise session.NotLoggedIn("學校網站的登入已過期")
    tabs = page.eval_on_selector_all("a.VSMenu", "els => els.map(e => e.getAttribute('title'))")
    candidates = [c for c in preferred + [t for t in tabs if t not in preferred] if c not in exclude]
    for court in candidates:
        tab = page.locator(f'a.VSMenu[title="{court}"]')
        if not tab.count():
            continue
        if "This" not in (tab.get_attribute("class") or ""):
            tab.click()
            page.wait_for_function("() => !document.querySelector('.ScheduleMain').classList.contains('Load')", timeout=15000)
        # The page zero-pads the hour (v="08"); accept either form.
        slot = page.locator(f'div.D[d="{day}"] a.S[s="Y"][v="{hour:02d}"], div.D[d="{day}"] a.S[s="Y"][v="{hour}"]').first
        if not slot.count():
            continue
        slot.evaluate("el => el.click()")
        page.wait_for_function(
            "() => { const v = document.getElementById('RentSchedule').value; return v && v !== '[]' && v !== '{}'; }",
            timeout=10000)
        for t, n in members.items():
            if n:
                page.fill(f'input[name="MemberType[{t}]"]', str(n))
                page.dispatch_event(f'input[name="MemberType[{t}]"]', "keyup")
        # The cost comes back asynchronously (UpdateCostAjax) and fills the form's TotalAmount;
        # submitting before that would send an empty total.
        page.wait_for_function(
            """() => { const t = document.querySelector('input[name="TotalAmount"]');
                       return (t && Number(t.value) > 0) || /合計\\s*\\$[1-9]/.test(document.body.innerText); }""",
            timeout=15000)
        amount = page.eval_on_selector('input[name="TotalAmount"]', "t => t.value") if page.locator(
            'input[name="TotalAmount"]').count() else ""
        return court, amount
    return None


def _final_button(page):
    """The confirmation page's one button that sends the order. On the site (Step=3 of RentForm) that is
    <button name="Send" title="下一步 完成租借">; the only other button is 「編輯」. If the page ever changes,
    fall back to the one submit-like control whose text says confirm/send and nothing like cancel/back.
    None unless exactly one fits."""
    send = page.locator('form#RentForm button[name="Send"]')
    if send.count() == 1 and "完成" in ((send.inner_text() or "") + (send.get_attribute("title") or "")):
        return send, send.inner_text().strip()
    found = []
    for el in page.locator('button, input[type="submit"], input[type="button"], a.Button, a.btn').all():
        try:
            if not el.is_visible():
                continue
            text = (el.inner_text() if el.evaluate("e => e.tagName") != "INPUT" else el.get_attribute("value")) or ""
        except Exception:
            continue
        text = text.strip()
        if text and any(w in text for w in FINAL_WORDS) and not any(w in text for w in NOT_FINAL):
            found.append((el, text))
    return found[0] if len(found) == 1 else None


def _wait_token(page, shown_at: float) -> bool:
    """Wait until the confirmation page's own script has put Google's token in RecaptchaToken. When the page
    has been open long enough for it to expire, ask for a new one the way the page does on load. True when
    there is a token, or the page has no such field."""
    if page.locator(_TOKEN_INPUTS).count() == 0:
        return True
    if time.monotonic() - shown_at > TOKEN_FRESH_S:
        page.evaluate("""sel => {
            const inputs = document.querySelectorAll(sel);
            inputs.forEach(i => { i.value = ""; });
            grecaptcha.ready(() => grecaptcha.execute(RecaptchaClass.RecaptchaSiteKey,
                {action: RecaptchaClass.ActionValue}).then(t => inputs.forEach(i => { i.value = t; })));
        }""", _TOKEN_INPUTS)
    try:
        page.wait_for_function("sel => [...document.querySelectorAll(sel)].every(i => i.value)", arg=_TOKEN_INPUTS,
                               timeout=TOKEN_WAIT_S * 1000)
        return True
    except Exception:
        return False


def _booked(acct: session.Account, day: str, hour: int, before: set[str]) -> str | None:
    """The id of a new, not cancelled order for this slot in the order list, or None."""
    data = orders.fetch_orders(acct)
    start = f"{hour:02d}:00"
    for o in data["orders"]:
        if o["id"] in before or o["kind"] == "cancelled":
            continue
        if any(s["date"] == day and s["start"].startswith(start) for s in o["slots"]):
            return o["id"]
    return None


# ---------- preparations ----------

class Prep:
    """One booking request of one person: its state is read by the web app and the notifier."""

    def __init__(self, acct, venue, label, day, hour, quantity, members, auto=False, by_tap=False):
        self.id = uuid.uuid4().hex[:12]
        self.acct, self.venue, self.label, self.day, self.hour = acct, venue, label, day, hour
        self.quantity, self.members = quantity, members
        self.status = "filling"  # filling | ready | submitting | done | failed | cancelled | expired
        self.message = "準備中…"
        self.court = None
        self.amount = ""
        self.screenshot: bytes | None = None
        self.shown_at = 0.0  # time.monotonic() when the confirmation page loaded
        self.expires_at: datetime | None = None
        self.booked: list[str] = []
        self.step = 1
        self.auto = auto  # the person's filter says: send without asking (robot, within the daily limit)
        self.by_tap = by_tap  # auto because the person pressed 預訂 (not the robot: no daily limit)
        self.order_ids: list[str] = []  # the orders this prep created
        self.preconfirmed_step = 0  # confirmed after the page expired: re-fill this step and send it at once
        self.created = datetime.now()
        self._decisions: queue.Queue = queue.Queue()
        self.on_change = lambda prep: None

    def public(self) -> dict:
        return {
            "id": self.id, "venue": self.venue, "label": self.label, "date": self.day, "hour": self.hour,
            "quantity": self.quantity, "step": self.step, "status": self.status, "message": self.message,
            "court": self.court, "amount": self.amount, "booked": self.booked,
            "has_screenshot": self.screenshot is not None, "auto": self.auto,
            "expires_at": self.expires_at.isoformat(timespec="seconds") if self.expires_at else None,
            "created": self.created.isoformat(timespec="seconds"),
        }

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed", "cancelled", "expired")


_preps: dict[str, Prep] = {}
_preps_lock = threading.Lock()


def get(prep_id: str) -> Prep | None:
    return _preps.get(prep_id)


def for_account(acct: session.Account) -> list[Prep]:
    """This person's preparations of the last day, newest first."""
    cutoff = datetime.now() - timedelta(days=1)
    with _preps_lock:
        for k in [k for k, p in _preps.items() if p.finished and p.created < cutoff]:
            del _preps[k]
        return sorted((p for p in _preps.values() if p.acct is acct), key=lambda p: p.created, reverse=True)


def busy(acct: session.Account) -> Prep | None:
    return next((p for p in for_account(acct) if not p.finished), None)


def prepare(acct: session.Account, venue: str, label: str, day: str, hour: int, quantity: int,
            members: dict[str, int], on_change=lambda prep: None, auto: bool = False,
            by_tap: bool = False) -> Prep:
    """Start filling in the background. `on_change(prep)` is called whenever its status changes. With
    `auto`, each filled confirmation page is sent at once instead of waiting for 確認送出."""
    prep = Prep(acct, venue, label, day, hour, quantity, members, auto, by_tap)
    prep.on_change = on_change
    with _preps_lock:
        _preps[prep.id] = prep
    threading.Thread(target=_run, args=(prep, on_change), daemon=True, name=f"booking-{acct.name}").start()
    return prep


def slot_passed(prep: Prep) -> bool:
    return datetime.now() >= datetime.fromisoformat(prep.day[:10]) + timedelta(hours=prep.hour)


def decide(prep_id: str, decision: str, on_change=None) -> bool:
    """"confirm" or "cancel" a prep that is waiting at the confirmation page. An expired prep can still be
    confirmed: its page was closed, so it is filled again and sent without waiting (the tap was the go-ahead)."""
    prep = _preps.get(prep_id)
    if not prep or decision not in ("confirm", "cancel"):
        return False
    if prep.status == "ready":
        prep._decisions.put(decision)
        return True
    if prep.status != "expired":
        return False
    with _preps_lock:
        if prep.status != "expired":
            return False
        if decision == "cancel":
            prep.status, prep.message = "cancelled", "已取消，沒有送出"
        else:
            if slot_passed(prep) or any(p is not prep and p.acct is prep.acct and not p.finished for p in _preps.values()):
                return False
            prep.preconfirmed_step = len(prep.booked) + 1
            prep.status, prep.message = "filling", "重新填寫並送出中…"
            prep._decisions = queue.Queue()
    if decision == "confirm":
        threading.Thread(target=_run, args=(prep, on_change or prep.on_change), daemon=True,
                         name=f"booking-{prep.acct.name}").start()
    return True


def _run(prep: Prep, on_change) -> None:
    def set_(status=None, message=None):
        if status:
            prep.status = status
        if message:
            prep.message = message
        try:
            on_change(prep)
        except Exception:
            pass

    acct = prep.acct
    # Hold the lock the whole time: ?Add= switches the venue in the server-side session, and a robot round
    # of the same account doing that mid-booking could change what is being confirmed.
    acct.booking_waiting.set()
    try:
        acct.site_lock.acquire()
    finally:
        acct.booking_waiting.clear()
    try:
        if not acct.has_session() and not acct.relogin():
            raise session.NotLoggedIn(acct.login_error or "學校網站尚未登入")
        try:
            _book(prep, set_)
        except session.NotLoggedIn:
            acct.clear()
            if prep.booked or not acct.relogin():
                raise
            _book(prep, set_)
    except SlotGone as e:
        set_("failed", str(e))
    except session.NotLoggedIn as e:
        set_("failed", f"{acct.login_error or e}")
    except Exception as e:  # a step timed out or the page changed
        first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        set_("failed", f"自動預訂沒有完成（{first}），請到學校網站自己完成")
    finally:
        acct.site_lock.release()


def _book(prep: Prep, set_) -> None:
    from playwright.sync_api import sync_playwright

    acct = prep.acct
    ua, cookies = acct.saved()
    preferred = open_courts(prep.venue, prep.day, prep.hour)
    with sync_playwright() as p:
        browser = session.launch_browser(p)
        try:
            ctx = browser.new_context(user_agent=ua, viewport=VIEWPORT, locale="zh-TW")
            ctx.add_cookies(cookies)
            for k in range(len(prep.booked) + 1, prep.quantity + 1):
                prep.step = k
                tag = f"第 {k}/{prep.quantity} 面：" if prep.quantity > 1 else ""
                set_("filling", f"{tag}自動填寫中…")
                page = ctx.new_page()
                page.route("**/*", _skip_extras)
                try:
                    found = _fill_one(page, prep.venue, prep.day, prep.hour, preferred, prep.booked, prep.members)
                    if found:
                        page.unroute("**/*", _skip_extras)  # the confirmation page loads in full
                        _submit(page)
                except session.NotLoggedIn:
                    raise
                except Exception as e:
                    _save_page(page, f"_booking_failed_{acct.name}", f"{e.__class__.__name__}: {e}")
                    raise
                if found is None:
                    if not prep.booked:
                        raise SlotGone("這個時段已經沒有可訂的場了")
                    set_("done", f"只訂到 {len(prep.booked)} 面，其餘已被訂走")
                    return
                prep.court, prep.amount = found
                page.wait_for_load_state("load", timeout=10000)
                prep.shown_at = time.monotonic()
                _save_page(page, f"_confirm_{acct.name}", "the school's confirmation page")
                prep.screenshot = page.screenshot(full_page=True, type="jpeg", quality=70)
                cost = f"，金額 ${prep.amount}" if prep.amount else ""
                if prep.auto or prep.preconfirmed_step == k:  # auto-send, or confirmed after the page expired
                    decision = "confirm"
                else:
                    prep.expires_at = datetime.now() + timedelta(seconds=HOLD_S)
                    set_("ready", f"{tag}已填到學校確認頁（{prep.court}{cost}），{HOLD_S // 60} 分鐘內按「確認送出」")
                    try:
                        decision = prep._decisions.get(timeout=HOLD_S)
                    except queue.Empty:
                        decision = None
                if decision != "confirm":
                    prep.screenshot = None
                    set_("cancelled" if decision == "cancel" else "expired",
                         "已取消，沒有送出" if decision == "cancel" else
                         f"{tag}{HOLD_S // 60} 分鐘內沒按，確認頁先關掉了，還沒送出。之後還想要的話照樣按「確認送出」，"
                         "會重新填一次並直接送出（場還在的話）")
                    return
                set_("submitting", f"{tag}{'自動' if prep.auto else ''}送出中…")
                _confirm(page, prep)
                prep.booked.append(prep.court)
                page.close()
            set_("done", f"已訂到 {len(prep.booked)} 面（{'、'.join(prep.booked)}），請記得在期限內繳費")
        finally:
            browser.close()


def _confirm(page, prep: Prep) -> None:
    """Press the school's final button and make sure the order exists."""
    acct = prep.acct
    before = {o["id"] for o in orders.fetch_orders(acct)["orders"]}
    button = _final_button(page)
    if button is None:
        _save_page(page, f"_confirm_nobutton_{acct.name}", "no single final button found")
        raise Stuck("確認頁上找不到唯一的送出鈕（網站可能改版，頁面已存到 data/snapshots/）")
    el, _text = button
    if not _wait_token(page, prep.shown_at):
        _save_page(page, f"_confirm_notoken_{acct.name}", "no reCAPTCHA token")
        raise Stuck(f"確認頁等了 {TOKEN_WAIT_S} 秒還沒拿到 Google 的 reCAPTCHA 驗證，沒有送出")
    # Playwright dismisses dialogs by default, which would answer "cancel" to a 「確定要送出嗎？」 prompt.
    said = []
    page.on("dialog", lambda d: (said.append(d.message.strip()), d.accept()))
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            el.evaluate("b => b.click()")
    except Exception:  # some sites answer with a dialog or an inline message instead of a new page
        page.wait_for_timeout(3000)
    _save_page(page, f"_confirm_result_{acct.name}", "after pressing the final button")
    time.sleep(1)
    order_id = _booked(acct, prep.day, prep.hour, before)
    if not order_id:
        if said:
            raise Stuck(f"學校網站回覆：{said[-1]}")
        if page.locator("textarea#RentSchedule").count():  # back on step 1's form
            raise Stuck("學校網站把這次送出退回第 1 步，沒有訂到")
        raise Stuck("按了送出，但訂單列表裡找不到這筆，請到學校網站確認")
    prep.order_ids.append(order_id)
