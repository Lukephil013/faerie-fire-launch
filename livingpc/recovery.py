"""Portable, authenticated Faerie Fire backup containers.

The local repository key is random and protected with Windows DPAPI.  A copy
of that key is also wrapped by the user's recovery passphrase and embedded in
every ``.ffbackup`` header.  Bundle payloads are gzip-compressed and encrypted
as a stream with AES-256-GCM; the complete, bounded header is authenticated as
additional data.

This module deliberately contains no logging.  Callers may report exception
types or their privacy-safe messages, but must never log passphrases, keys, or
decoded database-key material.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import BinaryIO, Iterator, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import crypto


FORMAT_VERSION = 1
MAGIC = b"FFBACKUP"
FORMAT_NAME = "faerie-fire-backup"
PAYLOAD_FORMAT = "ffarchive-1+gzip"
PAYLOAD_MAGIC = b"FFARCH01"

SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1

MAX_HEADER_SIZE = 64 * 1024
MAX_LOCAL_KEY_FILE_SIZE = 128 * 1024
MAX_DATABASE_KEY_EXPORT_SIZE = 16 * 1024
MAX_BUNDLE_SIZE = 512 * 1024 ** 3
MAX_SOURCE_SIZE = 512 * 1024 ** 3
MAX_MEMBER_SIZE = 64 * 1024 ** 3
MAX_MEMBERS = 100_000
MAX_PATH_BYTES = 4096
MAX_PATH_DEPTH = 64
STREAM_CHUNK_SIZE = 1024 * 1024
GCM_TAG_SIZE = 16

_PREAMBLE = struct.Struct(">8sBI")
_ENTRY = struct.Struct(">BIQqI")
_ENTRY_END = 0
_ENTRY_FILE = 1
_ENTRY_DIRECTORY = 2
_LOCAL_KEY_MAGIC = "faerie-fire-repository-key"
_DATABASE_KEY_MAGIC = "faerie-fire-database-key"
_KEY_WRAP_AAD_PREFIX = b"Faerie Fire repository key wrap v1\0"
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class RecoveryError(RuntimeError):
    """Base class for portable recovery failures."""


class BackupFormatError(RecoveryError):
    """The input is malformed, unsupported, or exceeds a safety bound."""


class BackupAuthenticationError(RecoveryError):
    """The passphrase is wrong or the authenticated bundle was modified."""


class RecoveryUnsupportedError(RecoveryError):
    """The current key mode or platform cannot provide portable recovery."""


class UnsafePathError(BackupFormatError):
    """An archive path is not portable and safe to extract."""


@dataclass(frozen=True)
class PassphraseWrapper:
    """Public metadata needed to recover a random repository key."""

    key_id: str
    salt: bytes
    nonce: bytes
    wrapped_key: bytes
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "kdf": "scrypt",
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "salt": _b64encode(self.salt),
            "cipher": "AES-256-GCM",
            "nonce": _b64encode(self.nonce),
            "wrapped_key": _b64encode(self.wrapped_key),
            "key_id": self.key_id,
        }


@dataclass(frozen=True)
class RepositoryKeyMaterial:
    """Locally unwrapped repository key and its portable wrapper."""

    key: bytes = field(repr=False)
    passphrase_wrapper: PassphraseWrapper


@dataclass(frozen=True)
class DatabaseKeyMaterial:
    """Raw automatic database secret and its original salt-file bytes."""

    secret: bytes = field(repr=False)
    salt_file: bytes = field(repr=False)


@dataclass(frozen=True)
class BundleInfo:
    path: str
    version: int
    bundle_id: str
    created_utc: str
    privacy_epoch: int
    source_size: int
    size_bytes: int
    header_meta: dict
    passphrase_wrapper: PassphraseWrapper

    @property
    def wrapper(self) -> PassphraseWrapper:
        """Short alias used by backup/restore engine integrations."""
        return self.passphrase_wrapper


@dataclass(frozen=True)
class _ParsedBundle:
    info: BundleInfo
    header_bytes: bytes
    aad: bytes
    payload_offset: int
    ciphertext_size: int
    content_nonce: bytes


@dataclass(frozen=True)
class _SourceEntry:
    path: str
    absolute_path: str
    is_directory: bool
    size: int
    mtime_ns: int
    mode: int
    device: int
    inode: int


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value, *, field_name: str, expected_length: int | None = None,
               max_length: int = MAX_HEADER_SIZE) -> bytes:
    if not isinstance(value, str) or len(value) > max_length * 2:
        raise BackupFormatError(f"invalid {field_name}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise BackupFormatError(f"invalid {field_name}") from error
    if len(decoded) > max_length or (
            expected_length is not None and len(decoded) != expected_length):
        raise BackupFormatError(f"invalid {field_name}")
    return decoded


def _json_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupFormatError("duplicate JSON field")
        result[key] = value
    return result


def _load_json(data: bytes, *, label: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_json_no_duplicates)
    except BackupFormatError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupFormatError(f"invalid {label}") from error
    if not isinstance(value, dict):
        raise BackupFormatError(f"invalid {label}")
    return value


def _canonical_json(value: Mapping) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as error:
        raise BackupFormatError("metadata is not JSON serializable") from error


def _validate_public_json(value, *, depth: int = 0, counter: list[int] | None = None):
    """Copy JSON-like public metadata while enforcing small structural bounds."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 2048 or depth > 8:
        raise BackupFormatError("header metadata is too complex")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise BackupFormatError("invalid header metadata number")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 4096 or "\x00" in value:
            raise BackupFormatError("header metadata string is too large")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise BackupFormatError("header metadata list is too large")
        return [_validate_public_json(item, depth=depth + 1, counter=counter)
                for item in value]
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise BackupFormatError("header metadata object is too large")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 128:
                raise BackupFormatError("invalid header metadata field")
            result[key] = _validate_public_json(item, depth=depth + 1,
                                                counter=counter)
        return result
    raise BackupFormatError("unsupported header metadata value")


def _validate_scrypt_parameters(n: int, r: int, p: int) -> None:
    if isinstance(n, bool) or not isinstance(n, int) or n < 2 ** 10 or n > 2 ** 20:
        raise BackupFormatError("unsupported Scrypt work factor")
    if n & (n - 1):
        raise BackupFormatError("unsupported Scrypt work factor")
    if isinstance(r, bool) or not isinstance(r, int) or not 1 <= r <= 16:
        raise BackupFormatError("unsupported Scrypt block size")
    if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 8:
        raise BackupFormatError("unsupported Scrypt parallelism")
    if 128 * n * r > 256 * 1024 ** 2:
        raise BackupFormatError("Scrypt memory requirement is too large")


def _passphrase_bytes(passphrase: str) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("a non-empty recovery passphrase is required")
    encoded = passphrase.encode("utf-8")
    if len(encoded) > 4096:
        raise ValueError("recovery passphrase is too long")
    return encoded


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def _key_wrap_aad(key_id: str, n: int, r: int, p: int, salt: bytes) -> bytes:
    return (_KEY_WRAP_AAD_PREFIX + key_id.encode("ascii") +
            struct.pack(">QII", n, r, p) + salt)


def _derive_wrap_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    _validate_scrypt_parameters(n, r, p)
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(
        _passphrase_bytes(passphrase))


def _wrap_repository_key(repository_key: bytes, passphrase: str, *,
                         n: int = SCRYPT_N, r: int = SCRYPT_R,
                         p: int = SCRYPT_P) -> PassphraseWrapper:
    if not isinstance(repository_key, bytes) or len(repository_key) != 32:
        raise ValueError("repository key must be exactly 32 bytes")
    _validate_scrypt_parameters(n, r, p)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key_id = _key_id(repository_key)
    wrapping_key = _derive_wrap_key(passphrase, salt, n, r, p)
    wrapped = AESGCM(wrapping_key).encrypt(
        nonce, repository_key, _key_wrap_aad(key_id, n, r, p, salt))
    return PassphraseWrapper(key_id=key_id, salt=salt, nonce=nonce,
                             wrapped_key=wrapped, n=n, r=r, p=p)


def _unwrap_repository_key(wrapper: PassphraseWrapper, passphrase: str) -> bytes:
    wrapping_key = _derive_wrap_key(passphrase, wrapper.salt,
                                    wrapper.n, wrapper.r, wrapper.p)
    try:
        key = AESGCM(wrapping_key).decrypt(
            wrapper.nonce, wrapper.wrapped_key,
            _key_wrap_aad(wrapper.key_id, wrapper.n, wrapper.r,
                          wrapper.p, wrapper.salt))
    except InvalidTag as error:
        raise BackupAuthenticationError(
            "wrong recovery passphrase or modified backup") from error
    if len(key) != 32 or not hmac.compare_digest(_key_id(key), wrapper.key_id):
        raise BackupAuthenticationError(
            "wrong recovery passphrase or modified backup")
    return key


def _wrapper_from_mapping(value) -> PassphraseWrapper:
    if isinstance(value, PassphraseWrapper):
        return value
    if not isinstance(value, Mapping):
        raise BackupFormatError("invalid passphrase wrapper")
    expected = {"version", "kdf", "n", "r", "p", "salt", "cipher",
                "nonce", "wrapped_key", "key_id"}
    if set(value) != expected or value.get("version") != 1 or value.get("kdf") != "scrypt":
        raise BackupFormatError("unsupported passphrase wrapper")
    if value.get("cipher") != "AES-256-GCM":
        raise BackupFormatError("unsupported passphrase wrapper cipher")
    n, r, p = value.get("n"), value.get("r"), value.get("p")
    _validate_scrypt_parameters(n, r, p)
    key_id = value.get("key_id")
    if not isinstance(key_id, str) or not _HEX_256.fullmatch(key_id):
        raise BackupFormatError("invalid repository key id")
    return PassphraseWrapper(
        key_id=key_id,
        salt=_b64decode(value.get("salt"), field_name="wrapper salt",
                        expected_length=16),
        nonce=_b64decode(value.get("nonce"), field_name="wrapper nonce",
                         expected_length=12),
        wrapped_key=_b64decode(value.get("wrapped_key"),
                               field_name="wrapped repository key",
                               expected_length=48),
        n=n, r=r, p=p,
    )


def _require_dpapi() -> None:
    if not crypto.dpapi_available():
        raise RecoveryUnsupportedError(
            "portable recovery currently requires Windows DPAPI")


@contextmanager
def _atomic_binary_writer(path: str, *, overwrite: bool,
                          before_publish=None) -> Iterator[BinaryIO]:
    destination = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(destination)
    if not parent:
        raise ValueError("destination must have a parent directory")
    os.makedirs(parent, exist_ok=True)
    if not overwrite and os.path.exists(destination):
        raise FileExistsError(destination)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.", suffix=".partial", dir=parent)
    handle = os.fdopen(fd, "wb")
    published = False
    try:
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        if not overwrite and os.path.exists(destination):
            raise FileExistsError(destination)
        if before_publish is not None:
            before_publish(temporary)
        os.replace(temporary, destination)
        published = True
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        _fsync_directory(parent)
    finally:
        if not handle.closed:
            handle.close()
        if not published:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def _fsync_directory(path: str) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str, data: bytes, *, overwrite: bool = False) -> str:
    """Write bytes through a flushed sibling ``.partial`` and atomic rename."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    with _atomic_binary_writer(path, overwrite=overwrite) as handle:
        handle.write(data)
    return os.path.abspath(os.fspath(path))


def create_repository_key(passphrase: str, key_path: str, *,
                          _scrypt_n: int | None = None) -> RepositoryKeyMaterial:
    """Create and DPAPI-cache a new repository key.

    ``_scrypt_n`` exists only to keep deterministic tests inexpensive; normal
    callers receive the required 2^17 work factor.
    """
    _require_dpapi()
    key = os.urandom(32)
    wrapper = _wrap_repository_key(
        key, passphrase, n=SCRYPT_N if _scrypt_n is None else _scrypt_n)
    try:
        protected = crypto.protect_secret(key)
    except crypto.EncryptionError as error:
        raise RecoveryUnsupportedError("Windows could not protect the backup key") from error
    document = {
        "format": _LOCAL_KEY_MAGIC,
        "version": 1,
        "key_id": wrapper.key_id,
        "protected_key": _b64encode(protected),
        "passphrase_wrapper": wrapper.to_dict(),
    }
    encoded = _canonical_json(document)
    if len(encoded) > MAX_LOCAL_KEY_FILE_SIZE:
        raise BackupFormatError("repository key file is too large")
    atomic_write_bytes(key_path, encoded, overwrite=False)
    return RepositoryKeyMaterial(key=key, passphrase_wrapper=wrapper)


def load_repository_key(key_path: str) -> RepositoryKeyMaterial:
    """Load a local repository key without requiring its recovery passphrase."""
    _require_dpapi()
    with open(key_path, "rb") as handle:
        encoded = handle.read(MAX_LOCAL_KEY_FILE_SIZE + 1)
    if len(encoded) > MAX_LOCAL_KEY_FILE_SIZE:
        raise BackupFormatError("repository key file is too large")
    document = _load_json(encoded, label="repository key file")
    expected = {"format", "version", "key_id", "protected_key",
                "passphrase_wrapper"}
    if set(document) != expected or document.get("format") != _LOCAL_KEY_MAGIC or document.get("version") != 1:
        raise BackupFormatError("unsupported repository key file")
    wrapper = _wrapper_from_mapping(document.get("passphrase_wrapper"))
    if document.get("key_id") != wrapper.key_id:
        raise BackupFormatError("repository key file does not match its wrapper")
    protected = _b64decode(document.get("protected_key"),
                           field_name="protected repository key",
                           max_length=64 * 1024)
    try:
        key = crypto.unprotect_secret(protected)
    except crypto.EncryptionError as error:
        raise RecoveryUnsupportedError("Windows could not unlock the backup key") from error
    if len(key) != 32 or not hmac.compare_digest(_key_id(key), wrapper.key_id):
        raise BackupAuthenticationError("local backup key is invalid")
    return RepositoryKeyMaterial(key=key, passphrase_wrapper=wrapper)


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _portable_path_parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise UnsafePathError("unsafe archive path")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise UnsafePathError("unsafe archive path") from error
    if len(encoded) > MAX_PATH_BYTES:
        raise UnsafePathError("archive path is too long")
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts or len(parts) > MAX_PATH_DEPTH:
        raise UnsafePathError("unsafe archive path")
    for part in parts:
        if part in {"", ".", ".."} or part.endswith((" ", ".")) or ":" in part:
            raise UnsafePathError("unsafe archive path")
        if any(ord(character) < 32 for character in part):
            raise UnsafePathError("unsafe archive path")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise UnsafePathError("archive path is not portable to Windows")
    return tuple(parts)


def _portable_path_key(parts: tuple[str, ...]) -> str:
    return "/".join(part.rstrip(" .").casefold() for part in parts)


def _scan_source(source_dir: str) -> tuple[list[_SourceEntry], int]:
    root = os.path.abspath(os.fspath(source_dir))
    try:
        root_info = os.stat(root, follow_symlinks=False)
    except FileNotFoundError:
        raise
    if not stat.S_ISDIR(root_info.st_mode) or _is_reparse_point(root_info) or os.path.islink(root):
        raise UnsafePathError("backup source must be a real directory")
    entries: list[_SourceEntry] = []
    portable_names: set[str] = set()
    total_size = 0

    def visit(directory: str, prefix: tuple[str, ...]) -> None:
        nonlocal total_size
        if len(prefix) >= MAX_PATH_DEPTH:
            raise UnsafePathError("backup source nesting is too deep")
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as error:
            raise RecoveryError("could not enumerate backup source") from error
        for child in children:
            relative_parts = prefix + (child.name,)
            relative = "/".join(relative_parts)
            parts = _portable_path_parts(relative)
            portable = _portable_path_key(parts)
            if portable in portable_names:
                raise UnsafePathError("backup contains colliding portable paths")
            portable_names.add(portable)
            try:
                info = os.stat(child.path, follow_symlinks=False)
            except OSError as error:
                raise RecoveryError("could not inspect backup source") from error
            if child.is_symlink() or _is_reparse_point(info):
                raise UnsafePathError("links and reparse points are not backed up")
            is_directory = stat.S_ISDIR(info.st_mode)
            is_file = stat.S_ISREG(info.st_mode)
            if not is_directory and not is_file:
                raise UnsafePathError("special files are not backed up")
            size = 0 if is_directory else int(info.st_size)
            if size < 0 or size > MAX_MEMBER_SIZE:
                raise BackupFormatError("backup member is too large")
            total_size += size
            if total_size > MAX_SOURCE_SIZE or len(entries) >= MAX_MEMBERS:
                raise BackupFormatError("backup source exceeds safety limits")
            entries.append(_SourceEntry(
                path=relative,
                absolute_path=child.path,
                is_directory=is_directory,
                size=size,
                mtime_ns=int(info.st_mtime_ns),
                mode=stat.S_IMODE(info.st_mode),
                device=int(info.st_dev),
                inode=int(info.st_ino),
            ))
            if is_directory:
                visit(child.path, relative_parts)

    visit(root, ())
    return entries, total_size


def _snapshot_matches(entry: _SourceEntry, info: os.stat_result) -> bool:
    return (not _is_reparse_point(info) and stat.S_ISREG(info.st_mode) and
            int(info.st_dev) == entry.device and int(info.st_ino) == entry.inode and
            int(info.st_size) == entry.size and int(info.st_mtime_ns) == entry.mtime_ns)


class _EncryptingSink(io.RawIOBase):
    def __init__(self, destination: BinaryIO, encryptor):
        self.destination = destination
        self.encryptor = encryptor
        self.position = 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        value = bytes(data)
        encrypted = self.encryptor.update(value)
        if encrypted:
            self.destination.write(encrypted)
        self.position += len(value)
        return len(value)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        self.destination.flush()


def _write_payload(entries: list[_SourceEntry], sink: BinaryIO) -> None:
    with gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=6, mtime=0) as compressed:
        compressed.write(PAYLOAD_MAGIC)
        for entry in entries:
            path_bytes = entry.path.encode("utf-8")
            entry_type = _ENTRY_DIRECTORY if entry.is_directory else _ENTRY_FILE
            compressed.write(_ENTRY.pack(entry_type, len(path_bytes), entry.size,
                                         entry.mtime_ns, entry.mode & 0o777))
            compressed.write(path_bytes)
            if entry.is_directory:
                continue
            try:
                before = os.stat(entry.absolute_path, follow_symlinks=False)
                if not _snapshot_matches(entry, before):
                    raise RecoveryError("backup source changed during collection")
                with open(entry.absolute_path, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if not _snapshot_matches(entry, opened):
                        raise RecoveryError("backup source changed during collection")
                    remaining = entry.size
                    while remaining:
                        chunk = source.read(min(STREAM_CHUNK_SIZE, remaining))
                        if not chunk:
                            raise RecoveryError("backup source changed during collection")
                        compressed.write(chunk)
                        remaining -= len(chunk)
                after = os.stat(entry.absolute_path, follow_symlinks=False)
                if not _snapshot_matches(entry, after):
                    raise RecoveryError("backup source changed during collection")
            except RecoveryError:
                raise
            except OSError as error:
                raise RecoveryError("could not read backup source") from error
        compressed.write(_ENTRY.pack(_ENTRY_END, 0, 0, 0, 0))


def encrypt_bundle(source_dir: str, dest_path: str, repo_key: bytes,
                   passphrase_wrapper: PassphraseWrapper | Mapping,
                   header_meta: Mapping | None = None) -> BundleInfo:
    """Stream a directory into a new, atomically published ``.ffbackup``."""
    destination = os.path.abspath(os.fspath(dest_path))
    if not destination.lower().endswith(".ffbackup"):
        raise ValueError("backup destination must end in .ffbackup")
    if not isinstance(repo_key, bytes) or len(repo_key) != 32:
        raise ValueError("repository key must be exactly 32 bytes")
    wrapper = _wrapper_from_mapping(passphrase_wrapper)
    if not hmac.compare_digest(_key_id(repo_key), wrapper.key_id):
        raise BackupAuthenticationError("repository key does not match its wrapper")
    entries, source_size = _scan_source(source_dir)
    metadata = _validate_public_json(dict(header_meta or {}))
    privacy_epoch = metadata.pop("privacy_epoch", 0)
    if (isinstance(privacy_epoch, bool) or not isinstance(privacy_epoch, int) or
            privacy_epoch < 0 or privacy_epoch > 2 ** 63 - 1):
        raise BackupFormatError("invalid privacy epoch")
    content_nonce = os.urandom(12)
    header = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "bundle_id": os.urandom(16).hex(),
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "privacy_epoch": privacy_epoch,
        "source_size": source_size,
        "cipher": "AES-256-GCM",
        "payload": PAYLOAD_FORMAT,
        "content_nonce": _b64encode(content_nonce),
        "passphrase_wrapper": wrapper.to_dict(),
        "meta": metadata,
    }
    header_bytes = _canonical_json(header)
    if len(header_bytes) > MAX_HEADER_SIZE:
        raise BackupFormatError("backup header is too large")
    preamble = _PREAMBLE.pack(MAGIC, FORMAT_VERSION, len(header_bytes))
    aad = preamble + header_bytes
    with _atomic_binary_writer(
            destination, overwrite=False,
            before_publish=lambda temporary: verify_bundle(temporary, repo_key)) as output:
        output.write(aad)
        encryptor = Cipher(algorithms.AES(repo_key), modes.GCM(content_nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        sink = _EncryptingSink(output, encryptor)
        _write_payload(entries, sink)
        final = encryptor.finalize()
        if final:
            output.write(final)
        output.write(encryptor.tag)
    return inspect_bundle(destination)


def _parse_bundle(path: str) -> _ParsedBundle:
    absolute = os.path.abspath(os.fspath(path))
    try:
        size = os.path.getsize(absolute)
    except OSError:
        raise
    if size > MAX_BUNDLE_SIZE:
        raise BackupFormatError("backup exceeds the supported size")
    if size < _PREAMBLE.size + 2 + GCM_TAG_SIZE:
        raise BackupFormatError("backup is truncated")
    with open(absolute, "rb") as handle:
        preamble = handle.read(_PREAMBLE.size)
        if len(preamble) != _PREAMBLE.size:
            raise BackupFormatError("backup is truncated")
        magic, version, header_length = _PREAMBLE.unpack(preamble)
        if magic != MAGIC:
            raise BackupFormatError("not a Faerie Fire backup")
        if version != FORMAT_VERSION:
            raise BackupFormatError("backup requires a newer Faerie Fire version")
        if not 1 <= header_length <= MAX_HEADER_SIZE:
            raise BackupFormatError("invalid backup header size")
        if size <= _PREAMBLE.size + header_length + GCM_TAG_SIZE:
            raise BackupFormatError("backup is truncated")
        header_bytes = handle.read(header_length)
        if len(header_bytes) != header_length:
            raise BackupFormatError("backup is truncated")
    header = _load_json(header_bytes, label="backup header")
    expected = {"format", "version", "bundle_id", "created_utc", "privacy_epoch",
                "source_size", "cipher", "payload", "content_nonce",
                "passphrase_wrapper", "meta"}
    if set(header) != expected or header.get("format") != FORMAT_NAME or header.get("version") != FORMAT_VERSION:
        raise BackupFormatError("unsupported backup header")
    if header.get("cipher") != "AES-256-GCM" or header.get("payload") != PAYLOAD_FORMAT:
        raise BackupFormatError("unsupported backup encryption format")
    bundle_id = header.get("bundle_id")
    if not isinstance(bundle_id, str) or not re.fullmatch(r"[0-9a-f]{32}", bundle_id):
        raise BackupFormatError("invalid bundle id")
    created_utc = header.get("created_utc")
    if not isinstance(created_utc, str) or len(created_utc) > 64 or not created_utc.endswith("Z"):
        raise BackupFormatError("invalid backup creation time")
    privacy_epoch = header.get("privacy_epoch")
    if (isinstance(privacy_epoch, bool) or not isinstance(privacy_epoch, int) or
            privacy_epoch < 0 or privacy_epoch > 2 ** 63 - 1):
        raise BackupFormatError("invalid privacy epoch")
    source_size = header.get("source_size")
    if (isinstance(source_size, bool) or not isinstance(source_size, int) or
            source_size < 0 or source_size > MAX_SOURCE_SIZE):
        raise BackupFormatError("invalid backup source size")
    metadata = _validate_public_json(header.get("meta"))
    if not isinstance(metadata, dict):
        raise BackupFormatError("invalid backup metadata")
    wrapper = _wrapper_from_mapping(header.get("passphrase_wrapper"))
    content_nonce = _b64decode(header.get("content_nonce"),
                               field_name="content nonce", expected_length=12)
    aad = preamble + header_bytes
    payload_offset = len(aad)
    ciphertext_size = size - payload_offset - GCM_TAG_SIZE
    info = BundleInfo(
        path=absolute,
        version=version,
        bundle_id=bundle_id,
        created_utc=created_utc,
        privacy_epoch=privacy_epoch,
        source_size=source_size,
        size_bytes=size,
        header_meta=metadata,
        passphrase_wrapper=wrapper,
    )
    return _ParsedBundle(info=info, header_bytes=header_bytes, aad=aad,
                         payload_offset=payload_offset,
                         ciphertext_size=ciphertext_size,
                         content_nonce=content_nonce)


def inspect_bundle(path: str) -> BundleInfo:
    """Read and strictly validate public envelope metadata without decrypting."""
    return _parse_bundle(path).info

def verify_bundle(bundle_path: str, repository_key: bytes) -> BundleInfo:
    """Authenticate and fully validate a bundle using the local repository key."""
    parsed = _parse_bundle(bundle_path)
    if not isinstance(repository_key, bytes) or len(repository_key) != 32:
        raise ValueError("repository key must be exactly 32 bytes")
    if not hmac.compare_digest(_key_id(repository_key), parsed.info.wrapper.key_id):
        raise BackupAuthenticationError("repository key does not match this backup")
    scratch = tempfile.mkdtemp(prefix="faerie-fire-verify-")
    try:
        compressed = os.path.join(scratch, "payload.gz")
        staging = os.path.join(scratch, "staging")
        os.mkdir(staging)
        _decrypt_payload(os.path.abspath(os.fspath(bundle_path)), parsed,
                         repository_key, compressed)
        _extract_payload(compressed, staging, parsed.info.source_size,
                         preserve_metadata=False)
        return parsed.info
    finally:
        shutil.rmtree(scratch, ignore_errors=True)




def _read_exact(handle: BinaryIO, size: int, *, message: str) -> bytes:
    if size < 0:
        raise BackupFormatError(message)
    chunks = []
    remaining = size
    while remaining:
        chunk = handle.read(min(STREAM_CHUNK_SIZE, remaining))
        if not chunk:
            raise BackupFormatError(message)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _copy_exact(source: BinaryIO, destination: BinaryIO, size: int) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(STREAM_CHUNK_SIZE, remaining))
        if not chunk:
            raise BackupFormatError("compressed payload is truncated")
        destination.write(chunk)
        remaining -= len(chunk)


def _decrypt_payload(bundle_path: str, parsed: _ParsedBundle,
                     repository_key: bytes, compressed_path: str) -> None:
    with open(bundle_path, "rb") as source:
        source.seek(parsed.payload_offset + parsed.ciphertext_size)
        tag = source.read(GCM_TAG_SIZE)
        if len(tag) != GCM_TAG_SIZE or source.read(1):
            raise BackupFormatError("backup is truncated")
        source.seek(parsed.payload_offset)
        decryptor = Cipher(
            algorithms.AES(repository_key),
            modes.GCM(parsed.content_nonce, tag),
        ).decryptor()
        decryptor.authenticate_additional_data(parsed.aad)
        with open(compressed_path, "wb") as output:
            remaining = parsed.ciphertext_size
            while remaining:
                chunk = source.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    raise BackupFormatError("backup is truncated")
                plaintext = decryptor.update(chunk)
                if plaintext:
                    output.write(plaintext)
                remaining -= len(chunk)
            try:
                final = decryptor.finalize()
            except InvalidTag as error:
                raise BackupAuthenticationError(
                    "wrong recovery passphrase or modified backup") from error
            if final:
                output.write(final)
            output.flush()
            os.fsync(output.fileno())


def _extract_payload(compressed_path: str, staging_dir: str,
                     expected_source_size: int, *, preserve_metadata: bool = True) -> None:
    seen: set[str] = set()
    file_total = 0
    member_count = 0
    try:
        with gzip.open(compressed_path, "rb") as archive:
            if _read_exact(archive, len(PAYLOAD_MAGIC),
                           message="compressed payload is truncated") != PAYLOAD_MAGIC:
                raise BackupFormatError("invalid encrypted payload")
            while True:
                header = _read_exact(archive, _ENTRY.size,
                                     message="compressed payload is truncated")
                entry_type, path_length, size, mtime_ns, mode = _ENTRY.unpack(header)
                if entry_type == _ENTRY_END:
                    if path_length or size or mtime_ns or mode:
                        raise BackupFormatError("invalid payload terminator")
                    if archive.read(1):
                        raise BackupFormatError("trailing data in encrypted payload")
                    break
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise BackupFormatError("backup contains too many members")
                if entry_type not in {_ENTRY_FILE, _ENTRY_DIRECTORY}:
                    raise BackupFormatError("unsupported payload member type")
                if not 1 <= path_length <= MAX_PATH_BYTES:
                    raise UnsafePathError("invalid archive path length")
                path_bytes = _read_exact(archive, path_length,
                                         message="compressed payload is truncated")
                try:
                    relative = path_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise UnsafePathError("archive path is not UTF-8") from error
                parts = _portable_path_parts(relative)
                portable = _portable_path_key(parts)
                if portable in seen:
                    raise UnsafePathError("backup contains duplicate portable paths")
                seen.add(portable)
                if size > MAX_MEMBER_SIZE:
                    raise BackupFormatError("backup member is too large")
                target = os.path.join(staging_dir, *parts)
                if entry_type == _ENTRY_DIRECTORY:
                    if size:
                        raise BackupFormatError("directory member has file data")
                    os.makedirs(target, exist_ok=False)
                    continue
                file_total += size
                if file_total > MAX_SOURCE_SIZE or file_total > expected_source_size:
                    raise BackupFormatError("expanded backup exceeds its declared size")
                parent = os.path.dirname(target)
                os.makedirs(parent, exist_ok=True)
                with open(target, "xb") as output:
                    _copy_exact(archive, output, size)
                if preserve_metadata:
                    try:
                        os.chmod(target, mode & 0o777)
                        os.utime(target, ns=(mtime_ns, mtime_ns))
                    except (OSError, OverflowError, ValueError):
                        pass
    except (gzip.BadGzipFile, EOFError, zlib_error_types()) as error:
        raise BackupFormatError("invalid compressed payload") from error
    if file_total != expected_source_size:
        raise BackupFormatError("backup source-size check failed")


def zlib_error_types():
    """Return zlib.error lazily without adding it to the public API surface."""
    import zlib
    return zlib.error


def decrypt_bundle(bundle_path: str, passphrase: str, dest_dir: str) -> BundleInfo:
    """Authenticate and safely extract a bundle to a new destination directory."""
    parsed = _parse_bundle(bundle_path)
    repository_key = _unwrap_repository_key(parsed.info.passphrase_wrapper, passphrase)
    destination = os.path.abspath(os.fspath(dest_dir))
    if os.path.exists(destination):
        raise FileExistsError(destination)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    file_descriptor, compressed_path = tempfile.mkstemp(
        prefix=".faerie-fire-payload.", suffix=".partial", dir=parent)
    os.close(file_descriptor)
    staging = tempfile.mkdtemp(prefix=".faerie-fire-restore.", dir=parent)
    published = False
    try:
        _decrypt_payload(os.path.abspath(os.fspath(bundle_path)), parsed,
                         repository_key, compressed_path)
        _extract_payload(compressed_path, staging, parsed.info.source_size)
        if os.path.exists(destination):
            raise FileExistsError(destination)
        os.replace(staging, destination)
        published = True
        _fsync_directory(parent)
        return parsed.info
    finally:
        try:
            os.remove(compressed_path)
        except FileNotFoundError:
            pass
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _require_automatic_database_key_mode() -> None:
    custom = ("LIVINGPC_DB_KEY", "LIVINGPC_KEY_FILE", "LIVINGPC_SALT_FILE")
    if any(name in os.environ for name in custom):
        raise RecoveryUnsupportedError(
            "portable recovery requires the automatic database-key mode")
    if os.environ.get("LIVINGPC_AUTO_ENCRYPTION", "1").lower() in {"0", "false", "no"}:
        raise RecoveryUnsupportedError(
            "portable recovery requires automatic database encryption")
    _require_dpapi()


def _validate_database_key_material(material: DatabaseKeyMaterial) -> None:
    if not isinstance(material, DatabaseKeyMaterial) or len(material.secret) != 32:
        raise BackupFormatError("invalid automatic database key material")
    if not material.salt_file or len(material.salt_file) > 4096:
        raise BackupFormatError("invalid automatic database salt")
    try:
        decoded_salt = base64.b64decode(material.salt_file.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise BackupFormatError("invalid automatic database salt") from error
    if len(decoded_salt) != 16:
        raise BackupFormatError("invalid automatic database salt")


def _database_key_verifier(material: DatabaseKeyMaterial) -> bytes:
    """Authenticate the exact DB key/salt pair even when databases are empty."""
    _validate_database_key_material(material)
    salt = base64.b64decode(material.salt_file.strip(), validate=True)
    passphrase = base64.urlsafe_b64encode(material.secret)
    derived = hashlib.pbkdf2_hmac(
        "sha256", passphrase, salt, 200_000, dklen=32)
    return hmac.new(
        derived, b"Faerie Fire automatic database key verifier v1\0",
        hashlib.sha256).digest()


def export_automatic_database_key(*, key_path: str | None = None,
                                  salt_file: str | None = None) -> DatabaseKeyMaterial:
    """Export existing automatic key material without ever generating it."""
    _require_automatic_database_key_mode()
    key_path = os.path.abspath(key_path or crypto.automatic_key_path())
    salt_file = os.path.abspath(salt_file or crypto.salt_path())
    if not os.path.isfile(key_path) or not os.path.isfile(salt_file):
        raise FileNotFoundError("automatic database key material is incomplete")
    with open(key_path, "rb") as handle:
        protected = handle.read(64 * 1024 + 1)
    if not protected or len(protected) > 64 * 1024:
        raise BackupFormatError("invalid protected automatic database key")
    with open(salt_file, "rb") as handle:
        salt_bytes = handle.read(4097)
    try:
        secret = crypto.unprotect_secret(protected)
    except crypto.EncryptionError as error:
        raise RecoveryUnsupportedError(
            "Windows could not unlock the automatic database key") from error
    material = DatabaseKeyMaterial(secret=secret, salt_file=salt_bytes)
    _validate_database_key_material(material)
    return material


def encode_database_key_material(material: DatabaseKeyMaterial) -> bytes:
    """Encode key material for placement *inside* an encrypted payload."""
    _validate_database_key_material(material)
    encoded = _canonical_json({
        "format": _DATABASE_KEY_MAGIC,
        "version": 2,
        "secret": _b64encode(material.secret),
        "salt_file": _b64encode(material.salt_file),
        "verifier": _b64encode(_database_key_verifier(material)),
    })
    if len(encoded) > MAX_DATABASE_KEY_EXPORT_SIZE:
        raise BackupFormatError("database key export is too large")
    return encoded


def decode_database_key_material(encoded: bytes) -> DatabaseKeyMaterial:
    """Decode key material only after the surrounding bundle is authenticated."""
    if not isinstance(encoded, bytes) or len(encoded) > MAX_DATABASE_KEY_EXPORT_SIZE:
        raise BackupFormatError("invalid database key export")
    document = _load_json(encoded, label="database key export")
    version = document.get("version")
    expected = {"format", "version", "secret", "salt_file"}
    if version == 2:
        expected.add("verifier")
    if (set(document) != expected
            or document.get("format") != _DATABASE_KEY_MAGIC
            or version not in {1, 2}):
        raise BackupFormatError("unsupported database key export")
    material = DatabaseKeyMaterial(
        secret=_b64decode(document.get("secret"), field_name="database secret",
                          expected_length=32),
        salt_file=_b64decode(document.get("salt_file"),
                             field_name="database salt file", max_length=4096),
    )
    _validate_database_key_material(material)
    if version == 2:
        verifier = _b64decode(
            document.get("verifier"), field_name="database key verifier",
            expected_length=32)
        if not hmac.compare_digest(verifier, _database_key_verifier(material)):
            raise BackupFormatError("database key verifier does not match")
    return material


def _stage_file(destination: str, data: bytes) -> str:
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.", suffix=".partial", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
        raise


def install_automatic_database_key(material: DatabaseKeyMaterial, *,
                                   key_path: str | None = None,
                                   salt_file: str | None = None,
                                   overwrite: bool = False) -> tuple[str, str]:
    """DPAPI-rewrap recovered database key material for this Windows user."""
    _require_automatic_database_key_mode()
    _validate_database_key_material(material)
    key_path = os.path.abspath(key_path or crypto.automatic_key_path())
    salt_file = os.path.abspath(salt_file or crypto.salt_path())
    if os.path.normcase(key_path) == os.path.normcase(salt_file):
        raise ValueError("database key and salt paths must differ")
    if not overwrite and (os.path.exists(key_path) or os.path.exists(salt_file)):
        raise FileExistsError("automatic database key destination is not empty")
    try:
        protected = crypto.protect_secret(material.secret)
    except crypto.EncryptionError as error:
        raise RecoveryUnsupportedError(
            "Windows could not protect the automatic database key") from error
    staged_salt = _stage_file(salt_file, material.salt_file)
    staged_key = None
    try:
        staged_key = _stage_file(key_path, protected)
        if not overwrite and (os.path.exists(key_path) or os.path.exists(salt_file)):
            raise FileExistsError("automatic database key destination is not empty")
        os.replace(staged_salt, salt_file)
        staged_salt = ""
        os.replace(staged_key, key_path)
        staged_key = ""
        for path in (key_path, salt_file):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            _fsync_directory(os.path.dirname(path))
        return key_path, salt_file
    finally:
        for temporary in (staged_salt, staged_key):
            if temporary:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass
