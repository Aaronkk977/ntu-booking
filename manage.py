"""Service settings from the command line (edits data/config.json; the running server picks them up).

  python manage.py show
  python manage.py set-admin b12345678          the admin; also allowed, and the default scan account
  python manage.py allow b12345678 r13922000    let friends log in
  python manage.py deny r13922000               and take it back (logs them out)
  python manage.py set-scan-account b12345678   whose login the shared robot scans with
  python manage.py set-discord-token            asks for the bot token (not echoed)
  python manage.py set-public-url https://x.trycloudflare.com [--notify]
                                                --notify DMs everybody the new address
  python manage.py discord-setup                register /link, /unlink and the button endpoint with
                                                Discord (done by the two commands above too)
  python manage.py write-redirect               rewrite the fixed-address page (REDIRECT_FILE) now

A quick tunnel's address changes on every restart. With REDIRECT_FILE set (deploy/env), e.g. to
~/htdocs/nb/index.html on the CSIE workstations, a tiny static page there forwards to the current address,
so https://www.csie.ntu.edu.tw/~<you>/nb/ (REDIRECT_URL) keeps working as a bookmark.
"""
import getpass
import html
import json
import os
import sys
from pathlib import Path

import config


def _names(args: list[str]) -> list[str]:
    out = []
    for a in args:
        n = config.normalize_account(a)
        if not n:
            sys.exit(f"不是計中帳號：{a}")
        out.append(n)
    if not out:
        sys.exit("請給至少一個計中帳號")
    return out


def main(argv: list[str]) -> None:
    config.init_dirs()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    cfg = config.load()
    if cmd == "show":
        shown = {**cfg, "discord_token": "(已設定)" if cfg["discord_token"] else ""}
        for k, v in shown.items():
            print(f"{k:14} {v}")
        print(f"{'users':14} {config.users()}")
        return
    if cmd == "set-admin":
        names = _names(args)
        cfg["admins"] = sorted(set(cfg["admins"]) | set(names))
    elif cmd == "allow":
        cfg["allowed"] = sorted(set(cfg["allowed"]) | set(_names(args)))
    elif cmd == "deny":
        names = set(_names(args))
        cfg["allowed"] = [a for a in cfg["allowed"] if a not in names]
        cfg["admins"] = [a for a in cfg["admins"] if a not in names]
        _end_web_sessions(names)
    elif cmd == "set-scan-account":
        cfg["scan_account"] = _names(args)[0]
    elif cmd == "set-discord-token":
        cfg["discord_token"] = (args[0] if args else getpass.getpass("Discord bot token: ")).strip()
        config.save(cfg)
        print("已更新。")
        _discord_setup()
        return
    elif cmd == "discord-setup":
        _discord_setup()
        return
    elif cmd == "set-public-url":
        if not args or not args[0].startswith(("https://", "http://")):
            sys.exit("請給完整網址，例如 https://booking.example.com")
        changed = cfg["public_url"] != args[0].rstrip("/")
        cfg["public_url"] = args[0].rstrip("/")
        config.save(cfg)
        print(f"public_url = {cfg['public_url']}")
        _discord_setup()  # Discord must send /link and button presses to the new address
        _write_redirect(cfg["public_url"])
        if changed and "--notify" in args:
            fixed = os.environ.get("REDIRECT_URL", "").strip()
            if fixed:
                _notify_everybody(f"🔗 伺服器重新啟動過，網站要重新登入一次：打開 {fixed} 登入，或在 Discord 打 /login。", fixed)
            else:
                _notify_everybody(f"🔗 場地預約的新網址：{cfg['public_url']}")
        return
    elif cmd == "write-redirect":
        _write_redirect(cfg["public_url"])
        return
    else:
        sys.exit(f"不認得的指令：{cmd}\n{__doc__}")
    config.save(cfg)
    print("已更新。")


def _discord_setup() -> None:
    cfg = config.load()
    if not cfg["discord_token"] or not cfg["public_url"].startswith("https://"):
        return
    import notify

    try:
        print(notify.setup_interactions(cfg["public_url"]))
    except Exception as e:
        print(f"Discord 設定沒有成功：{e}")


REDIRECT_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="Cache-Control" content="no-cache">
<title>臺大場地預約</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#ddd;display:grid;place-items:center;min-height:90vh;margin:0}}a{{color:#8ab4ff}}</style>
<script>
// The address in this page can be stale in the phone's cache, so ask for url.txt fresh first.
(function () {{
  var fallback = {url_js};
  function go(u) {{ location.replace(u.replace(/\/+$/, "") + "/" + location.hash); }}
  fetch("url.txt?t=" + Date.now(), {{ cache: "no-store" }})
    .then(function (r) {{ return r.ok ? r.text() : fallback; }})
    .then(function (u) {{ u = u.trim(); go(/^https:\/\//.test(u) ? u : fallback); }})
    .catch(function () {{ go(fallback); }});
}})();
</script>
<noscript><meta http-equiv="refresh" content="0; url={url_attr}/"></noscript>
</head><body><p>正在打開場地預約… 沒有自動跳轉的話請按 <a href="{url_attr}/">這裡</a>。</p></body></html>
"""


def _write_redirect(url: str) -> None:
    """Point the fixed-address page (REDIRECT_FILE, if set) at the current public address."""
    target = os.environ.get("REDIRECT_FILE", "").strip()
    if not target or not url.startswith("https://"):
        return
    page = Path(os.path.expanduser(target))
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(REDIRECT_PAGE.format(url_js=json.dumps(url), url_attr=html.escape(url)), encoding="utf-8")
    (page.parent / "url.txt").write_text(url + "\n", encoding="utf-8")
    for f in (page, page.parent / "url.txt"):
        f.chmod(0o644)  # the department's web server reads it as another user
    page.parent.chmod(0o755)
    print(f"轉址頁已更新：{page} → {url}")


def _end_web_sessions(names: set[str]) -> None:
    """Log these accounts out of the web app (same file as server.end_web_sessions)."""
    import json

    f = config.DATA / "web_sessions.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        config.write_json(f, {k: v for k, v in data.items() if v["account"] not in names})


def _notify_everybody(text: str, open_url: str = "") -> None:
    import notify
    import robot
    import session

    for name in config.users():
        did = robot.load_config(session.account(name)).get("discord_id")
        if did:
            try:
                notify.send_now(did, text, ("打開網站", open_url or config.load()["public_url"] + "/"))
            except Exception as e:
                print(f"通知 {name} 失敗：{e}")


if __name__ == "__main__":
    main(sys.argv[1:])
