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
"""
import getpass
import sys

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
        if changed and "--notify" in args:
            _notify_everybody(f"🔗 場地預約的新網址：{cfg['public_url']}")
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


def _end_web_sessions(names: set[str]) -> None:
    """Log these accounts out of the web app (same file as server.end_web_sessions)."""
    import json

    f = config.DATA / "web_sessions.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        config.write_json(f, {k: v for k, v in data.items() if v["account"] not in names})


def _notify_everybody(text: str) -> None:
    import notify
    import robot
    import session

    for name in config.users():
        did = robot.load_config(session.account(name)).get("discord_id")
        if did:
            try:
                notify.send_now(did, text, ("打開網站", config.load()["public_url"] + "/"))
            except Exception as e:
                print(f"通知 {name} 失敗：{e}")


if __name__ == "__main__":
    main(sys.argv[1:])
