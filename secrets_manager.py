# secrets_manager.py
import base64
import ctypes
import json
import os
import re
import secrets
import stat
import subprocess
from ctypes import wintypes
from pathlib import Path

APP_NAME = "3D Open Dock U"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _secret_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        root = Path(base) / APP_NAME / "secure"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME / "secure"
    return root


def get_secret_file_path() -> str:
    return str(_secret_root() / "secrets.json")


def _hide_path(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(["attrib", "+h", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _restrict_path(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

    if os.name == "nt" and path.exists():
        user = os.environ.get("USERNAME")
        if user:
            try:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass


def secure_file(path: str | Path) -> None:
    target = Path(path)
    if target.exists():
        _hide_path(target)
        _restrict_path(target)


def secure_directory(path: str | Path) -> None:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    _hide_path(target)
    _restrict_path(target)


def _to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buf = _to_blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "3D Open Dock U Secret Store",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buf = _to_blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _encode_store(data: dict) -> dict:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if os.name == "nt":
        encrypted = _dpapi_protect(raw)
        return {
            "version": 2,
            "protection": "windows-dpapi-current-user",
            "payload": base64.b64encode(encrypted).decode("ascii"),
        }
    return {
        "version": 2,
        "protection": "local-permissions-only",
        "payload": base64.b64encode(raw).decode("ascii"),
    }


def _decode_store(wrapper: dict) -> dict:
    if wrapper.get("protection") == "windows-dpapi-current-user":
        encrypted = base64.b64decode(wrapper.get("payload", ""))
        raw = _dpapi_unprotect(encrypted)
        loaded = json.loads(raw.decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {"version": 1, "secrets": {}}
    if wrapper.get("protection") == "local-permissions-only":
        raw = base64.b64decode(wrapper.get("payload", ""))
        loaded = json.loads(raw.decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {"version": 1, "secrets": {}}
    return wrapper


def secret_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("._-") or "default"


class SecretStore:
    def __init__(self, path: str | None = None):
        self.root = Path(path).parent if path else _secret_root()
        self.path = Path(path) if path else self.root / "secrets.json"
        self.data = {"version": 1, "secrets": {}}
        self.load()

    def load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        secure_directory(self.root)
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(_decode_store(loaded))
                    self.data.setdefault("secrets", {})
                    if loaded.get("protection") != "windows-dpapi-current-user" and os.name == "nt":
                        self.save()
            except Exception:
                self.data = {"version": 1, "secrets": {}}
        secure_file(self.path)

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(_encode_store(self.data), f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)
        secure_directory(self.root)
        secure_file(self.path)

    def get(self, key: str, default=None):
        return self.data.get("secrets", {}).get(key, default)

    def set(self, key: str, value, save: bool = False):
        self.data.setdefault("secrets", {})[key] = value
        if save:
            self.save()
        return value

    def delete(self, key: str, save: bool = False) -> None:
        self.data.setdefault("secrets", {}).pop(key, None)
        if save:
            self.save()

    def get_or_create(self, key: str, factory, legacy=None):
        current = self.get(key)
        if current not in (None, ""):
            return current
        value = legacy if legacy not in (None, "") else factory()
        return self.set(key, value)


def token(length: int = 32) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hex_token(length: int = 64) -> str:
    alphabet = "ABCDEF0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
