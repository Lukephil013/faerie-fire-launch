from __future__ import annotations

import base64
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from livingpc import backup_profile, instance_backup, recovery


PASSPHRASE = "correct horse battery staple"


def _cfg(root: Path, primary: Path | None = None, secondary: Path | None = None):
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    config = root / "config.toml"
    config.write_text('profile = "launch"\n', encoding="utf-8")
    return SimpleNamespace(
        profile="launch",
        memory_db_path=str(data / "memory.db"),
        db_path=str(data / "living_computer.db"),
        blob_dir=str(data / "blobs"),
        projects_dir=str(root / "projects"),
        skills_dir=str(root / "skills"),
        journal_dir=str(data / "notion"),
        filing_journal_dir=str(data / "filed_dumps"),
        blocklist=["Password.exe"],
        instance_backup_enabled=False,
        instance_backup_primary_dir=str(primary or ""),
        instance_backup_secondary_dir=str(secondary or ""),
        instance_backup_hour=20,
        instance_backup_keep_daily=14,
        instance_backup_keep_weekly=4,
        instance_backup_keep_monthly=12,
        instance_backup_include_blobs=False,
        notion_sync_enabled=True,
        browser_assistant_enabled=True,
        reminders_enabled=True,
        _config_path=str(config),
    )


def _create_profile(root: Path, cfg):
    (root / "projects").mkdir()
    (root / "projects" / "plan.md").write_text("private plan", encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "my-skill.md").write_text("personal skill", encoding="utf-8")
    (root / "vault").mkdir()
    (root / "vault" / "journal.md").write_text("old export", encoding="utf-8")
    (root / "personas.json").write_text('{"name":"Faerie"}', encoding="utf-8")
    (root / "data" / "portrait.jpg").write_bytes(b"portrait")
    (root / "data" / "notion").mkdir()
    (root / "data" / "notion" / "entry.md").write_text("journal", encoding="utf-8")
    (root / "data" / "filed_dumps").mkdir()
    (root / "data" / "filed_dumps" / "dump.md").write_text("dump", encoding="utf-8")
    (root / "data" / "blobs").mkdir()
    (root / "data" / "blobs" / "screen.png").write_bytes(b"private screenshot")

    memory = sqlite3.connect(cfg.memory_db_path)
    memory.execute("PRAGMA journal_mode=WAL")
    memory.executescript("""
        CREATE TABLE memory(id INTEGER PRIMARY KEY, value TEXT, notion_page_id TEXT);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE browser_site_permissions(origin TEXT, approved INTEGER);
        CREATE TABLE browser_tasks(id TEXT, url TEXT);
    """)
    memory.execute("INSERT INTO memory(value,notion_page_id) VALUES(?,?)",
                   ("open WAL memory", "external-page"))
    memory.execute("INSERT INTO browser_site_permissions VALUES('https://private.test',1)")
    memory.execute("INSERT INTO browser_tasks VALUES('task','https://private.test/form')")
    memory.commit()

    activity = sqlite3.connect(cfg.db_path)
    activity.execute("PRAGMA journal_mode=WAL")
    activity.execute(
        "CREATE TABLE event(id INTEGER PRIMARY KEY,text_payload TEXT,blob_ref TEXT)")
    activity.execute("INSERT INTO event(text_payload,blob_ref) VALUES(?,?)",
                     ("OCR remains", str(root / "data" / "blobs" / "screen.png")))
    activity.commit()
    return memory, activity


@pytest.fixture
def automatic_keys(monkeypatch):
    material = recovery.DatabaseKeyMaterial(
        secret=b"K" * 32,
        salt_file=base64.b64encode(b"S" * 16),
    )
    monkeypatch.delenv("LIVINGPC_DB_KEY", raising=False)
    monkeypatch.delenv("LIVINGPC_KEY_FILE", raising=False)
    monkeypatch.delenv("LIVINGPC_SALT_FILE", raising=False)
    monkeypatch.delenv("LIVINGPC_AUTO_ENCRYPTION", raising=False)
    monkeypatch.setattr(recovery.crypto, "dpapi_available", lambda: True)
    monkeypatch.setattr(recovery.crypto, "protect_secret", lambda value: b"dpapi:" + value)
    monkeypatch.setattr(
        recovery.crypto, "unprotect_secret",
        lambda value: value[6:] if value.startswith(b"dpapi:") else value)
    monkeypatch.setattr(recovery, "export_automatic_database_key", lambda **_kwargs: material)
    original_create = recovery.create_repository_key
    monkeypatch.setattr(
        recovery, "create_repository_key",
        lambda passphrase, path: original_create(
            passphrase, path, _scrypt_n=2 ** 10))
    return material


def _point_app(monkeypatch, root: Path):
    monkeypatch.setattr(instance_backup, "APP_DIR", str(root))
    monkeypatch.setattr(backup_profile, "APP_DIR", str(root))


def test_cross_instance_round_trip_snapshots_wal_and_scrubs_private_state(
        tmp_path, monkeypatch, automatic_keys):
    source = tmp_path / "source"
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    cfg = _cfg(source, primary, secondary)
    memory, activity = _create_profile(source, cfg)
    _point_app(monkeypatch, source)

    configured = instance_backup.configure_instance_backup(
        cfg, primary_dir=str(primary.resolve()), secondary_dir=str(secondary.resolve()),
        passphrase=PASSPHRASE, include_blobs=False)
    assert configured.ok and configured.enabled, configured.error_code
    result = instance_backup.create_instance_backup(cfg, reason="manual")
    assert result.ok and result.verified and result.mirrored
    assert Path(result.path).is_file()
    assert (secondary / Path(result.path).name).is_file()
    assert b"private plan" not in Path(result.path).read_bytes()
    assert b"private screenshot" not in Path(result.path).read_bytes()
    memory.close(); activity.close()

    unpacked = tmp_path / "unpacked"
    recovery.decrypt_bundle(result.path, PASSPHRASE, str(unpacked))
    manifest, restored_keys = backup_profile.validate_profile_stage(unpacked)
    assert manifest["privacy_epoch"] == 0
    assert restored_keys == automatic_keys
    restored_memory = sqlite3.connect(unpacked / "profile" / "data" / "memory.db")
    try:
        assert restored_memory.execute("SELECT value FROM memory").fetchone()[0] == "open WAL memory"
        assert restored_memory.execute("SELECT notion_page_id FROM memory").fetchone()[0] is None
        assert restored_memory.execute("SELECT COUNT(*) FROM browser_tasks").fetchone()[0] == 0
        assert restored_memory.execute(
            "SELECT COUNT(*) FROM browser_site_permissions").fetchone()[0] == 0
    finally:
        restored_memory.close()
    restored_activity = sqlite3.connect(
        unpacked / "profile" / "data" / "living_computer.db")
    try:
        assert restored_activity.execute(
            "SELECT text_payload,blob_ref FROM event").fetchone() == ("OCR remains", None)
    finally:
        restored_activity.close()
    assert (unpacked / "profile" / "projects" / "plan.md").read_text() == "private plan"
    assert not (unpacked / "profile" / "data" / "blobs").exists()

    target = tmp_path / "replacement"
    target_cfg = _cfg(target)
    _point_app(monkeypatch, target)
    key_path = target / "data" / "secret.key"
    salt_path = target / "data" / "secret.salt"
    monkeypatch.setattr(recovery.crypto, "automatic_key_path", lambda: str(key_path))
    monkeypatch.setattr(recovery.crypto, "salt_path", lambda: str(salt_path))

    wrong = instance_backup.prepare_restore(target_cfg, result.path, "wrong passphrase")
    assert not wrong.ok and not Path(target_cfg.memory_db_path).exists()
    prepared = instance_backup.prepare_restore(target_cfg, result.path, PASSPHRASE)
    assert prepared.ok and prepared.verified and prepared.token
    restored = instance_backup.apply_prepared_restore(target_cfg, prepared.token)
    assert restored.ok and restored.activated and not restored.rolled_back
    assert Path(target_cfg.memory_db_path).is_file()
    assert (target / "projects" / "plan.md").read_text() == "private plan"
    assert (target / "data" / "portrait.jpg").read_bytes() == b"portrait"
    assert key_path.read_bytes() == b"dpapi:" + automatic_keys.secret
    assert salt_path.read_bytes() == automatic_keys.salt_file
    config_text = (target / "config.toml").read_text(encoding="utf-8")
    assert "notion_sync_enabled = false" in config_text
    assert "browser_assistant_enabled = false" in config_text
    assert "reminders_enabled = false" in config_text


def test_forget_purge_tracks_offline_destination_and_blocks_new_uploads(
        tmp_path, monkeypatch, automatic_keys):
    source = tmp_path / "source"
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    cfg = _cfg(source, primary, secondary)
    memory, activity = _create_profile(source, cfg)
    _point_app(monkeypatch, source)
    assert instance_backup.configure_instance_backup(
        cfg, primary_dir=str(primary.resolve()), secondary_dir=str(secondary.resolve()),
        passphrase=PASSPHRASE).ok
    created = instance_backup.create_instance_backup(cfg)
    assert created.ok
    memory.close(); activity.close()
    for child in secondary.rglob("*"):
        if child.is_file():
            child.unlink()
    for child in sorted(secondary.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    secondary.rmdir()

    purged = instance_backup.purge_managed_backups(cfg, 1)
    assert purged.ok and purged.privacy_epoch == 1
    assert purged.pending_destinations == ("secondary",)
    assert not list(primary.glob("*.ffbackup"))
    blocked = instance_backup.create_instance_backup(cfg, reason="post_forget")
    assert not blocked.ok and blocked.error_code == "privacy_purge_pending"


def test_retention_preserves_daily_weekly_monthly_and_recent_rollback():
    now = datetime.now(timezone.utc)
    entries = []
    for days in range(70):
        created = now - timedelta(days=days)
        entries.append({
            "name": f"faerie-fire-{created.strftime('%Y%m%dT%H%M%SZ')}-{days:012x}.ffbackup",
            "created_utc": created.isoformat().replace("+00:00", "Z"),
            "reason": "pre_restore" if days == 6 else "scheduled",
        })
    kept = instance_backup._retention_keep(entries, 14, 4, 3)
    assert entries[0]["name"] in kept
    assert entries[6]["name"] in kept
    assert len(kept) >= 14
    assert len(kept) < len(entries)


def test_configuration_rejects_relative_or_missing_primary(tmp_path, automatic_keys):
    cfg = _cfg(tmp_path / "profile")
    result = instance_backup.configure_instance_backup(
        cfg, primary_dir="", passphrase=PASSPHRASE)
    assert not result.ok and result.error_code.startswith("configure_")
    relative = instance_backup.configure_instance_backup(
        cfg, primary_dir="relative", passphrase=PASSPHRASE)
    assert not relative.ok and relative.error_code.startswith("configure_")


