"""Open the school's booking page in a visible Edge window with the saved login, pick a free court for
the requested slot, fill the head count, and press 「下一步 確認租借內容」 so the school's confirmation
page is showing. The final confirmation and payment are always left to the user in that window.
"""
import json
import time

import scanner
import session

MEMBER_TYPES = {"1": "學生", "2": "教職", "3": "眷屬校友", "4": "校外人士"}


class SlotGone(Exception):
    pass


def open_courts(venue: str, day: str, hour: int) -> list[str]:
    """Court names that the latest cached walk saw open for this slot (tried first, in order)."""
    cached = scanner.cache_get(venue, 24 * 3600)
    if not cached:
        return []
    return [name for name, slots in cached[0] if slots.get((day, hour)) == "open"]


def _playwright_cookies() -> tuple[str, list[dict]]:
    if not session.SESSION_FILE.exists():
        raise session.NotLoggedIn("尚未登入")
    data = json.loads(session.SESSION_FILE.read_text(encoding="utf-8"))
    keys = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
    cookies = [{k: c[k] for k in keys if k in c} for c in data["cookies"] if session._domain_matches(c["domain"])]
    return data["user_agent"], cookies


def _wait_closed(page, browser, keep_open_s: int | None) -> None:
    """Wait until the user closes this tab (or the whole window), or keep_open_s if given. The user may still
    be confirming or paying, so by default there is no time limit: the window is never closed from here.
    wait_for_timeout (not time.sleep) keeps Playwright processing events, so the close is noticed."""
    deadline = time.monotonic() + keep_open_s if keep_open_s else float("inf")
    while time.monotonic() < deadline and browser.is_connected() and not page.is_closed():
        try:
            page.wait_for_timeout(1000)
        except Exception:  # closed while waiting
            break


def _fill_one(page, venue: str, day: str, hour: int, preferred: list[str], exclude: list[str],
              members: dict[str, int]) -> str | None:
    """On a fresh tab: open the venue, pick the first court (cached-open ones first, skipping `exclude`)
    that still has the slot free, fill the head count and wait for the cost. Returns the court, or None."""
    page.goto(f"{session.BASE}/order/?Add={venue}")
    page.wait_for_url(lambda u: "/order/A/" in u or "U=login" in u, timeout=20000)
    if "U=login" in page.url:
        session.clear()
        raise session.NotLoggedIn("登入已過期")
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
        return court
    return None


def open_booking(venue: str, day: str, hour: int, members: dict[str, int], status, quantity: int = 1,
                 submit: bool = True, keep_open_s: int | None = None, inspect=None) -> list[str]:
    """Book `quantity` courts for one slot. The site allows one court per order, so courts are done one
    after another: each gets its own tab stopped at the school's confirmation page, and the next one
    starts when the user closes that tab. Returns the courts that reached the confirmation page.
    `status(msg)` reports progress; `inspect(page)`, if given, runs before each tab is handed over."""
    from playwright.sync_api import sync_playwright

    ua, cookies = _playwright_cookies()
    preferred = open_courts(venue, day, hour)
    done: list[str] = []
    # Hold the lock the whole time: ?Add= switches the venue in the server-side session, and a robot
    # round doing that mid-booking could change what the user is confirming.
    with session.SITE_LOCK, sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=False)
        ctx = browser.new_context(user_agent=ua)
        ctx.add_cookies(cookies)
        for k in range(1, quantity + 1):
            if not browser.is_connected():
                break
            tag = f"第 {k}/{quantity} 面" if quantity > 1 else ""
            page = ctx.new_page()
            status(f"{tag}開啟訂場頁面…")
            try:
                court = _fill_one(page, venue, day, hour, preferred, done, members)
            except session.NotLoggedIn:
                browser.close()
                raise
            if court is None:
                page.close()
                if not done:
                    browser.close()
                    raise SlotGone("這個時段已經沒有可訂的場了")
                status(f"只剩 {len(done)} 面可訂，其餘已被訂走")
                break
            if submit:
                page.click('button[name="Send"]')
                page.wait_for_load_state("domcontentloaded")
                more = "完成後關閉這個分頁，就會接著開下一面" if k < quantity else "完成後關閉視窗即可"
                status(f"{tag}已到學校的確認頁，請在 Edge 確認並完成預訂（{more}）")
            else:
                status(f"{tag}已填好表單（測試模式，未按下一步）")
            done.append(court)
            if inspect:
                inspect(page)
            _wait_closed(page, browser, keep_open_s)
        if browser.is_connected():
            browser.close()
        return done
