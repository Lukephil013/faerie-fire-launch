"""Field-level encryption for sensitive content.

Encrypts the bulky sensitive fields (OCR text, memory values) at rest while
leaving structural metadata (timestamps, app names, window titles) readable so
session grouping and queries still work.

An explicit LIVINGPC_DB_KEY passphrase takes precedence. On Windows, a random
key protected to the current user with DPAPI is created automatically when no
explicit key exists. A persistent random salt is stored next to that key.
Behavior is safe and gradual:
  * Windows default       -> automatic per-user encryption.
  * Explicit key set      -> new writes use that passphrase (prefixed 'enc::').
  * Auto disabled/no DPAPI -> plaintext compatibility mode.
  * dec() on plaintext    -> returned unchanged (so old rows still read).
  * dec() with no/wrong key on encrypted text -> a placeholder string.
  * encryption failure while enabled -> the write fails closed.

IMPORTANT: if you change the passphrase or lose the salt file, previously
encrypted data cannot be recovered. Back up secret.salt and remember the key.
"""
from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

_PREFIX = "enc::"
_AUTO_KEY_FILE = os.environ.get("LIVINGPC_KEY_FILE", os.path.join("data", "secret.key"))


class EncryptionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    incoming = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = _DataBlob()
    function = (ctypes.windll.crypt32.CryptUnprotectData if decrypt
                else ctypes.windll.crypt32.CryptProtectData)
    ok = function(ctypes.byref(incoming), None, None, None, None, 1,
                  ctypes.byref(outgoing))
    if not ok:
        raise EncryptionError("Windows could not protect the database key")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


@lru_cache(maxsize=1)
def _automatic_passphrase() -> str | None:
    if os.environ.get("LIVINGPC_AUTO_ENCRYPTION", "1").lower() in {"0", "false", "no"}:
        return None
    if os.name != "nt":
        return None
    path = os.path.abspath(_AUTO_KEY_FILE)
    try:
        if os.path.exists(path):
            with open(path, "rb") as handle:
                secret = _dpapi(handle.read(), decrypt=True)
        else:
            secret = os.urandom(32)
            protected = _dpapi(secret, decrypt=False)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "xb") as handle:
                handle.write(protected)
        return base64.urlsafe_b64encode(secret).decode()
    except EncryptionError:
        raise
    except Exception as error:
        raise EncryptionError("automatic at-rest encryption initialization failed") from error


def _passphrase() -> str | None:
    return os.environ.get("LIVINGPC_DB_KEY") or _automatic_passphrase()


def enabled() -> bool:
    """True when a passphrase is configured (encryption active)."""
    return _passphrase() is not None


def dpapi_available() -> bool:
    """True when Windows DPAPI can be used to protect secrets on this machine."""
    return os.name == "nt"


def protect_secret(data: bytes) -> bytes:
    """Protect arbitrary secret bytes (e.g. a user-entered API key) to the current
    Windows user with DPAPI. Raises EncryptionError off Windows or on failure —
    callers needing a non-Windows fallback should check dpapi_available() first."""
    if not dpapi_available():
        raise EncryptionError("DPAPI is only available on Windows")
    return _dpapi(data, decrypt=False)


def unprotect_secret(data: bytes) -> bytes:
    """Reverse of protect_secret()."""
    if not dpapi_available():
        raise EncryptionError("DPAPI is only available on Windows")
    return _dpapi(data, decrypt=True)


def automatic_key_path() -> str:
    return os.path.abspath(_AUTO_KEY_FILE)


def salt_path() -> str:
    explicit = os.environ.get("LIVINGPC_SALT_FILE")
    legacy = os.path.abspath("secret.salt")
    return os.path.abspath(explicit) if explicit else (
        legacy if os.path.exists(legacy) else
        os.path.join(os.path.dirname(automatic_key_path()), "secret.salt")
    )


def _salt_b64() -> str:
    salt_file = salt_path()
    if os.path.exists(salt_file):
        with open(salt_file, "r") as f:
            return f.read().strip()
    salt = base64.b64encode(os.urandom(16)).decode()
    os.makedirs(os.path.dirname(salt_file), exist_ok=True)
    with open(salt_file, "w") as f:
        f.write(salt)
    return salt


@lru_cache(maxsize=8)
def _fernet(passphrase: str, salt_b64: str):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=base64.b64decode(salt_b64),
        iterations=200_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))
    return Fernet(key)


def enc(text: str | None) -> str | None:
    """Encrypt text if a key is set; otherwise return it unchanged."""
    if not text or not enabled():
        return text
    try:
        f = _fernet(_passphrase(), _salt_b64())
        return _PREFIX + f.encrypt(text.encode()).decode()
    except Exception as error:
        raise EncryptionError("refusing to write sensitive text as plaintext") from error


def enc_bytes(data: bytes) -> bytes:
    """Encrypt binary capture data when at-rest encryption is active."""
    if not data or not enabled():
        return data
    try:
        return _fernet(_passphrase(), _salt_b64()).encrypt(data)
    except Exception as error:
        raise EncryptionError("refusing to write sensitive bytes as plaintext") from error


def dec(text):
    """Decrypt if the value is encrypted; pass plaintext through untouched."""
    if not isinstance(text, str) or not text.startswith(_PREFIX):
        return text
    passphrase = _passphrase()
    if not passphrase:
        return "[encrypted — set LIVINGPC_DB_KEY to read]"
    try:
        f = _fernet(passphrase, _salt_b64())
        return f.decrypt(text[len(_PREFIX):].encode()).decode()
    except Exception:
        return "[decryption failed — wrong key?]"


def is_encrypted(text) -> bool:
    return isinstance(text, str) and text.startswith(_PREFIX)
