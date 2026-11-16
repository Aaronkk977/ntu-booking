"""Store each person's linked 計中 account and its password, encrypted, in data/users/<account>/credentials.json.

The password may be left out ("don't keep my password"): then the server can use the school login only
until it expires (an hour or two) and cannot log in again on its own.

The password is encrypted with Fernet (AES + HMAC). The key comes from the NTU_BOOKING_KEY environment
variable, or data/secret.key (created on first use, readable only by the account running the server).
Anyone who can read both the key and the data folder (e.g. the machine's root) can decrypt them; the
people using the service should know that.
"""
import json
import os

from cryptography.fernet import Fernet, InvalidToken

import config

KEY_FILE = config.DATA / "secret.key"
_fernet: Fernet | None = None
_raw_key: bytes = b""


def _key() -> Fernet:
    global _fernet, _raw_key
    if _fernet is None:
        key = os.environ.get("NTU_BOOKING_KEY", "").encode()
        if not key:
            if not KEY_FILE.exists():
                config.init_dirs()
                KEY_FILE.write_bytes(Fernet.generate_key())
                if os.name == "posix":
                    os.chmod(KEY_FILE, 0o600)
            key = KEY_FILE.read_bytes().strip()
        _fernet = Fernet(key)
        _raw_key = key
    return _fernet


def sign(text: str) -> str:
    """A short HMAC of `text` with the server's secret, for links that must work without a web login
    (e.g. 「線上付款」 in a Discord DM): only the server can make one, and it fits one order of one person."""
    import hashlib
    import hmac
    _key()
    return hmac.new(b"link:" + _raw_key, text.encode(), hashlib.sha256).hexdigest()[:24]


def _file(account: str):
    return config.user_dir(account) / "credentials.json"


def save(account: str, school: str, password: str | None) -> None:
    """Link `school` (a 計中 account) to `account`; keep the password only when given."""
    token = _key().encrypt(password.encode("utf-8")).decode("ascii") if password else None
    config.write_json(_file(account), {"school": school, "password": token})


def _read(account: str) -> dict | None:
    f = _file(account)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def school(account: str) -> str | None:
    """The 計中 account linked to this site account."""
    data = _read(account)
    if data:
        return data.get("school") or data.get("account")  # "account": files from before Discord login
    return None if config.is_discord_account(account) else account


def load(account: str) -> str | None:
    """The stored password, or None when there is none or it can't be decrypted (key changed)."""
    data = _read(account)
    if not data or not data.get("password"):
        return None
    try:
        return _key().decrypt(data["password"].encode()).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def delete(account: str) -> None:
    _file(account).unlink(missing_ok=True)
