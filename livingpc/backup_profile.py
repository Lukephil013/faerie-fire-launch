"""Build and validate the private profile payload inside a portable backup."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from .config import APP_DIR, Config
from . import recovery


PROFILE_FORMAT = "faerie-fire-profile"
PROFILE_VERSION = 1
APP_VERSION = "2026.07"
MANIFEST_NAME = "manifest.json"
KEY_MATERIAL_NAME = ".faerie-fire/database-key.json"
SETTINGS_NAME = ".faerie-fire/portable-settings.json"

_PORTABLE_SETTINGS = {
    "profile", "blocklist", "blob_retention_days", "ocr_enabled",
    "browser_history_enabled", "clipboard_enabled", "companion_voice",
    "companion_tts_engine", "whisper_model", "whisper_device",
    "companion_wake_phrase", "companion_ptt_hotkey",
    "inference_scheduler_enabled", "inference_schedule",
    "inference_nightly_hour", "triage_nightly_enabled",
    "companion_reflection_enabled", "notifications_enabled",
    "notify_on_graduation", "reminders_enabled", "goal_ai_enabled",
    "goal_ai_schedule", "goal_ai_notifications", "filing_auto_offer",
    "filing_to_memory", "browser_assistant_enabled",
}
_EXTERNAL_ID_RE = re.compile(
    r"^(?:notion_.+_id|.+_upload_id|external_.+|authorization_.+)$",
    re.I,
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_EXCLUDED_DIR_NAMES = {
    ".git", ".cache", ".pytest_cache", ".venv", "__pycache__",
    "node_modules", "venv", "env", "diagnostics", "reports", "logs",
    "browser-profile",
}
_EXCLUDED_FILE_NAMES = {
    ".env", "api_key.secret", "secret.key", "secret.salt",
    "credentials.json", "token.json",
}


def _excluded(name: str, *, directory: bool) -> bool:
    lowered = str(name).casefold()
    if directory:
        return lowered in _EXCLUDED_DIR_NAMES
    return (
        lowered in _EXCLUDED_FILE_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith((".pyc", ".pyo", ".log"))
    )


class ProfileError(RuntimeError):
    pass


class ProfileChangedError(ProfileError):
    pass


@dataclass(frozen=True)
class ProfileManifest:
    app_version: str
    format_version: int
    privacy_epoch: int
    created_utc: str
    include_blobs: bool
    files: dict


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _atomic_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".partial"
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sqlite_snapshot(source_path: str, destination_path: str) -> None:
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError("required profile database is missing")
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    source = sqlite3.connect(source_path, timeout=30)
    destination = sqlite3.connect(destination_path, timeout=30)
    try:
        source.backup(destination, pages=256, sleep=0.01)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]


def sanitize_database(path: str, *, include_blobs: bool) -> None:
    """Remove machine authorization state from a staged SQLite snapshot."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA secure_delete=ON")
        tables = _tables(connection)
        for table in ("browser_site_permissions", "browser_tasks"):
            if table in tables:
                connection.execute(f"DELETE FROM {_quote_identifier(table)}")
        for table in tables:
            columns = list(connection.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"))
            names = {str(row[1]): bool(row[3]) for row in columns}
            if not include_blobs and "blob_ref" in names:
                replacement = "''" if names["blob_ref"] else "NULL"
                connection.execute(
                    f"UPDATE {_quote_identifier(table)} SET blob_ref={replacement}")
            for column, not_null in names.items():
                if _EXTERNAL_ID_RE.match(column):
                    replacement = "''" if not_null else "NULL"
                    connection.execute(
                        f"UPDATE {_quote_identifier(table)} "
                        f"SET {_quote_identifier(column)}={replacement}")
        connection.commit()
        # A staged snapshot must stay one self-contained file: WAL mode would
        # let later validation opens leave -wal/-shm siblings that break the
        # manifest hash check.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()


def sqlite_integrity(path: str) -> None:
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if rows != ["ok"]:
            raise ProfileError("staged database failed integrity validation")
    finally:
        connection.close()


def _entry_signature(root: str) -> tuple:
    root = os.path.abspath(root)
    signature = []

    def walk(directory: str, relative: str = ""):
        info = os.stat(directory, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
            raise ProfileError("profile sources may not contain links or junctions")
        with os.scandir(directory) as entries:
            children = sorted(
                (item for item in entries
                 if not _excluded(
                     item.name, directory=item.is_dir(follow_symlinks=False))),
                key=lambda item: item.name.casefold(),
            )
        signature.append((relative, "d", info.st_mtime_ns, len(children)))
        for child in children:
            child_info = os.stat(child.path, follow_symlinks=False)
            if child.is_symlink() or (
                    getattr(child_info, "st_file_attributes", 0) & _REPARSE_POINT):
                raise ProfileError("profile sources may not contain links or junctions")
            child_relative = f"{relative}/{child.name}" if relative else child.name
            if stat.S_ISDIR(child_info.st_mode):
                walk(child.path, child_relative)
            elif stat.S_ISREG(child_info.st_mode):
                signature.append((child_relative, "f", child_info.st_size,
                                  child_info.st_mtime_ns,
                                  getattr(child_info, "st_ino", 0)))
            else:
                raise ProfileError("profile sources contain an unsupported file")

    walk(root)
    return tuple(signature)


def _copy_tree_once(source: str, destination: str) -> None:
    os.makedirs(destination, exist_ok=False)
    for directory, directories, files in os.walk(source, followlinks=False):
        directories.sort(key=str.casefold)
        directories[:] = [name for name in directories
                           if not _excluded(name, directory=True)]
        files.sort(key=str.casefold)
        files = [name for name in files
                 if not _excluded(name, directory=False)]
        relative = os.path.relpath(directory, source)
        target_dir = destination if relative == "." else os.path.join(destination, relative)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            source_file = os.path.join(directory, name)
            info = os.stat(source_file, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
                raise ProfileError("profile sources may not contain links or junctions")
            shutil.copyfile(source_file, os.path.join(target_dir, name))


def copy_tree_stable(source: str, destination: str, *, attempts: int = 2) -> None:
    """Copy only if the source inventory and every file stat stay unchanged."""
    for attempt in range(max(1, attempts)):
        before = _entry_signature(source)
        if os.path.exists(destination):
            shutil.rmtree(destination)
        _copy_tree_once(source, destination)
        after = _entry_signature(source)
        if before == after:
            return
        shutil.rmtree(destination, ignore_errors=True)
        if attempt + 1 < attempts:
            time.sleep(0.05)
    raise ProfileChangedError("profile files changed while the backup was collected")


def copy_file_stable(source: str, destination: str, *, attempts: int = 2) -> None:
    for attempt in range(max(1, attempts)):
        before = os.stat(source, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or (
                getattr(before, "st_file_attributes", 0) & _REPARSE_POINT):
            raise ProfileError("profile sources may not contain links or junctions")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(source, destination)
        after = os.stat(source, follow_symlinks=False)
        if (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", 0)) == (
                after.st_size, after.st_mtime_ns, getattr(after, "st_ino", 0)):
            return
        try:
            os.remove(destination)
        except FileNotFoundError:
            pass
        if attempt + 1 < attempts:
            time.sleep(0.05)
    raise ProfileChangedError("profile file changed while the backup was collected")


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except ValueError:
        return False


def _personal_sources(cfg, include_blobs: bool):
    data_dir = os.path.dirname(os.path.abspath(cfg.memory_db_path))
    def rooted(value, default):
        value = os.fspath(value or default)
        return value if os.path.isabs(value) else os.path.join(APP_DIR, value)

    directories = [
        (rooted(getattr(cfg, "projects_dir", ""), "projects"), "projects"),
        (rooted(getattr(cfg, "skills_dir", ""), "skills"), "skills"),
        (os.path.join(APP_DIR, "vault"), "vault"),
        (os.path.join(data_dir, "notion"), "data/notion"),
        (os.path.join(data_dir, "filed_dumps"), "data/filed_dumps"),
    ]
    journal = getattr(cfg, "journal_dir", "")
    if journal:
        journal = journal if os.path.isabs(journal) else os.path.join(APP_DIR, journal)
        directories.append((journal, "data/journals"))
    filing_journal = getattr(cfg, "filing_journal_dir", "")
    if filing_journal:
        filing_journal = (filing_journal if os.path.isabs(filing_journal)
                          else os.path.join(APP_DIR, filing_journal))
        directories.append((filing_journal, "data/filing_journals"))
    if include_blobs:
        directories.append((rooted(getattr(cfg, "blob_dir", ""),
                                           os.path.join(data_dir, "blobs")),
                            "data/blobs"))
    seen = set()
    for source, relative in directories:
        source = os.path.abspath(os.fspath(source))
        key = os.path.normcase(source)
        if key in seen or not os.path.isdir(source):
            continue
        seen.add(key)
        yield source, relative


def portable_settings(cfg) -> dict:
    allowed = {item.name for item in fields(Config)}
    settings = {}
    for name in sorted(_PORTABLE_SETTINGS & allowed):
        value = getattr(cfg, name, None)
        if value is None or isinstance(value, (str, int, float, bool, list, dict)):
            settings[name] = value
    try:
        from .lang import app_language
        settings["language"] = app_language()
    except Exception:
        settings["language"] = "en"
    # Restored integrations are deliberately paused until the user reviews and
    # authenticates them again.
    settings.update({
        "notion_sync_enabled": False,
        "browser_assistant_enabled": False,
        "reminders_enabled": False,
        "external_integrations_paused": True,
    })
    return settings


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _payload_files(root: str) -> dict:
    result = {}
    for directory, directories, files in os.walk(root):
        directories.sort(key=str.casefold)
        files.sort(key=str.casefold)
        for name in files:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative == MANIFEST_NAME:
                continue
            digest, size = _hash_file(path)
            result[relative] = {"sha256": digest, "size": size}
    return result


def build_profile_stage(cfg, stage_root: str, *, privacy_epoch: int,
                        include_blobs: bool) -> ProfileManifest:
    """Create a consistent, scrubbed profile tree ready for encryption."""
    stage_root = os.path.abspath(stage_root)
    if os.path.exists(stage_root):
        raise FileExistsError(stage_root)
    profile = os.path.join(stage_root, "profile")
    os.makedirs(os.path.join(profile, "data"), exist_ok=True)
    try:
        memory_snapshot = os.path.join(profile, "data", "memory.db")
        activity_snapshot = os.path.join(profile, "data", "living_computer.db")
        _sqlite_snapshot(cfg.memory_db_path, memory_snapshot)
        _sqlite_snapshot(cfg.db_path, activity_snapshot)
        sanitize_database(memory_snapshot, include_blobs=include_blobs)
        sanitize_database(activity_snapshot, include_blobs=include_blobs)
        sqlite_integrity(memory_snapshot)
        sqlite_integrity(activity_snapshot)

        data_dir = os.path.dirname(os.path.abspath(cfg.memory_db_path))
        for path in sorted(Path(data_dir).glob("portrait.*")):
            if path.is_file():
                copy_file_stable(str(path), os.path.join(profile, "data", path.name))
        for filename in ("personas.json",):
            source = os.path.join(APP_DIR, filename)
            if os.path.isfile(source):
                copy_file_stable(source, os.path.join(profile, filename))
        for source, relative in _personal_sources(cfg, include_blobs):
            copy_tree_stable(source, os.path.join(profile, *relative.split("/")))

        key_material = recovery.export_automatic_database_key()
        _atomic_bytes(os.path.join(stage_root, *KEY_MATERIAL_NAME.split("/")),
                      recovery.encode_database_key_material(key_material))
        _atomic_bytes(os.path.join(stage_root, *SETTINGS_NAME.split("/")),
                      _canonical_json(portable_settings(cfg)))
        files_map = _payload_files(stage_root)
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        manifest = {
            "format": PROFILE_FORMAT,
            "format_version": PROFILE_VERSION,
            "app_version": APP_VERSION,
            "created_utc": created,
            "privacy_epoch": int(privacy_epoch),
            "include_blobs": bool(include_blobs),
            "databases": ["profile/data/memory.db", "profile/data/living_computer.db"],
            "files": files_map,
        }
        _atomic_bytes(os.path.join(stage_root, MANIFEST_NAME),
                      _canonical_json(manifest))
        return ProfileManifest(APP_VERSION, PROFILE_VERSION, int(privacy_epoch),
                               created, bool(include_blobs), files_map)
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _load_manifest(stage_root: str) -> dict:
    path = os.path.join(stage_root, MANIFEST_NAME)
    with open(path, "rb") as handle:
        encoded = handle.read(16 * 1024 * 1024 + 1)
    if len(encoded) > 16 * 1024 * 1024:
        raise ProfileError("backup manifest is too large")
    try:
        manifest = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError("backup manifest is invalid") from error
    required = {"format", "format_version", "app_version", "created_utc",
                "privacy_epoch", "include_blobs", "databases", "files"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ProfileError("backup manifest is invalid")
    if manifest["format"] != PROFILE_FORMAT:
        raise ProfileError("not a Faerie Fire profile payload")
    version = manifest["format_version"]
    if not isinstance(version, int) or version > PROFILE_VERSION:
        raise ProfileError("update Faerie Fire first")
    if version < 1:
        raise ProfileError("backup profile version is unsupported")
    return manifest


def refresh_profile_manifest(stage_root: str) -> dict:
    """Re-hash an authenticated stage after intentional local path rebasing."""
    manifest = _load_manifest(stage_root)
    manifest["files"] = _payload_files(stage_root)
    _atomic_bytes(os.path.join(stage_root, MANIFEST_NAME),
                  _canonical_json(manifest))
    return manifest


def validate_profile_stage(stage_root: str, *, minimum_epoch: int = 0) -> tuple[dict, recovery.DatabaseKeyMaterial]:
    manifest = _load_manifest(stage_root)
    if int(manifest["privacy_epoch"]) < int(minimum_epoch):
        raise ProfileError("backup belongs to a stale privacy epoch")
    actual = _payload_files(stage_root)
    if actual != manifest["files"]:
        raise ProfileError("backup manifest hashes do not match the payload")
    for relative in manifest["databases"]:
        if relative not in actual:
            raise ProfileError("backup database is missing")
        sqlite_integrity(os.path.join(stage_root, *relative.split("/")))
    key_path = os.path.join(stage_root, *KEY_MATERIAL_NAME.split("/"))
    with open(key_path, "rb") as handle:
        key_material = recovery.decode_database_key_material(handle.read())
    _validate_encrypted_content(
        os.path.join(stage_root, "profile", "data", "memory.db"), key_material)
    _validate_encrypted_content(
        os.path.join(stage_root, "profile", "data", "living_computer.db"), key_material)
    return manifest, key_material


def _validate_encrypted_content(db_path: str,
                                material: recovery.DatabaseKeyMaterial) -> None:
    """Decrypt at most one ciphertext without exposing its plaintext."""
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    try:
        salt = base64.b64decode(material.salt_file.strip(), validate=True)
        passphrase = base64.urlsafe_b64encode(material.secret)
        key = base64.urlsafe_b64encode(PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=salt,
            iterations=200_000).derive(passphrase))
        fernet = Fernet(key)
    except Exception as error:
        raise ProfileError("backup database key material is invalid") from error
    connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        for table in _tables(connection):
            columns = [str(row[1]) for row in connection.execute(
                f"PRAGMA table_info({_quote_identifier(table)})")
                if str(row[2]).upper() in {"TEXT", ""}]
            for column in columns:
                row = connection.execute(
                    f"SELECT {_quote_identifier(column)} FROM {_quote_identifier(table)} "
                    f"WHERE {_quote_identifier(column)} LIKE 'enc::%' LIMIT 1").fetchone()
                if row and isinstance(row[0], str):
                    try:
                        fernet.decrypt(row[0][5:].encode("ascii"))
                    except (InvalidToken, ValueError, UnicodeEncodeError) as error:
                        raise ProfileError(
                            "backup database content cannot be decrypted") from error
                    return
    finally:
        connection.close()


def rebase_staged_paths(stage_root: str, target_root: str = APP_DIR, *,
                        data_dir: str | None = None,
                        blob_dir: str | None = None) -> None:
    profile = os.path.join(stage_root, "profile")
    target_root = os.path.abspath(target_root)
    data_dir = os.path.abspath(data_dir or os.path.join(target_root, "data"))
    blob_dir = os.path.abspath(blob_dir or os.path.join(data_dir, "blobs"))
    memory_db = os.path.join(profile, "data", "memory.db")
    portraits = sorted(Path(os.path.join(profile, "data")).glob("portrait.*"))
    connection = sqlite3.connect(memory_db)
    try:
        if "meta" in _tables(connection):
            if portraits:
                new_path = os.path.join(data_dir, portraits[0].name)
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES('portrait_image_path',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_path,))
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('restore_review_required','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('external_integrations_paused','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")
        connection.commit()
    finally:
        connection.close()

    activity_db = os.path.join(profile, "data", "living_computer.db")
    connection = sqlite3.connect(activity_db)
    try:
        for table in _tables(connection):
            columns = {str(row[1]) for row in connection.execute(
                f"PRAGMA table_info({_quote_identifier(table)})")}
            if "blob_ref" not in columns:
                continue
            rows = list(connection.execute(
                f"SELECT rowid,blob_ref FROM {_quote_identifier(table)} "
                "WHERE blob_ref IS NOT NULL AND blob_ref!=''"))
            for rowid, old_path in rows:
                name = os.path.basename(str(old_path))
                staged = os.path.join(profile, "data", "blobs", name)
                replacement = (os.path.join(blob_dir, name)
                               if os.path.isfile(staged) else None)
                connection.execute(
                    f"UPDATE {_quote_identifier(table)} SET blob_ref=? WHERE rowid=?",
                    (replacement, rowid))
        connection.commit()
    finally:
        connection.close()


def load_portable_settings(stage_root: str) -> dict:
    path = os.path.join(stage_root, *SETTINGS_NAME.split("/"))
    with open(path, "rb") as handle:
        value = json.loads(handle.read(1024 * 1024).decode("utf-8"))
    if not isinstance(value, dict):
        raise ProfileError("portable settings are invalid")
    return value

