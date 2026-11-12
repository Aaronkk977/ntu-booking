"""Where the data lives, and the service-wide settings in data/config.json.

data/                      (chmod 700; everything here is private to the account running the server)
  config.json              allowed accounts, admins, scan account, Discord bot token, public URL, scan interval
  secret.key               Fernet key for the stored passwords (unless NTU_BOOKING_KEY is set)
  web_sessions.json        logins to this web app (cookie token -> account)
  search_cache.json        latest walk per venue, shared by everybody
  history_stats.json       history statistics, shared
  snapshots/               pages saved for debugging
  users/<account>/         one folder per person: credentials.json, session.json, robot.json, hits.json, orders.json
"""
import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("NTU_BOOKING_DATA") or ROOT / "data")
USERS = DATA / "users"
SNAPSHOT_DIR = DATA / "snapshots"
CONFIG_FILE = DATA / "config.json"

# The 計中 account name, e.g. b12345678 (lowercase letters, digits, a few separators).
ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

DEFAULTS = {
    "allowed": [],          # 計中 accounts that may log in to this web app
    "admins": [],           # may change the scan interval, run history, manage the allow list
    "scan_account": None,   # whose login the shared scanner uses; None = the first admin
    "discord_token": "",    # bot token (Bot ...), for direct messages
    "public_url": "",       # e.g. https://booking.example.com, used for links and the Origin check
    "interval_min": 5,      # shared robot scan interval
}

_lock = threading.Lock()


def init_dirs() -> None:
    """Create data/ readable by this account only. Files created afterwards are private too (umask)."""
    if hasattr(os, "umask"):
        os.umask(0o077)
    for d in (DATA, USERS, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(DATA, 0o700)


def normalize_account(raw: str) -> str | None:
    """'B12345678@ntu.edu.tw' or 'b12345678' -> 'b12345678'; None if it isn't an NTU account name."""
    name, _, domain = (raw or "").strip().lower().partition("@")
    if domain and domain != "ntu.edu.tw":
        return None
    return name if ACCOUNT_RE.match(name) else None


def load() -> dict:
    with _lock:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    cfg = {**DEFAULTS, **data}
    if os.environ.get("PUBLIC_URL"):
        cfg["public_url"] = os.environ["PUBLIC_URL"]
    if os.environ.get("DISCORD_TOKEN"):
        cfg["discord_token"] = os.environ["DISCORD_TOKEN"]
    cfg["public_url"] = cfg["public_url"].rstrip("/")
    return cfg


def save(cfg: dict) -> None:
    keep = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)


def is_allowed(account: str) -> bool:
    cfg = load()
    return account in cfg["allowed"] or account in cfg["admins"]


def is_admin(account: str) -> bool:
    return account in load()["admins"]


def scan_account() -> str | None:
    cfg = load()
    return cfg["scan_account"] or (cfg["admins"][0] if cfg["admins"] else None)


def user_dir(account: str) -> Path:
    if not ACCOUNT_RE.match(account):
        raise ValueError("bad account name")
    return USERS / account


def users() -> list[str]:
    """Accounts that have logged in at least once (have a folder) and are still allowed."""
    if not USERS.exists():
        return []
    return sorted(p.name for p in USERS.iterdir() if p.is_dir() and is_allowed(p.name))


def write_json(path: Path, data) -> None:
    """Write through a temp file, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
