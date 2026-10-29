"""Logged-in session for rent.pe.ntu.edu.tw.

Login goes through NTU's 計中 SSO (adfs.ntu.edu.tw, no captcha) in a hidden browser window when the saved
account is an @ntu.edu.tw address, so it can also run unattended when the login expires. Otherwise, or
when that fails, a visible browser window opens with the stored username and password pre-filled and waits
for the user to finish (the site's own form needs a captcha). The resulting cookies and the browser's
User-Agent are saved to session.json and reused by the scanner.
"""
import json
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HOST = "rent.pe.ntu.edu.tw"
BASE = f"https://{HOST}"
LOGIN_URL = f"{BASE}/member/?U=login"
SSO_URL = f"{BASE}/sso2_go.php"  # the site's 「計資中心帳號認證登入」 link; redirects to adfs.ntu.edu.tw
SSO_HOST = "adfs.ntu.edu.tw"
PROBE_PATH = "/order/A/"  # redirects to the login page when not logged in
SESSION_FILE = Path(__file__).with_name("session.json")
SNAPSHOT_DIR = Path(__file__).with_name("snapshots")


# "/order/?Add=<venue>" switches the venue stored in the server-side session, so only one
# venue walk (scan, search, history) may talk to the site at a time.
SITE_LOCK = threading.Lock()
# Set while a booking waits for SITE_LOCK. Walks over many courts (robot, search, history) stop at the next
# court and let it go first, instead of making the user wait out a whole venue.
BOOKING_WAITING = threading.Event()


# Edge first (it comes with Windows), then Chrome; None is Playwright's own Chromium, for a Mac with
# neither (`python -m playwright install chromium`).
BROWSER_CHANNELS = ("msedge", "chrome", None)


def launch_browser(p, **kwargs):
    """Launch the first installed browser of BROWSER_CHANNELS with Playwright `p`."""
    last = None
    for channel in BROWSER_CHANNELS:
        try:
            return p.chromium.launch(channel=channel, **kwargs)
        except Exception as e:  # playwright's Error: this one is not installed
            last = e
    raise last


class NotLoggedIn(Exception):
    pass


class Preempted(Exception):
    """A venue walk stopped early so that a booking could use the site."""


def pause(seconds: float) -> None:
    """The polite gap between requests of a venue walk. Raises Preempted at once if a booking is waiting."""
    if BOOKING_WAITING.wait(seconds):
        raise Preempted("讓預訂先使用網站")


def is_logged_out_page(html: str) -> bool:
    # The "not logged in" response is a tiny page whose only job is alert() + redirect.
    return "請登入會員" in html and len(html) < 2000


def _domain_matches(cookie_domain: str) -> bool:
    d = cookie_domain.lstrip(".")
    return HOST == d or HOST.endswith("." + d)


def sso_username(account: str) -> str | None:
    """The 計中 account behind a rent-system login like b12345678@ntu.edu.tw; None for other accounts."""
    name, _, domain = account.strip().partition("@")
    return name if name and domain.lower() == "ntu.edu.tw" else None


def _wait_logged_in(page, ctx, timeout_s: float) -> bool:
    """Poll until the site itself answers a members-only page. Leaving the login page is not proof of
    login (the SSO link leaves it too)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)  # raises if the user closed the window
        if urlparse(page.url).hostname != HOST or "U=login" in page.url:
            continue
        if not is_logged_out_page(ctx.request.get(BASE + PROBE_PATH).text()):
            return True
    return False


def _save_session(ctx, user_agent: str) -> None:
    cookies = [c for c in ctx.cookies() if _domain_matches(c["domain"])]
    SESSION_FILE.write_text(json.dumps({"user_agent": user_agent, "cookies": cookies}, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def sso_login(sso_user: str, password: str, timeout_s: int = 60) -> None:
    """Log in through 計中 SSO in a hidden browser window. Raises NotLoggedIn when it does not end logged in
    (wrong password, or an extra step such as a verification code that needs a person)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        try:
            # Save the User-Agent of a normal window, not the headless one, for the scanner's requests.
            probe = browser.new_page()
            ua = probe.evaluate("navigator.userAgent").replace("HeadlessEdg", "Edg").replace("HeadlessChrome", "Chrome")
            probe.close()
            ctx = browser.new_context(user_agent=ua)
            page = ctx.new_page()
            page.goto(SSO_URL)
            page.wait_for_url(lambda u: urlparse(u).hostname == SSO_HOST, timeout=20000)
            page.fill('input[name$="UsernameTextBox"]', sso_user)
            page.fill('input[name$="PasswordTextBox"]', password)
            page.click('input[name$="SubmitButton"]')
            if not _wait_logged_in(page, ctx, timeout_s):
                SNAPSHOT_DIR.mkdir(exist_ok=True)
                (SNAPSHOT_DIR / "_sso_failed.html").write_text(page.content(), encoding="utf-8")
                stuck = "停在計中登入頁，可能是帳號密碼錯誤" if urlparse(page.url).hostname == SSO_HOST else "沒有回到租借系統"
                raise NotLoggedIn(f"計中帳號登入沒有成功（{stuck}，頁面已存到 snapshots/）")
            _save_session(ctx, ua)
        finally:
            browser.close()


def interactive_login(username: str, password: str, timeout_s: int = 300) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(LOGIN_URL)
        page.fill("#Username_login", username)
        page.fill("#Password_login", password)
        page.focus("#UserInputNo_login")
        if not _wait_logged_in(page, ctx, timeout_s):
            raise TimeoutError("5 分鐘內未完成登入")
        _save_session(ctx, page.evaluate("navigator.userAgent"))
        browser.close()


def has_session() -> bool:
    """True while a login is saved. An expired login is deleted as soon as the site reports it."""
    return SESSION_FILE.exists()


def check() -> None:
    """Ask the site once whether the saved login still works (deletes it if not)."""
    try:
        fetch(PROBE_PATH)
    except (NotLoggedIn, OSError):  # offline: leave it to the next real request
        pass


def clear() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def fetch(path: str, form: str | None = None) -> str:
    """GET (or POST `form`, an urlencoded body) with the saved cookies.

    Raises NotLoggedIn when the session expired.
    """
    if not SESSION_FILE.exists():
        raise NotLoggedIn("尚未登入")
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    cookies = [c for c in data["cookies"] if _domain_matches(c["domain"])]
    headers = {"User-Agent": data["user_agent"], "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies)}
    if form is not None:
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/order/A/",
        })
    req = urllib.request.Request(BASE + path, data=form.encode("utf-8") if form is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as res:
        html = res.read().decode("utf-8", errors="replace")
    if is_logged_out_page(html):
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        (SNAPSHOT_DIR / "_logged_out.html").write_text(html, encoding="utf-8")
        clear()  # so the header stops showing "logged in"
        raise NotLoggedIn("登入已過期")
    return html
