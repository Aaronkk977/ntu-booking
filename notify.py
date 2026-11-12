"""Discord direct messages from the bot, through Discord's REST API only (no gateway connection to keep).

Setup: create an application at https://discord.com/developers/applications, add a Bot, copy its token
into data/config.json ("discord_token", or `python manage.py set-discord-token`), and invite the bot to a
small server everybody is in (a bot can only DM people who share a server with it). Each person puts their
Discord user ID (Settings → Advanced → Developer Mode, then right-click yourself → Copy User ID) in the
web app's settings.

Messages go out on a background thread, so a slow or failing Discord never holds up the robot.
"""
import json
import queue
import threading
import urllib.error
import urllib.request

import config

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0) ntu-booking"

_dm_channels: dict[str, str] = {}
_queue: queue.Queue = queue.Queue()


class NotConfigured(Exception):
    pass


def _call(method: str, path: str, body: dict | None = None) -> dict:
    token = config.load()["discord_token"]
    if not token:
        raise NotConfigured("伺服器還沒設定 Discord bot")
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise OSError(f"Discord 回應 {e.code}：{detail}") from e


def send_now(discord_id: str, text: str, link: tuple[str, str] | None = None) -> None:
    """DM `discord_id` right away. `link` = (button label, url) adds a link button. Raises on failure."""
    if not discord_id:
        raise NotConfigured("還沒設定 Discord 使用者 ID")
    channel = _dm_channels.get(discord_id)
    if not channel:
        channel = _dm_channels[discord_id] = _call("POST", "/users/@me/channels", {"recipient_id": discord_id})["id"]
    msg = {"content": text[:1900], "allowed_mentions": {"parse": []}}
    if link and link[1].startswith("https://"):  # Discord only accepts http(s) URLs; localhost testing has none
        msg["components"] = [{"type": 1, "components": [{"type": 2, "style": 5, "label": link[0][:80], "url": link[1]}]}]
    elif link:
        msg["content"] = f"{msg['content']}\n{link[1]}"
    _call("POST", f"/channels/{channel}/messages", msg)


def send(discord_id: str, text: str, link: tuple[str, str] | None = None) -> None:
    """Queue a DM; does nothing when the person or the server has no Discord set up."""
    if discord_id and config.load()["discord_token"]:
        _queue.put((discord_id, text, link))


def url(fragment: str = "") -> str:
    base = config.load()["public_url"] or "http://127.0.0.1:8765"
    return f"{base}/{fragment}"


def _worker(log) -> None:
    while True:
        discord_id, text, link = _queue.get()
        try:
            send_now(discord_id, text, link)
        except Exception as e:
            log("warn", f"Discord 通知沒有送出：{e}")


def start(log) -> None:
    threading.Thread(target=_worker, args=(log,), daemon=True, name="discord").start()
