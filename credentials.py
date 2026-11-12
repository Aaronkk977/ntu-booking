"""Store each person's 計中 password, encrypted, in data/users/<account>/credentials.json.

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


def _key() -> Fernet:
    global _fernet
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
    return _fernet


def _file(account: str):
    return config.user_dir(account) / "credentials.json"


def save(account: str, password: str) -> None:
    token = _key().encrypt(password.encode("utf-8")).decode("ascii")
    config.write_json(_file(account), {"account": account, "password": token})


def load(account: str) -> str | None:
    """The stored password, or None when there is none or it can't be decrypted (key changed)."""
    f = _file(account)
    if not f.exists():
        return None
    try:
        return _key().decrypt(json.loads(f.read_text(encoding="utf-8"))["password"].encode()).decode("utf-8")
    except (InvalidToken, KeyError, ValueError):
        return None


def delete(account: str) -> None:
    _file(account).unlink(missing_ok=True)
