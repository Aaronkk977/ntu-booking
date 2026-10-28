"""Logged-in session for rent.pe.ntu.edu.tw.

Login opens a real Edge window via Playwright, pre-fills the stored username
and password, and waits for the user to finish logging in (captcha, or the
NTU SSO button). The resulting cookies and the browser's User-Agent are saved
to session.json and reused by the scanner.
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
PROBE_PATH = "/order/A/"  # redirects to the login page when not logged in
SESSION_FILE = Path(__file__).with_name("session.json")
SNAPSHOT_DIR = Path(__file__).with_name("snapshots")


# "/order/?Add=<venue>" switches the venue stored in the server-side session, so only one
# venue walk (scan, search, history) may talk to the site at a time.
SITE_LOCK = threading.Lock()


class NotLoggedIn(Exception):
    pass


def is_logged_out_page(html: str) -> bool:
    # The "not logged in" response is a tiny page whose only job is alert() + redirect.
    return "請登入會員" in html and len(html) < 2000


def _domain_matches(cookie_domain: str) -> bool:
    d = cookie_domain.lstrip(".")
    return HOST == d or HOST.endswith("." + d)


def interactive_login(username: str, password: str, timeout_s: int = 300) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(LOGIN_URL)
        page.fill("#Username_login", username)
        page.fill("#Password_login", password)
        page.focus("#UserInputNo_login")

        # Leaving the login page is not proof of login (the SSO button leaves it too),
        # so poll until the site itself answers a members-only page.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            page.wait_for_timeout(1500)  # raises if the user closed the window
            if urlparse(page.url).hostname != HOST or "U=login" in page.url:
                continue
            if not is_logged_out_page(ctx.request.get(BASE + PROBE_PATH).text()):
                break
        else:
            raise TimeoutError("5 分鐘內未完成登入")

        cookies = [c for c in ctx.cookies() if _domain_matches(c["domain"])]
        ua = page.evaluate("navigator.userAgent")
        SESSION_FILE.write_text(json.dumps({"user_agent": ua, "cookies": cookies}, ensure_ascii=False, indent=2), encoding="utf-8")
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
