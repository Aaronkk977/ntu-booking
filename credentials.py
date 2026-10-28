"""Store NTU rent-system credentials locally.

The username goes in plain JSON; the password is encrypted with Windows
DPAPI (CryptProtectData), so only the current Windows user on this machine
can decrypt it. Nothing is sent over the network.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
from pathlib import Path

CRED_FILE = Path(__file__).with_name("credentials.json")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _Blob) -> bytes:
    data = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return data


def _protect(data: bytes) -> bytes:
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_to_blob(data)), None, None, None, None, 0, ctypes.byref(out)
    ):
        raise ctypes.WinError()
    return _from_blob(out)


def _unprotect(data: bytes) -> bytes:
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_to_blob(data)), None, None, None, None, 0, ctypes.byref(out)
    ):
        raise ctypes.WinError()
    return _from_blob(out)


def save(username: str, password: str) -> None:
    enc = base64.b64encode(_protect(password.encode("utf-8"))).decode("ascii")
    CRED_FILE.write_text(
        json.dumps({"username": username, "password_dpapi": enc}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load() -> tuple[str, str] | None:
    if not CRED_FILE.exists():
        return None
    data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    password = _unprotect(base64.b64decode(data["password_dpapi"])).decode("utf-8")
    return data["username"], password


def delete() -> None:
    CRED_FILE.unlink(missing_ok=True)
