"""Portable encrypted whole-instance backup, retention, purge, and restore."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import recovery
from .backup_profile import (
    APP_VERSION,
    PROFILE_VERSION,
    build_profile_stage,
    load_portable_settings,
    rebase_staged_paths,
    refresh_profile_manifest,
    sqlite_integrity,
    validate_profile_stage,
)
from .config import APP_DIR
from .maintenance import MaintenanceBusy, maintenance_lock


BACKUP_FORMAT_VERSION = recovery.FORMAT_VERSION
_CONTROL_DIR = ".faerie-fire"
_INDEX_FILE = "index.json"
_EPOCH_FILE = "privacy-epoch"
_STATE_VERSION = 1
_BUNDLE_RE = re.compile(
    r"^faerie-fire-\d{8}T\d{6}Z-[0-9a-f]{12}\.ffbackup$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_CONFIG_KEYS = (
    "instance_backup_enabled", "instance_backup_primary_dir",
    "instance_backup_secondary_dir", "instance_backup_hour",
    "instance_backup_keep_daily", "instance_backup_keep_weekly",
    "instance_backup_keep_monthly", "instance_backup_include_blobs",
)


@dataclass(frozen=True)
class BackupStatus:
    ok: bool
    enabled: bool
    configured: bool
    due: bool
    last_verified_utc: str = ""
    age_seconds: float | None = None
    primary_healthy: bool = False
    secondary_healthy: bool | None = None
    retained_generations: int = 0
    next_attempt_utc: str = ""
    error_code: str = ""
    purge_pending: bool = False
    pending_destinations: tuple[str, ...] = ()
    privacy_epoch: int = 0
    same_volume_warning: bool = False
    include_blobs: bool = False
    secondary_configured: bool = False


@dataclass(frozen=True)
class BackupResult:
    ok: bool
    verified: bool
    path: str = ""
    mirrored: bool = False
    bundle_id: str = ""
    created_utc: str = ""
    error_code: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundleInfo:
    ok: bool
    format_version: int
    bundle_id: str
    created_utc: str
    privacy_epoch: int
    app_version: str
    size_bytes: int
    reason: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class PreparedRestore:
    ok: bool
    token: str = field(default="", repr=False)
    verified: bool = False
    bundle_id: str = ""
    created_utc: str = ""
    privacy_epoch: int = 0
    preview: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error_code: str = ""


@dataclass(frozen=True)
class RestoreResult:
    ok: bool
    activated: bool
    rolled_back: bool = False
    rollback_backup: str = ""
    error_code: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PurgeResult:
    ok: bool
    privacy_epoch: int
    removed: int = 0
    pending_destinations: tuple[str, ...] = ()
    purge_pending: bool = False
    error_code: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _local_root(cfg) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg.memory_db_path)),
                        ".instance-backup")


def _state_path(cfg) -> str:
    return os.path.join(_local_root(cfg), "state.json")


def _key_path(cfg) -> str:
    return os.path.join(_local_root(cfg), "repository.key")


def _restore_root(cfg) -> str:
    return os.path.join(_local_root(cfg), "restore")


def _safe_error(prefix: str, error: Exception) -> str:
    return f"{prefix}_{type(error).__name__}"


def _json_read(path: str, default, *, limit: int = 8 * 1024 * 1024):
    try:
        with open(path, "rb") as handle:
            encoded = handle.read(limit + 1)
        if len(encoded) > limit:
            return default
        return json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _json_write(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".partial"
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    with open(temporary, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(os.path.dirname(path))


def _fsync_directory(path: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:

        pass

def _default_state() -> dict:
    return {
        "version": _STATE_VERSION,
        "privacy_epoch": 0,
        "last_verified_utc": "",
        "last_error_code": "",
        "pending_purges": [],
        "primary": "",
        "secondary": "",
    }


def _pending_record(label: str, path: str, epoch: int) -> dict | None:
    if not path or not os.path.isabs(os.fspath(path)):
        return None
    safe_label = str(label) if str(label) in {"primary", "secondary"} else "repository"
    return {
        "label": safe_label,
        "path": os.path.abspath(os.fspath(path)),
        "epoch": max(0, int(epoch)),
    }


def _normalize_pending(value, state: dict) -> list[dict]:
    if not isinstance(value, list):
        return []
    default_epoch = max(0, int(state.get("privacy_epoch", 0) or 0))
    normalized: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            record = _pending_record(
                str(item.get("label", "repository")), str(item.get("path", "")),
                int(item.get("epoch", default_epoch) or default_epoch))
        else:
            label = str(item)
            record = _pending_record(
                label, str(state.get(label, "") or ""), default_epoch)
        if record is None:
            continue
        key = os.path.normcase(record["path"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(record)
    return normalized
def _load_state(cfg) -> dict:
    state = _json_read(_state_path(cfg), _default_state())
    if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
        return _default_state()
    result = _default_state()
    result.update({key: state.get(key, value) for key, value in result.items()})
    result["pending_purges"] = _normalize_pending(result["pending_purges"], result)
    return result


def _save_state(cfg, state: dict) -> None:
    state = dict(state)
    state["version"] = _STATE_VERSION
    _json_write(_state_path(cfg), state)


def _control_path(destination: str, name: str) -> str:
    return os.path.join(destination, _CONTROL_DIR, name)


def _read_epoch(destination: str) -> int | None:
    try:
        with open(_control_path(destination, _EPOCH_FILE), encoding="ascii") as handle:
            value = int(handle.read(64).strip())
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def _write_epoch(destination: str, epoch: int) -> None:
    control = os.path.join(destination, _CONTROL_DIR)
    os.makedirs(control, exist_ok=True)
    path = os.path.join(control, _EPOCH_FILE)
    temporary = path + ".partial"
    with open(temporary, "w", encoding="ascii", newline="\n") as handle:
        handle.write(str(int(epoch)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_index(destination: str) -> dict:
    value = _json_read(_control_path(destination, _INDEX_FILE), {})
    if not isinstance(value, dict) or not isinstance(value.get("entries", []), list):
        return {"version": 1, "privacy_epoch": 0, "entries": []}
    return {
        "version": 1,
        "privacy_epoch": int(value.get("privacy_epoch", 0) or 0),
        "entries": [item for item in value.get("entries", []) if isinstance(item, dict)],
    }


def _save_index(destination: str, index: dict) -> None:
    _json_write(_control_path(destination, _INDEX_FILE), index)


def _destination_healthy(path: str) -> bool:
    return bool(path and os.path.isabs(path) and os.path.isdir(path)
                and os.access(path, os.R_OK | os.W_OK))


def _same_volume(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_drive = os.path.splitdrive(os.path.abspath(left))[0]
    right_drive = os.path.splitdrive(os.path.abspath(right))[0]
    if left_drive or right_drive:
        return bool(left_drive and left_drive.casefold() == right_drive.casefold())
    try:
        return os.stat(left).st_dev == os.stat(right).st_dev
    except OSError:
        return False


def _config_path(cfg) -> str | None:
    value = getattr(cfg, "_config_path", None) or getattr(cfg, "config_path", None)
    if value:
        return os.path.abspath(value)
    try:
        if os.path.commonpath((os.path.abspath(cfg.memory_db_path), APP_DIR)) == APP_DIR:
            return os.path.join(APP_DIR, "config.toml")
    except ValueError:
        pass
    return None


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{key} = {_toml_value(item)}" for key, item in sorted(value.items())) + "}"
    raise TypeError("unsupported portable setting")


def _update_config(cfg, values: dict) -> None:
    for key, value in values.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    path = _config_path(cfg)
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    for key, value in values.items():
        encoded = _toml_value(value)
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
        if pattern.search(text):
            # Use a callable replacement: a plain replacement string would have
            # its backslashes reprocessed by re (\\ -> \), corrupting a valid
            # TOML value like "C:\\Users\\..." back into an invalid single-
            # backslash Windows path that then fails to parse at launch.
            text = pattern.sub(lambda _match: f"{key} = {encoded}", text)
        else:
            text = (text.rstrip() + "\n" if text.strip() else "") + f"{key} = {encoded}\n"
    temporary = path + ".partial"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _initialize_destination(destination: str, epoch: int) -> None:
    os.makedirs(destination, exist_ok=True)
    existing_epoch = _read_epoch(destination)
    index = _load_index(destination)
    if index["entries"] and existing_epoch not in {None, epoch}:
        raise RuntimeError("backup repository privacy epoch does not match")
    _write_epoch(destination, epoch)
    index["privacy_epoch"] = epoch
    _save_index(destination, index)


def _managed_bundle_names(cfg, destination: str) -> set[str]:
    index = _load_index(destination)
    names = {
        str(entry.get("name", ""))
        for entry in index["entries"]
        if _BUNDLE_RE.match(str(entry.get("name", "")))
    }
    try:
        material = recovery.load_repository_key(_key_path(cfg))
        key_id = material.passphrase_wrapper.key_id
    except Exception:
        key_id = ""
    if key_id:
        try:
            candidates = os.listdir(destination)
        except OSError:
            candidates = []
        for name in candidates:
            if not _BUNDLE_RE.match(name) or name in names:
                continue
            path = os.path.join(destination, name)
            try:
                info = recovery.inspect_bundle(path)
            except Exception:
                continue
            if info.passphrase_wrapper.key_id == key_id:
                names.add(name)
    return names


def _purge_destination(cfg, destination: str, epoch: int) -> int:
    removed = 0
    for name in sorted(_managed_bundle_names(cfg, destination)):
        path = os.path.join(destination, name)
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
    for partial in Path(destination).glob("faerie-fire-*.ffbackup.partial"):
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
    _write_epoch(destination, int(epoch))
    _save_index(destination, {
        "version": 1, "privacy_epoch": int(epoch), "entries": []})
    return removed


def _retry_pending_purges(cfg, state: dict) -> tuple[dict, int]:
    remaining: list[dict] = []
    removed = 0
    for record in _normalize_pending(state.get("pending_purges", []), state):
        destination = record["path"]
        if not _destination_healthy(destination):
            remaining.append(record)
            continue
        try:
            removed += _purge_destination(
                cfg, destination, int(record.get("epoch", state["privacy_epoch"])))
        except Exception:
            remaining.append(record)
    state["pending_purges"] = remaining
    state["last_error_code"] = "privacy_purge_pending" if remaining else ""
    return state, removed


def configure_instance_backup(cfg, *, primary_dir: str, passphrase: str,
                              secondary_dir: str = "",
                              include_blobs: bool = False) -> BackupStatus:
    try:
        with maintenance_lock():
            if (not str(primary_dir or "").strip()
                    or not os.path.isabs(os.fspath(primary_dir))):
                raise ValueError("an absolute primary backup folder is required")
            primary = os.path.abspath(os.fspath(primary_dir))
            if (str(secondary_dir or "").strip()
                    and not os.path.isabs(os.fspath(secondary_dir))):
                raise ValueError("the secondary backup folder must be absolute")
            secondary = (os.path.abspath(os.fspath(secondary_dir))
                         if secondary_dir else "")
            if not os.path.isabs(primary) or not primary:
                raise ValueError("an absolute primary backup folder is required")
            if secondary and os.path.normcase(primary) == os.path.normcase(secondary):
                raise ValueError("primary and secondary backup folders must differ")
            # Also validates the supported Windows automatic-key mode without
            # placing key material in configuration or logs.
            recovery.export_automatic_database_key()
            os.makedirs(_local_root(cfg), exist_ok=True)
            state = _load_state(cfg)
            key_path = _key_path(cfg)
            if os.path.exists(key_path):
                material = recovery.load_repository_key(key_path)
                recovered = recovery._unwrap_repository_key(  # package-private validation
                    material.passphrase_wrapper, str(passphrase))
                if recovered != material.key:
                    raise recovery.BackupAuthenticationError(
                        "recovery passphrase does not match this repository")
            else:
                recovery.create_repository_key(str(passphrase), key_path)
            epoch = int(state.get("privacy_epoch", 0) or 0)
            state, _removed = _retry_pending_purges(cfg, state)
            _initialize_destination(primary, epoch)
            if secondary:
                _initialize_destination(secondary, epoch)
            state.update({"primary": primary, "secondary": secondary})
            state["last_error_code"] = (
                "privacy_purge_pending" if state["pending_purges"] else "")
            _save_state(cfg, state)
            _update_config(cfg, {
                "instance_backup_enabled": True,
                "instance_backup_primary_dir": primary,
                "instance_backup_secondary_dir": secondary,
                "instance_backup_include_blobs": bool(include_blobs),
            })
        return backup_status(cfg)
    except Exception as error:
        return BackupStatus(False, False, False, False,
                            error_code=_safe_error("configure", error))


def _scheduled_times(cfg, last_verified: datetime | None):
    now_local = datetime.now().astimezone()
    hour = max(0, min(23, int(getattr(cfg, "instance_backup_hour", 20))))
    today = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if last_verified is None:
        due = True
    else:
        due = now_local >= today and last_verified.astimezone() < today
    next_attempt = today if now_local < today else today + timedelta(days=1)
    return due, next_attempt.astimezone(timezone.utc)


def backup_status(cfg) -> BackupStatus:
    try:
        state = _load_state(cfg)
        primary = str(getattr(cfg, "instance_backup_primary_dir", "") or state["primary"] or "")
        secondary = str(getattr(cfg, "instance_backup_secondary_dir", "") or state["secondary"] or "")
        enabled = bool(getattr(cfg, "instance_backup_enabled", False))
        configured = bool(primary and os.path.isabs(primary))
        last_value = str(state.get("last_verified_utc", "") or "")
        last = _parse_time(last_value)
        age = max(0.0, (_utc_now() - last).total_seconds()) if last else None
        due, next_attempt = _scheduled_times(cfg, last)
        index = _load_index(primary) if _destination_healthy(primary) else {"entries": []}
        retained = sum(
            1 for entry in index["entries"]
            if _BUNDLE_RE.match(str(entry.get("name", "")))
            and os.path.isfile(os.path.join(primary, str(entry["name"]))))
        pending = tuple(dict.fromkeys(
            str(item.get("label", "repository"))
            for item in state.get("pending_purges", [])))
        return BackupStatus(
            True, enabled, configured, bool(enabled and configured and due),
            last_value, age, _destination_healthy(primary),
            (_destination_healthy(secondary) if secondary else None), retained,
            next_attempt.isoformat().replace("+00:00", "Z"),
            str(state.get("last_error_code", "") or ""), bool(pending), pending,
            int(state.get("privacy_epoch", 0) or 0),
            _same_volume(primary, secondary),
            bool(getattr(cfg, "instance_backup_include_blobs", False)), bool(secondary))
    except Exception as error:
        return BackupStatus(False, False, False, False,
                            error_code=_safe_error("status", error))


def _bundle_name(now: datetime | None = None) -> str:
    now = now or _utc_now()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"faerie-fire-{stamp}-{uuid.uuid4().hex[:12]}.ffbackup"


def _entry_for(info: recovery.BundleInfo, name: str, reason: str) -> dict:
    return {"name": name, "bundle_id": info.bundle_id,
            "created_utc": info.created_utc, "privacy_epoch": info.privacy_epoch,
            "reason": str(reason or "manual")}


def _record_generation(destination: str, entry: dict, epoch: int) -> None:
    index = _load_index(destination)
    entries = [item for item in index["entries"] if item.get("name") != entry["name"]]
    entries.append(entry)
    index.update({"version": 1, "privacy_epoch": epoch, "entries": entries})
    _save_index(destination, index)


def _mirror(source: str, destination_dir: str, name: str,
            key: bytes) -> None:
    final = os.path.join(destination_dir, name)
    temporary = final + ".partial"
    if os.path.exists(final):
        raise FileExistsError("mirrored backup already exists")
    with open(source, "rb") as incoming, open(temporary, "xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    try:
        recovery.verify_bundle(temporary, key)
        os.replace(temporary, final)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def _retention_keep(entries: list[dict], daily: int, weekly: int,
                    monthly: int) -> set[str]:
    parsed = []
    for item in entries:
        created = _parse_time(str(item.get("created_utc", "")))
        name = str(item.get("name", ""))
        if created and _BUNDLE_RE.match(name):
            parsed.append((created, name, item))
    parsed.sort(reverse=True, key=lambda row: row[0])
    keep: set[str] = set()
    now = _utc_now()
    for limit, bucket in (
        (max(0, daily), lambda value: value.date().isoformat()),
        (max(0, weekly), lambda value: f"{value.isocalendar().year}-W{value.isocalendar().week:02d}"),
        (max(0, monthly), lambda value: value.strftime("%Y-%m")),
    ):
        seen = set()
        for created, name, _item in parsed:
            key = bucket(created)
            if key in seen or len(seen) >= limit:
                continue
            seen.add(key)
            keep.add(name)
    for created, name, item in parsed:
        if item.get("reason") == "pre_restore" and now - created <= timedelta(days=7):
            keep.add(name)
    if parsed and not keep:
        keep.add(parsed[0][1])
    return keep


def _apply_retention(cfg, destination: str) -> int:
    index = _load_index(destination)
    entries = index["entries"]
    keep = _retention_keep(
        entries, int(getattr(cfg, "instance_backup_keep_daily", 14)),
        int(getattr(cfg, "instance_backup_keep_weekly", 4)),
        int(getattr(cfg, "instance_backup_keep_monthly", 12)))
    retained = []
    for entry in entries:
        name = str(entry.get("name", ""))
        if name in keep and os.path.isfile(os.path.join(destination, name)):
            retained.append(entry)
        elif _BUNDLE_RE.match(name):
            try:
                os.remove(os.path.join(destination, name))
            except FileNotFoundError:
                pass
    index["entries"] = retained
    _save_index(destination, index)
    return len(retained)


def create_instance_backup(cfg, reason: str = "manual") -> BackupResult:
    state = _load_state(cfg)
    try:
        with maintenance_lock():
            state = _load_state(cfg)
            status = backup_status(cfg)
            if not status.ok or not status.enabled or not status.configured:
                return BackupResult(False, False, error_code="backup_not_configured")
            if state.get("pending_purges"):
                state, _removed = _retry_pending_purges(cfg, state)
                _save_state(cfg, state)
                if state.get("pending_purges"):
                    return BackupResult(
                        False, False, error_code="privacy_purge_pending")
            primary = os.path.abspath(getattr(cfg, "instance_backup_primary_dir"))
            secondary = str(getattr(cfg, "instance_backup_secondary_dir", "") or "")
            if not _destination_healthy(primary):
                return BackupResult(False, False, error_code="primary_unavailable")
            epoch = int(state.get("privacy_epoch", 0) or 0)
            if _read_epoch(primary) not in {None, epoch}:
                return BackupResult(False, False, error_code="primary_epoch_mismatch")
            material = recovery.load_repository_key(_key_path(cfg))
            work_parent = os.path.join(_local_root(cfg), "staging")
            os.makedirs(work_parent, exist_ok=True)
            work = tempfile.mkdtemp(prefix="generation-", dir=work_parent)
            payload = os.path.join(work, "payload")
            name = _bundle_name()
            destination = os.path.join(primary, name)
            try:
                build_profile_stage(
                    cfg, payload, privacy_epoch=epoch,
                    include_blobs=bool(getattr(
                        cfg, "instance_backup_include_blobs", False)))
                info = recovery.encrypt_bundle(
                    payload, destination, material.key, material.passphrase_wrapper,
                    {"privacy_epoch": epoch, "app_version": APP_VERSION,
                     "profile_format": PROFILE_VERSION, "reason": str(reason)})
            finally:
                _remove_tree(work)
            entry = _entry_for(info, name, str(reason))
            _record_generation(primary, entry, epoch)
            _apply_retention(cfg, primary)
            warnings = []
            mirrored = False
            if secondary:
                if not _destination_healthy(secondary):
                    warnings.append("secondary_unavailable")
                elif _read_epoch(secondary) not in {None, epoch}:
                    warnings.append("secondary_epoch_mismatch")
                else:
                    try:
                        _mirror(destination, secondary, name, material.key)
                        _record_generation(secondary, entry, epoch)
                        _apply_retention(cfg, secondary)
                        mirrored = True
                    except Exception as error:
                        warnings.append(_safe_error("secondary", error))
            state.update({"last_verified_utc": info.created_utc,
                          "last_error_code": "", "primary": primary,
                          "secondary": secondary})
            _save_state(cfg, state)
            return BackupResult(True, True, destination, mirrored,
                                info.bundle_id, info.created_utc, warnings=tuple(warnings))
    except MaintenanceBusy:
        return BackupResult(False, False, error_code="maintenance_busy")
    except Exception as error:
        try:
            state["last_error_code"] = _safe_error("backup", error)
            _save_state(cfg, state)
        except Exception:
            pass
        return BackupResult(False, False, error_code=_safe_error("backup", error))


def _newer_app_version(value: str) -> bool:
    def parts(text):
        match = re.fullmatch(r"(\d{4})\.(\d+)(?:\.(\d+))?", str(text or ""))
        if not match:
            raise ValueError("invalid application version")
        return tuple(int(item or 0) for item in match.groups())
    try:
        return parts(value) > parts(APP_VERSION)
    except ValueError:
        return True


def inspect_backup(path: str) -> BundleInfo:
    info = recovery.inspect_bundle(os.path.abspath(os.fspath(path)))
    meta = info.header_meta
    profile_version = int(meta.get("profile_format", 0) or 0)
    if profile_version > PROFILE_VERSION:
        raise RuntimeError("update Faerie Fire first")
    if _newer_app_version(str(meta.get("app_version", ""))):
        raise RuntimeError("update Faerie Fire first")
    return BundleInfo(True, info.version, info.bundle_id, info.created_utc,
                      info.privacy_epoch, str(meta.get("app_version", "")),
                      info.size_bytes, str(meta.get("reason", "")))


def _profile_root(cfg) -> str:
    config_path = _config_path(cfg)
    return os.path.dirname(config_path) if config_path else os.path.abspath(APP_DIR)


def _resolved_profile_path(cfg, attribute: str, default: str) -> str:
    value = os.fspath(getattr(cfg, attribute, "") or default)
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(_profile_root(cfg), value))


def _pending_source(cfg, path: str, pending: list) -> bool:
    absolute = os.path.abspath(path)
    legacy = {
        "primary": str(getattr(cfg, "instance_backup_primary_dir", "") or ""),
        "secondary": str(getattr(cfg, "instance_backup_secondary_dir", "") or ""),
    }
    for item in pending:
        if isinstance(item, dict):
            destination = str(item.get("path", "") or "")
        else:
            destination = legacy.get(str(item), "")
        if not destination:
            continue
        destination = os.path.abspath(destination)
        try:
            if os.path.commonpath((absolute, destination)) == destination:
                return True
        except ValueError:
            pass
    return False


def prepare_restore(cfg, path: str, passphrase: str) -> PreparedRestore:
    token = ""
    try:
        with maintenance_lock():
            recovery._require_automatic_database_key_mode()
            state = _load_state(cfg)
            if _pending_source(cfg, path, state.get("pending_purges", [])):
                return PreparedRestore(False, error_code="privacy_purge_pending")
            public = recovery.inspect_bundle(path)
            repository_epoch = _read_epoch(os.path.dirname(os.path.abspath(path)))
            if (repository_epoch is not None
                    and public.privacy_epoch != repository_epoch):
                return PreparedRestore(False, error_code="stale_repository_epoch")
            if public.privacy_epoch < int(state.get("privacy_epoch", 0) or 0):
                return PreparedRestore(False, error_code="stale_privacy_epoch")
            profile_version = int(public.header_meta.get("profile_format", 0) or 0)
            if profile_version > PROFILE_VERSION:
                return PreparedRestore(False, error_code="update_faerie_fire_first")
            if _newer_app_version(str(public.header_meta.get("app_version", ""))):
                return PreparedRestore(False,
                                       error_code="update_faerie_fire_first")
            token = uuid.uuid4().hex
            root = os.path.join(_restore_root(cfg), token)
            payload = os.path.join(root, "payload")
            os.makedirs(root, exist_ok=False)
            try:
                recovery.decrypt_bundle(path, str(passphrase), payload)
                manifest, _key = validate_profile_stage(
                    payload, minimum_epoch=int(state.get("privacy_epoch", 0) or 0))
                if (int(manifest["privacy_epoch"]) != public.privacy_epoch
                        or manifest["app_version"] != public.header_meta.get("app_version")):
                    raise RuntimeError("encrypted manifest does not match its envelope")
                target_data = os.path.dirname(os.path.abspath(cfg.memory_db_path))
                target_blob = _resolved_profile_path(
                    cfg, "blob_dir", os.path.join(target_data, "blobs"))
                rebase_staged_paths(
                    payload, _profile_root(cfg), data_dir=target_data,
                    blob_dir=target_blob)
                refresh_profile_manifest(payload)
                manifest, _key = validate_profile_stage(
                    payload, minimum_epoch=int(state.get("privacy_epoch", 0) or 0))
                recovery.create_repository_key(
                    str(passphrase), os.path.join(root, "rollback.key"))
                prepared = {
                    "version": 1, "token": token, "bundle_id": public.bundle_id,
                    "created_utc": public.created_utc,
                    "privacy_epoch": public.privacy_epoch,
                    "prepared_utc": _utc_now().isoformat().replace("+00:00", "Z"),
                    "manifest_created_utc": manifest["created_utc"],
                }
                _json_write(os.path.join(root, "prepared.json"), prepared)
            except Exception:
                _remove_tree(root)
                raise
            preview = {
                "whole_profile_replacement": True,
                "include_blobs": bool(manifest.get("include_blobs", False)),
                "file_count": len(manifest.get("files", {})),
                "external_integrations_paused": True,
            }
            return PreparedRestore(True, token, True, public.bundle_id,
                                   public.created_utc, public.privacy_epoch, preview)
    except MaintenanceBusy:
        return PreparedRestore(False, error_code="maintenance_busy")
    except recovery.BackupAuthenticationError:
        if token:
            _remove_tree(os.path.join(_restore_root(cfg), token))
        return PreparedRestore(False, error_code="wrong_passphrase_or_tampered")
    except Exception as error:
        if token:
            _remove_tree(os.path.join(_restore_root(cfg), token))
        return PreparedRestore(False, error_code=_safe_error("restore_prepare", error))


def _token_root(cfg, token: str) -> str:
    token = str(token or "")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("invalid prepared restore token")
    root = os.path.abspath(os.path.join(_restore_root(cfg), token))
    if os.path.dirname(root) != os.path.abspath(_restore_root(cfg)):
        raise ValueError("invalid prepared restore token")
    return root


def _profile_has_content(cfg) -> bool:
    for path in (cfg.memory_db_path, cfg.db_path):
        if not os.path.isfile(path):
            continue
        try:
            connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
            try:
                tables = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name!='meta'")]
                for table in tables:
                    quoted = '"' + str(table).replace('"', '""') + '"'
                    if connection.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                        return True
            finally:
                connection.close()
        except sqlite3.Error:
            return True
    for path in (os.path.join(APP_DIR, "projects"), os.path.join(APP_DIR, "vault")):
        if os.path.isdir(path) and any(Path(path).rglob("*")):
            return True
    return os.path.isfile(os.path.join(APP_DIR, "personas.json"))


def _activation_targets(profile: str, cfg) -> list[tuple[str, str]]:
    profile = os.path.abspath(profile)
    root = _profile_root(cfg)
    data_dir = os.path.dirname(os.path.abspath(cfg.memory_db_path))
    data_targets = (
        ("memory.db", os.path.abspath(cfg.memory_db_path)),
        ("living_computer.db", os.path.abspath(cfg.db_path)),
        ("notion", os.path.join(data_dir, "notion")),
        ("filed_dumps", os.path.join(data_dir, "filed_dumps")),
        ("journals", _resolved_profile_path(
            cfg, "journal_dir", os.path.join(data_dir, "notion"))),
        ("filing_journals", _resolved_profile_path(
            cfg, "filing_journal_dir", os.path.join(data_dir, "filed_dumps"))),
        ("blobs", _resolved_profile_path(
            cfg, "blob_dir", os.path.join(data_dir, "blobs"))),
    )
    root_targets = (
        ("projects", _resolved_profile_path(cfg, "projects_dir", "projects")),
        ("skills", _resolved_profile_path(cfg, "skills_dir", "skills")),
        ("vault", os.path.join(root, "vault")),
        ("personas.json", os.path.join(root, "personas.json")),
    )
    pairs: list[tuple[str, str]] = []
    seen: dict[str, str] = {}

    def add(source: str, target: str) -> None:
        if not os.path.lexists(source):
            return
        target = os.path.abspath(target)
        key = os.path.normcase(target)
        if key in seen and os.path.normcase(seen[key]) != os.path.normcase(source):
            raise RuntimeError("prepared restore destinations collide")
        if key not in seen:
            seen[key] = source
            pairs.append((source, target))

    for name, target in data_targets:
        add(os.path.join(profile, "data", name), target)
    for portrait in sorted(Path(os.path.join(profile, "data")).glob("portrait.*")):
        add(str(portrait), os.path.join(data_dir, portrait.name))
    for name, target in root_targets:
        add(os.path.join(profile, name), target)
    return pairs


def _base_authoritative_targets(cfg) -> list[str]:
    root = _profile_root(cfg)
    data_dir = os.path.dirname(os.path.abspath(cfg.memory_db_path))
    paths = [
        os.path.abspath(cfg.memory_db_path),
        os.path.abspath(cfg.db_path),
        os.path.join(data_dir, "notion"),
        os.path.join(data_dir, "filed_dumps"),
        _resolved_profile_path(cfg, "journal_dir", os.path.join(data_dir, "notion")),
        _resolved_profile_path(
            cfg, "filing_journal_dir", os.path.join(data_dir, "filed_dumps")),
        _resolved_profile_path(cfg, "blob_dir", os.path.join(data_dir, "blobs")),
        _resolved_profile_path(cfg, "projects_dir", "projects"),
        _resolved_profile_path(cfg, "skills_dir", "skills"),
        os.path.join(root, "vault"),
        os.path.join(root, "personas.json"),
        os.path.join(data_dir, "api_key.secret"),
        os.path.join(root, "data", "api_key.secret"),
        _resolved_profile_path(
            cfg, "browser_assistant_profile_dir",
            os.path.join(data_dir, "browser-profile")),
        recovery.crypto.automatic_key_path(),
        recovery.crypto.salt_path(),
    ]
    for database in (os.path.abspath(cfg.memory_db_path), os.path.abspath(cfg.db_path)):
        paths.extend((database + "-wal", database + "-shm"))
    result = []
    seen = set()
    for path in paths:
        absolute = os.path.abspath(path)
        key = os.path.normcase(absolute)
        if key not in seen:
            seen.add(key)
            result.append(absolute)
    return result


def _authoritative_targets(cfg) -> list[str]:
    targets = _base_authoritative_targets(cfg)
    data_dir = os.path.dirname(os.path.abspath(cfg.memory_db_path))
    targets.extend(str(path) for path in Path(data_dir).glob("portrait.*"))
    result = []
    seen = set()
    for path in targets:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            result.append(os.path.abspath(path))
    return result


def _move(source: str, destination: str) -> None:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(source, destination)


def _remove_path(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        _remove_tree(path)
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _remove_tree(path: str) -> None:
    if not os.path.lexists(path):
        return

    def onerror(_function, target, _error):
        try:
            os.chmod(target, 0o700)
            _function(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)


def _apply_settings(cfg, settings: dict) -> None:
    safe = {}
    for key, value in settings.items():
        lowered = str(key).lower()
        if (lowered.endswith(("_path", "_dir", "_folder", "_key", "_token", "_id"))
                or lowered in {"profile", "external_integrations_paused"}):
            continue
        if hasattr(cfg, key) or key == "language":
            safe[key] = value
    safe.update({
        "notion_sync_enabled": False,
        "notion_api_key": "",
        "notion_parent_page_id": "",
        "notion_curiosity_database_id": "",
        "notion_curiosity_data_source_id": "",
        "notion_curiosity_cover_file_upload_ids": [],
        "browser_assistant_enabled": False,
        "reminders_enabled": False,
    })
    _update_config(cfg, safe)


def _activation_journal_path(cfg) -> str:
    return os.path.join(_local_root(cfg), "activation.json")


def _journal_write(cfg, journal: dict) -> None:
    _json_write(_activation_journal_path(cfg), journal)


def _path_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            (os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except ValueError:
        return False


def _allowed_activation_path(cfg, path: str) -> bool:
    absolute = os.path.abspath(path)
    if os.path.normcase(absolute) in {
            os.path.normcase(item) for item in _base_authoritative_targets(cfg)}:
        return True
    data_dir = os.path.dirname(os.path.abspath(cfg.memory_db_path))
    return (os.path.normcase(os.path.dirname(absolute)) == os.path.normcase(data_dir)
            and os.path.basename(absolute).casefold().startswith("portrait."))


def _valid_activation_journal(cfg, journal: dict) -> bool:
    if not isinstance(journal, dict) or journal.get("version") != 1:
        return False
    token = str(journal.get("token", ""))
    if not _TOKEN_RE.fullmatch(token):
        return False
    rollback_root = os.path.abspath(str(journal.get("rollback_root", "")))
    expected_rollback = os.path.abspath(
        os.path.join(_local_root(cfg), "activation-rollback", token))
    if os.path.normcase(rollback_root) != os.path.normcase(expected_rollback):
        return False
    restore_root = os.path.abspath(str(journal.get("restore_root", "")))
    if os.path.normcase(restore_root) != os.path.normcase(_token_root(cfg, token)):
        return False
    if journal.get("phase") not in {
            "activating", "rolling_back", "rolled_back", "committed"}:
        return False
    moved = journal.get("moved_old", [])
    activated = journal.get("activated", [])
    if not isinstance(moved, list) or not isinstance(activated, list):
        return False
    for entry in moved:
        if (not isinstance(entry, dict)
                or not _allowed_activation_path(cfg, str(entry.get("original", "")))
                or not _path_within(str(entry.get("backup", "")), rollback_root)):
            return False
    for target in activated:
        if not _allowed_activation_path(cfg, str(target)):
            return False
    allowed_snapshots = {
        "config_snapshot": _config_path(cfg) or "",
        "state_snapshot": _state_path(cfg),
    }
    for name, expected in allowed_snapshots.items():
        entry = journal.get(name)
        if not isinstance(entry, dict):
            return False
        if os.path.normcase(os.path.abspath(str(entry.get("path", "")))) != (
                os.path.normcase(os.path.abspath(expected))):
            return False
        backup = str(entry.get("backup", ""))
        if backup and not _path_within(backup, rollback_root):
            return False
        if not isinstance(entry.get("existed"), bool):
            return False
    return True


def _snapshot_file(cfg, journal: dict, name: str, path: str) -> None:
    rollback_root = journal["rollback_root"]
    existed = os.path.lexists(path)
    backup = os.path.join(rollback_root, "snapshots", name + ".bak")
    if existed:
        if os.path.isdir(path) or os.path.islink(path):
            raise RuntimeError("restore control files must be regular files")
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        with open(path, "rb") as source, open(backup, "wb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    journal[name] = {"path": os.path.abspath(path), "backup": backup,
                     "existed": bool(existed)}
    _journal_write(cfg, journal)


def _record_old_move(cfg, journal: dict, original: str) -> None:
    backup = os.path.join(
        journal["rollback_root"], "old",
        f"{len(journal['moved_old']):06d}")
    journal["moved_old"].append({
        "original": os.path.abspath(original),
        "backup": os.path.abspath(backup),
    })
    _journal_write(cfg, journal)
    _move(original, backup)


def _record_activation(cfg, journal: dict, target: str) -> None:
    absolute = os.path.abspath(target)
    if os.path.normcase(absolute) not in {
            os.path.normcase(item) for item in journal["activated"]}:
        journal["activated"].append(absolute)
        _journal_write(cfg, journal)


def _restore_snapshot(entry: dict) -> bool:
    path = str(entry["path"])
    backup = str(entry.get("backup", ""))
    if entry["existed"]:
        if os.path.lexists(backup):
            if os.path.lexists(path):
                _remove_path(path)
            _move(backup, path)
            return True
        return os.path.lexists(path)
    if os.path.lexists(path):
        _remove_path(path)
    return not os.path.lexists(path)


def _cleanup_activation(cfg, journal: dict) -> bool:
    for path in (journal["restore_root"], journal["rollback_root"]):
        try:
            _remove_tree(path)
        except Exception:
            return False
        if os.path.lexists(path):
            return False
    try:
        os.remove(_activation_journal_path(cfg))
        _fsync_directory(os.path.dirname(_activation_journal_path(cfg)))
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _rollback_activation(cfg, journal: dict) -> bool:
    journal["phase"] = "rolling_back"
    _journal_write(cfg, journal)
    moved_by_original = {
        os.path.normcase(os.path.abspath(str(entry["original"]))): entry
        for entry in journal["moved_old"]
    }
    failed = False
    for target in reversed(journal["activated"]):
        entry = moved_by_original.get(os.path.normcase(os.path.abspath(target)))
        already_restored = bool(
            entry and not os.path.lexists(str(entry["backup"]))
            and os.path.lexists(str(entry["original"])))
        if already_restored:
            continue
        try:
            if os.path.lexists(target):
                _remove_path(target)
        except Exception:
            failed = True
    for entry in reversed(journal["moved_old"]):
        backup = str(entry["backup"])
        original = str(entry["original"])
        try:
            if os.path.lexists(backup):
                if os.path.lexists(original):
                    _remove_path(original)
                _move(backup, original)
            elif not os.path.lexists(original):
                failed = True
        except Exception:
            failed = True
    for name in ("config_snapshot", "state_snapshot"):
        try:
            if not _restore_snapshot(journal[name]):
                failed = True
        except Exception:
            failed = True
    if failed:
        _journal_write(cfg, journal)
        return False
    journal["phase"] = "rolled_back"
    _journal_write(cfg, journal)
    return _cleanup_activation(cfg, journal)


def _recover_interrupted_restore_locked(cfg) -> RestoreResult:
    path = _activation_journal_path(cfg)
    if not os.path.isfile(path):
        return RestoreResult(True, False)
    journal = _json_read(path, None)
    if not _valid_activation_journal(cfg, journal):
        return RestoreResult(False, False,
                             error_code="restore_recovery_journal_invalid")
    if journal["phase"] == "committed":
        cleaned = _cleanup_activation(cfg, journal)
        return RestoreResult(
            cleaned, True, False,
            error_code="" if cleaned else "restore_cleanup_pending")
    if journal["phase"] == "rolled_back":
        cleaned = _cleanup_activation(cfg, journal)
        return RestoreResult(
            cleaned, False, True,
            error_code="" if cleaned else "restore_cleanup_pending")
    recovered = _rollback_activation(cfg, journal)
    return RestoreResult(
        recovered, False, recovered,
        error_code="" if recovered else "restore_activation_rollback_incomplete")


def recover_interrupted_restore(cfg) -> RestoreResult:
    try:
        with maintenance_lock():
            return _recover_interrupted_restore_locked(cfg)
    except MaintenanceBusy:
        return RestoreResult(False, False, error_code="maintenance_busy")
    except Exception as error:
        return RestoreResult(
            False, False, error_code=_safe_error("restore_recovery", error))


def _retire_local_rollbacks(directory: str, keep_path: str) -> None:
    cutoff = _utc_now() - timedelta(days=7)
    for candidate in Path(directory).glob("faerie-fire-*.ffbackup"):
        if os.path.normcase(str(candidate)) == os.path.normcase(keep_path):
            continue
        try:
            info = recovery.inspect_bundle(str(candidate))
            created = _parse_time(info.created_utc)
            if created is not None and created < cutoff:
                candidate.unlink()
        except Exception:
            continue


def _create_rollback_snapshot(cfg, repository_key_path: str,
                              privacy_epoch: int) -> str:
    material = recovery.load_repository_key(repository_key_path)
    directory = os.path.join(_local_root(cfg), "rollbacks")
    os.makedirs(directory, exist_ok=True)
    name = _bundle_name()
    destination = os.path.join(directory, name)
    work_parent = os.path.join(_local_root(cfg), "staging")
    os.makedirs(work_parent, exist_ok=True)
    work = tempfile.mkdtemp(prefix="rollback-", dir=work_parent)
    payload = os.path.join(work, "payload")
    try:
        build_profile_stage(
            cfg, payload, privacy_epoch=int(privacy_epoch),
            include_blobs=bool(getattr(
                cfg, "instance_backup_include_blobs", False)))
        recovery.encrypt_bundle(
            payload, destination, material.key, material.passphrase_wrapper,
            {"privacy_epoch": int(privacy_epoch), "app_version": APP_VERSION,
             "profile_format": PROFILE_VERSION, "reason": "pre_restore"})
        recovery.verify_bundle(destination, material.key)
    finally:
        _remove_tree(work)
    _retire_local_rollbacks(directory, destination)
    return destination


def apply_prepared_restore(cfg, token: str) -> RestoreResult:
    rollback_bundle = ""
    try:
        with maintenance_lock():
            recovered = _recover_interrupted_restore_locked(cfg)
            if not recovered.ok:
                return recovered
            root = _token_root(cfg, token)
            prepared = _json_read(os.path.join(root, "prepared.json"), {})
            if not isinstance(prepared, dict) or prepared.get("token") != token:
                return RestoreResult(False, False, error_code="restore_token_invalid")
            payload = os.path.join(root, "payload")
            manifest, key_material = validate_profile_stage(
                payload, minimum_epoch=int(
                    _load_state(cfg).get("privacy_epoch", 0) or 0))
            if (int(manifest["privacy_epoch"]) != int(
                    prepared.get("privacy_epoch", -1))
                    or manifest["created_utc"] != prepared.get(
                        "manifest_created_utc", manifest["created_utc"])):
                return RestoreResult(
                    False, False, error_code="prepared_restore_changed")
            profile = os.path.join(payload, "profile")
            targets = _activation_targets(profile, cfg)
            expected_databases = {
                os.path.normcase(os.path.abspath(cfg.memory_db_path)),
                os.path.normcase(os.path.abspath(cfg.db_path)),
            }
            if not expected_databases.issubset({
                    os.path.normcase(target) for _source, target in targets}):
                return RestoreResult(
                    False, False, error_code="prepared_restore_missing_database")
            if _profile_has_content(cfg):
                rollback_key = os.path.join(root, "rollback.key")
                try:
                    rollback_bundle = _create_rollback_snapshot(
                        cfg, rollback_key, int(manifest["privacy_epoch"]))
                except Exception:
                    return RestoreResult(
                        False, False, error_code="rollback_backup_failed")
            rollback_root = os.path.join(
                _local_root(cfg), "activation-rollback", token)
            os.makedirs(rollback_root, exist_ok=False)
            config_path = _config_path(cfg)
            if not config_path:
                _remove_tree(rollback_root)
                return RestoreResult(
                    False, False, error_code="restore_config_path_unavailable")
            journal = {
                "version": 1,
                "token": token,
                "phase": "activating",
                "rollback_root": os.path.abspath(rollback_root),
                "restore_root": os.path.abspath(root),
                "moved_old": [],
                "activated": [],
                "config_snapshot": {},
                "state_snapshot": {},
            }
            _journal_write(cfg, journal)
            try:
                _snapshot_file(cfg, journal, "config_snapshot", config_path)
                _snapshot_file(cfg, journal, "state_snapshot", _state_path(cfg))
                for existing in _authoritative_targets(cfg):
                    if os.path.lexists(existing):
                        _record_old_move(cfg, journal, existing)
                for source, target in targets:
                    _record_activation(cfg, journal, target)
                    _move(source, target)
                key_path = recovery.crypto.automatic_key_path()
                salt_path = recovery.crypto.salt_path()
                _record_activation(cfg, journal, key_path)
                _record_activation(cfg, journal, salt_path)
                recovery.install_automatic_database_key(
                    key_material, key_path=key_path, salt_file=salt_path,
                    overwrite=False)
                _apply_settings(cfg, load_portable_settings(payload))
                state = _load_state(cfg)
                state["privacy_epoch"] = int(manifest["privacy_epoch"])
                state["last_error_code"] = ""
                _save_state(cfg, state)
                sqlite_integrity(os.path.abspath(cfg.memory_db_path))
                sqlite_integrity(os.path.abspath(cfg.db_path))
                journal["phase"] = "committed"
                _journal_write(cfg, journal)
            except Exception:
                rolled_back = _rollback_activation(cfg, journal)
                return RestoreResult(
                    False, False, rolled_back, rollback_bundle,
                    "" if rolled_back else
                    "restore_activation_rollback_incomplete")
            cleaned = _cleanup_activation(cfg, journal)
            try:
                recovery.crypto._automatic_passphrase.cache_clear()
                recovery.crypto._fernet.cache_clear()
            except Exception:
                pass
            return RestoreResult(
                True, True, False, rollback_bundle,
                warnings=() if cleaned else ("restore_cleanup_pending",))
    except MaintenanceBusy:
        return RestoreResult(False, False, error_code="maintenance_busy")
    except Exception as error:
        return RestoreResult(False, False,
                             error_code=_safe_error("restore_apply", error))

def discard_prepared_restore(token: str, cfg=None) -> None:
    if cfg is None:
        from .config import load
        cfg = load("config.toml")
    try:
        with maintenance_lock():
            _remove_tree(_token_root(cfg, token))
    except FileNotFoundError:
        pass


def purge_managed_backups(cfg, new_epoch: int) -> PurgeResult:
    try:
        with maintenance_lock():
            state = _load_state(cfg)
            current = int(state.get("privacy_epoch", 0) or 0)
            requested = int(new_epoch)
            if requested < current:
                return PurgeResult(False, current, error_code="epoch_not_advanced")
            existing = _normalize_pending(state.get("pending_purges", []), state)
            if requested == current and not existing:
                return PurgeResult(True, current)
            if requested > current:
                state["privacy_epoch"] = requested
                configured = (
                    ("primary", str(
                        getattr(cfg, "instance_backup_primary_dir", "")
                        or state.get("primary", "") or "")),
                    ("secondary", str(
                        getattr(cfg, "instance_backup_secondary_dir", "")
                        or state.get("secondary", "") or "")),
                )
                combined = list(existing)
                for label, destination in configured:
                    record = _pending_record(label, destination, requested)
                    if record is not None:
                        combined.append(record)
                state["pending_purges"] = _normalize_pending(combined, state)
                for record in state["pending_purges"]:
                    record["epoch"] = requested
            else:
                state["pending_purges"] = existing
            _save_state(cfg, state)
            state, removed = _retry_pending_purges(cfg, state)
            _save_state(cfg, state)
            labels = tuple(dict.fromkeys(
                str(record.get("label", "repository"))
                for record in state["pending_purges"]))
            return PurgeResult(
                True, requested, removed, labels, bool(labels),
                "privacy_purge_pending" if labels else "")
    except MaintenanceBusy:
        return PurgeResult(False, int(new_epoch), error_code="maintenance_busy")
    except Exception as error:
        return PurgeResult(False, int(new_epoch),
                           error_code=_safe_error("purge", error))
