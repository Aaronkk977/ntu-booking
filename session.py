"""Logged-in sessions for rent.pe.ntu.edu.tw, one per person (Account).

Login goes through NTU's 計中 SSO (adfs.ntu.edu.tw, no captcha) in a headless browser, so it also runs
unattended when a login expires. The resulting cookies and the browser's User-Agent are saved to
data/users/<account>/session.json and reused for plain HTTP requests.

"/order/?Add=<venue>" switches the venue stored in the site's server-side session, so each Account has
its own lock: only one venue walk or booking per account talks to the site at a time.
"""
import json
import ssl
import threading
import time
import urllib.request
from urllib.parse import urlparse

import config
import credentials

HOST = "rent.pe.ntu.edu.tw"
BASE = f"https://{HOST}"
SSO_URL = f"{BASE}/sso2_go.php"  # the site's 「計資中心帳號認證登入」 link; redirects to adfs.ntu.edu.tw
SSO_HOST = "adfs.ntu.edu.tw"
PROBE_PATH = "/order/A/"  # redirects to the login page when not logged in
SNAPSHOT_DIR = config.SNAPSHOT_DIR

# Python 3.13+ checks certificates strictly (VERIFY_X509_STRICT) and rejects the site's chain for a missing
# Subject Key Identifier. Drop only that flag: the certificate is still verified as before 3.13.
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT

# Playwright's own Chromium first (what deploy/setup.sh installs on Linux), then Edge / Chrome (a Windows
# or Mac machine used for development).
BROWSER_CHANNELS = (None, "msedge", "chrome")

# After a failed unattended login, wait this long before trying again, so a wrong or changed password is
# not retried until the 計中 account locks.
RELOGIN_COOLDOWN_S = 10 * 60


def launch_browser(p, **kwargs):
    """Launch the first installed browser of BROWSER_CHANNELS with Playwright `p` (always headless)."""
    last = None
    for channel in BROWSER_CHANNELS:
        try:
            return p.chromium.launch(channel=channel, headless=True, **kwargs)
        except Exception as e:  # playwright's Error: this one is not installed
            last = e
    raise last


class NotLoggedIn(Exception):
    pass


class Preempted(Exception):
    """A venue walk stopped early so that a booking could use the site."""


def is_logged_out_page(html: str) -> bool:
    # The "not logged in" response is a tiny page whose only job is alert() + redirect.
    return "請登入會員" in html and len(html) < 2000


def _domain_matches(cookie_domain: str) -> bool:
    d = cookie_domain.lstrip(".")
    return HOST == d or HOST.endswith("." + d)


def _wait_logged_in(page, ctx, timeout_s: float) -> bool:
    """Poll until the site itself answers a members-only page. Leaving the login page is not proof of
    login (the SSO link leaves it too)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        if urlparse(page.url).hostname != HOST or "U=login" in page.url:
            continue
        if not is_logged_out_page(ctx.request.get(BASE + PROBE_PATH).text()):
            return True
    return False


def _err(e: Exception) -> str:
    return f"{e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}"


class Account:
    def __init__(self, name: str):
        self.name = name
        self.dir = config.user_dir(name)
        self.session_file = self.dir / "session.json"
        self.site_lock = threading.Lock()
        # Set while a booking waits for site_lock. Walks over many courts stop at the next court and let it
        # go first, instead of making the person wait out a whole venue.
        self.booking_waiting = threading.Event()
        self._relogin_lock = threading.Lock()
        self._relogin_failed_at = 0.0
        self.login_error: str | None = None  # the last unattended login failure, shown in the UI

    def __repr__(self):
        return f"Account({self.name})"

    # ---------- login ----------

    @property
    def school(self) -> str | None:
        """The 計中 account this person logs in to the school's site with (None: not linked yet)."""
        return credentials.school(self.name)

    def sso_login(self, password: str, timeout_s: int = 60, school: str | None = None) -> None:
        """Log in through 計中 SSO in a headless browser, as `school` (default: the linked 計中 account).
        Raises NotLoggedIn when it does not end logged in (wrong password, or an extra step such as a
        verification code that needs a person)."""
        school = school or self.school
        if not school:
            raise NotLoggedIn("還沒連結計中帳號")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = launch_browser(p)
            try:
                # Save the User-Agent of a normal window, not the headless one, for the plain requests.
                probe = browser.new_page()
                ua = probe.evaluate("navigator.userAgent").replace("HeadlessEdg", "Edg").replace("HeadlessChrome", "Chrome")
                probe.close()
                ctx = browser.new_context(user_agent=ua)
                page = ctx.new_page()
                page.goto(SSO_URL)
                page.wait_for_url(lambda u: urlparse(u).hostname == SSO_HOST, timeout=20000)
                page.fill('input[name$="UsernameTextBox"]', school)
                page.fill('input[name$="PasswordTextBox"]', password)
                page.click('input[name$="SubmitButton"]')
                if not _wait_logged_in(page, ctx, timeout_s):
                    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                    (SNAPSHOT_DIR / f"_sso_failed_{self.name}.html").write_text(page.content(), encoding="utf-8")
                    stuck = "停在計中登入頁，可能是帳號密碼錯誤" if urlparse(page.url).hostname == SSO_HOST else "沒有回到租借系統"
                    raise NotLoggedIn(f"計中帳號登入沒有成功（{stuck}）")
                cookies = [c for c in ctx.cookies() if _domain_matches(c["domain"])]
                config.write_json(self.session_file, {"user_agent": ua, "cookies": cookies})
            finally:
                browser.close()
        self.login_error = None
        self._relogin_failed_at = 0.0

    def relogin(self) -> bool:
        """Log in again unattended with the stored password. False (without trying) during the cooldown
        after a failure, or when no password is stored."""
        with self._relogin_lock:
            if self.has_session():  # another thread logged in again while this one waited
                return True
            if time.monotonic() - self._relogin_failed_at < RELOGIN_COOLDOWN_S and self._relogin_failed_at:
                return False
            if not self.school:
                self.login_error = "還沒連結計中帳號"
                return False
            password = credentials.load(self.name)
            if not password:
                self.login_error = "學校網站的登入已過期；你選了不儲存密碼，請在「設定」重新登入計中帳號"
                return False
            try:
                self.sso_login(password)
                return True
            except Exception as e:
                self._relogin_failed_at = time.monotonic()
                self.login_error = (f"自動重新登入學校網站失敗（{_err(e)}），{RELOGIN_COOLDOWN_S // 60} 分鐘後再試；"
                                    "改過計中密碼的話請登出再登入這個網站")
                return False

    def with_relogin(self, fn):
        """fn(), retried once after an unattended re-login if it found the login expired."""
        try:
            return fn()
        except NotLoggedIn:
            if not self.relogin():
                raise
            return fn()

    def has_session(self) -> bool:
        """True while a login is saved. An expired login is deleted as soon as the site reports it."""
        return self.session_file.exists()

    def clear(self) -> None:
        self.session_file.unlink(missing_ok=True)

    def check(self) -> None:
        """Ask the site once whether the saved login still works (deletes it if not)."""
        try:
            self.fetch(PROBE_PATH)
        except (NotLoggedIn, OSError):  # offline: leave it to the next real request
            pass

    # ---------- requests ----------

    def pause(self, seconds: float) -> None:
        """The polite gap between requests of a venue walk. Raises Preempted at once if a booking is waiting."""
        if self.booking_waiting.wait(seconds):
            raise Preempted("讓預訂先使用網站")

    def saved(self) -> tuple[str, list[dict]]:
        """(User-Agent, cookies) of the saved login."""
        if not self.session_file.exists():
            raise NotLoggedIn("尚未登入學校網站")
        data = json.loads(self.session_file.read_text(encoding="utf-8"))
        return data["user_agent"], [c for c in data["cookies"] if _domain_matches(c["domain"])]

    def fetch(self, path: str, form: str | None = None) -> str:
        """GET (or POST `form`, an urlencoded body) with the saved cookies. Raises NotLoggedIn when expired."""
        ua, cookies = self.saved()
        headers = {"User-Agent": ua, "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies)}
        if form is not None:
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE + "/order/A/",
            })
        req = urllib.request.Request(BASE + path, data=form.encode("utf-8") if form is not None else None, headers=headers)
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as res:
            html = res.read().decode("utf-8", errors="replace")
        if is_logged_out_page(html):
            self.clear()
            raise NotLoggedIn("學校網站的登入已過期")
        return html


_accounts: dict[str, Account] = {}
_accounts_lock = threading.Lock()


def account(name: str) -> Account:
    """The one Account object of `name` (its locks must be shared by every thread)."""
    with _accounts_lock:
        if name not in _accounts:
            _accounts[name] = Account(name)
        return _accounts[name]
