"""Store NTU rent-system credentials locally.

The username goes in plain JSON. The password is encrypted with Windows DPAPI (CryptProtectData) on
Windows, so only the current Windows user on this machine can decrypt it, and kept in the login Keychain
on macOS. Nothing is sent over the network.
"""
import base64
import json
import sys
from pathlib import Path

CRED_FILE = Path(__file__).with_name("credentials.json")
KEYCHAIN_SERVICE = "ntu-booking"

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

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


def _keychain(action: str, username: str, password: str | None = None) -> str | None:
    """get / set / delete the Keychain item of `username`. Keychain failures come out as OSError, like DPAPI's."""
    try:
        import keyring
        import keyring.errors

        if action == "get":
            return keyring.get_password(KEYCHAIN_SERVICE, username)
        if action == "set":
            keyring.set_password(KEYCHAIN_SERVICE, username, password)
        else:
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, username)
            except keyring.errors.PasswordDeleteError:  # not there
                pass
        return None
    except ImportError as e:
        raise OSError("keyring 套件沒有安裝") from e
    except keyring.errors.KeyringError as e:
        raise OSError(f"鑰匙圈存取失敗：{e}") from e


def _read() -> dict | None:
    return json.loads(CRED_FILE.read_text(encoding="utf-8")) if CRED_FILE.exists() else None


def save(username: str, password: str) -> None:
    old = _read()
    if sys.platform == "win32":
        entry = {"password_dpapi": base64.b64encode(_protect(password.encode("utf-8"))).decode("ascii")}
    else:
        _keychain("set", username, password)
        entry = {"password_keychain": True}
        if old and old.get("password_keychain") and old["username"] != username:
            _keychain("delete", old["username"])
    CRED_FILE.write_text(json.dumps({"username": username, **entry}, ensure_ascii=False, indent=2), encoding="utf-8")


def load() -> tuple[str, str] | None:
    data = _read()
    if data is None:
        return None
    if "password_dpapi" in data:
        if sys.platform != "win32":
            raise OSError("密碼是在 Windows 上儲存的")
        password = _unprotect(base64.b64decode(data["password_dpapi"])).decode("utf-8")
    else:
        password = _keychain("get", data["username"])
        if password is None:
            raise OSError("鑰匙圈裡找不到密碼")
    return data["username"], password


def load_or_none() -> tuple[str, str] | None:
    """Like load(), but None when the saved password can't be read (other machine, user or OS)."""
    try:
        return load()
    except OSError:
        return None


def delete() -> None:
    data = _read()
    if data and data.get("password_keychain"):
        try:
            _keychain("delete", data["username"])
        except OSError:  # still drop the file, so the account can be entered again
            pass
    CRED_FILE.unlink(missing_ok=True)
