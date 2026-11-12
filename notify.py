"""Discord direct messages and interactions, through Discord's REST API only (no gateway connection to keep).

Setup: create an application at https://discord.com/developers/applications, add a Bot, copy its token
into data/config.json ("discord_token", or `python manage.py set-discord-token`), and invite the bot to a
small server everybody is in (a bot can only DM people who share a server with it).

People link their Discord account by typing /link to the bot (or by pasting their user ID in the web
app). /link, /unlink and the Confirm / Cancel buttons on booking messages reach the server as HTTP
"interactions" at <public url>/discord/interactions. setup_interactions() registers the commands and
points the application's Interactions Endpoint URL there whenever the public address changes. Every
request is checked against the application's Ed25519 public key.

Messages go out on a background thread, so a slow or failing Discord never holds up the robot.
"""
import json
import queue
import threading
import time
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import config

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0) ntu-booking"

COMMANDS = [
    {"name": "login", "type": 1, "description": "Get a one-time link that logs you in to the booking site",
     "contexts": [0, 1, 2]},
    {"name": "link", "type": 1, "description": "Link this Discord account to your booking-site account",
     "contexts": [0, 1, 2]},
    {"name": "unlink", "type": 1, "description": "Stop booking notifications to this Discord account",
     "contexts": [0, 1, 2]},
]

_dm_channels: dict[str, str] = {}
_queue: queue.Queue = queue.Queue()
_app: dict = {}


class NotConfigured(Exception):
    pass


def _call(method: str, path: str, body=None, auth: bool = True) -> dict:
    token = config.load()["discord_token"]
    if auth and not token:
        raise NotConfigured("伺服器還沒設定 Discord bot")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if auth:
        headers["Authorization"] = f"Bot {token}"
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise OSError(f"Discord 回應 {e.code}：{detail}") from e


# ---------- components ----------

def button(label: str, custom_id: str, style: int = 2) -> dict:
    """A button that comes back to the server as an interaction (style 1 blue, 2 grey, 3 green, 4 red)."""
    return {"type": 2, "style": style, "label": label[:80], "custom_id": custom_id[:100]}


def link_button(label: str, url: str) -> dict:
    return {"type": 2, "style": 5, "label": label[:80], "url": url}


def row(buttons: list[dict]) -> list[dict]:
    return [{"type": 1, "components": buttons[:5]}] if buttons else []


# ---------- interactions ----------

def application() -> dict:
    """The bot's application (id, verify_key, ...), fetched once per token."""
    token = config.load()["discord_token"]
    if _app.get("_token") != token:
        info = _call("GET", "/applications/@me")
        _app.clear()
        _app.update(info, _token=token)
    return _app


def verify(signature: str, timestamp: str, body: bytes) -> bool:
    """True when an interaction request really comes from Discord (signed with the application's key)."""
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(application()["verify_key"]))
        key.verify(bytes.fromhex(signature), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError, KeyError, OSError, NotConfigured):
        return False


def edit_original(app_id: str, interaction_token: str, text: str) -> None:
    """Replace the message an interaction answered with type 7 (e.g. 「正在取消…」) with the outcome.
    The interaction token (valid 15 minutes) is the credential here, not the bot token."""
    _call("PATCH", f"/webhooks/{app_id}/{interaction_token}/messages/@original",
          {"content": text[:1900], "components": [], "allowed_mentions": {"parse": []}}, auth=False)


def setup_interactions(public_url: str, tries: int = 6) -> str:
    """Register /link and /unlink, and point the Interactions Endpoint URL at this server. Discord checks the
    URL right away (the server must answer through the tunnel), so a fresh quick tunnel gets a few tries."""
    if not public_url.startswith("https://"):
        raise OSError("Discord 的互動端點只接受 https 網址")
    app = application()
    _call("PUT", f"/applications/{app['id']}/commands", COMMANDS)
    last = None
    for i in range(tries):
        try:
            _call("PATCH", "/applications/@me", {"interactions_endpoint_url": public_url + "/discord/interactions"})
            return f"已註冊 /login、/link、/unlink，互動端點：{public_url}/discord/interactions"
        except OSError as e:  # the new address isn't reachable yet
            last = e
            time.sleep(5 * (i + 1))
    raise OSError(f"Discord 不接受互動端點（{last}）")


# ---------- direct messages ----------

def send_now(discord_id: str, text: str, link: tuple[str, str] | None = None, buttons: list[dict] | None = None) -> None:
    """DM `discord_id` right away. `link` = (label, url) adds a link button; `buttons` come first. Raises."""
    if not discord_id:
        raise NotConfigured("還沒連結 Discord")
    channel = _dm_channels.get(discord_id)
    if not channel:
        channel = _dm_channels[discord_id] = _call("POST", "/users/@me/channels", {"recipient_id": discord_id})["id"]
    msg = {"content": text[:1900], "allowed_mentions": {"parse": []}}
    items = list(buttons or [])
    if link and link[1].startswith("https://"):  # link buttons need https; localhost testing has none
        items.append(link_button(*link))
    elif link:
        msg["content"] = f"{msg['content']}\n{link[1]}"
    if items:
        msg["components"] = row(items)
    _call("POST", f"/channels/{channel}/messages", msg)


def send(discord_id: str, text: str, link: tuple[str, str] | None = None, buttons: list[dict] | None = None) -> None:
    """Queue a DM; does nothing when the person or the server has no Discord set up."""
    if discord_id and config.load()["discord_token"]:
        _queue.put((discord_id, text, link, buttons))


def url(fragment: str = "") -> str:
    base = config.load()["public_url"] or "http://127.0.0.1:8765"
    return f"{base}/{fragment}"


def _worker(log) -> None:
    while True:
        discord_id, text, link, buttons = _queue.get()
        try:
            send_now(discord_id, text, link, buttons)
        except Exception as e:
            log("warn", f"Discord 通知沒有送出：{e}")


def start(log) -> None:
    threading.Thread(target=_worker, args=(log,), daemon=True, name="discord").start()
