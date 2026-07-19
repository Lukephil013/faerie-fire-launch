from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from livingpc import backup_prompt


def _cfg(tmp_path, **overrides):
    values = {
        "memory_db_path": str(tmp_path / "data" / "memory.db"),
        "instance_backup_enabled": False,
        "instance_backup_primary_dir": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _create_db(cfg):
    path = cfg.memory_db_path
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE memory(id INTEGER PRIMARY KEY, value TEXT,
                            status TEXT DEFAULT 'active');
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE companion_message(id INTEGER PRIMARY KEY, role TEXT,
                                       content TEXT);
        CREATE TABLE curiosity_item(id INTEGER PRIMARY KEY, text TEXT);
    """)
    connection.commit()
    return connection


def test_missing_database_never_prompts(tmp_path):
    cfg = _cfg(tmp_path)
    state = backup_prompt.prompt_state(cfg)
    assert state["ok"] and not state["show"]
    assert state["counts"] == {
        "memories": 0, "chat_messages": 0, "investigation_items": 0}
    assert not backup_prompt.snooze_prompt(cfg)["ok"]


def test_below_threshold_does_not_prompt(tmp_path):
    cfg = _cfg(tmp_path)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO memory(value) VALUES(?)", [("m",)] * 3)
    connection.executemany(
        "INSERT INTO companion_message(role,content) VALUES('user',?)",
        [("hi",)] * 4)
    connection.commit(); connection.close()
    assert not backup_prompt.prompt_state(cfg)["show"]


def test_memory_threshold_prompts(tmp_path):
    cfg = _cfg(tmp_path)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO memory(value) VALUES(?)",
        [("m",)] * backup_prompt.MEMORY_THRESHOLD)
    connection.commit(); connection.close()
    state = backup_prompt.prompt_state(cfg)
    assert state["show"]
    assert state["counts"]["memories"] == backup_prompt.MEMORY_THRESHOLD


def test_inactive_memories_and_assistant_messages_do_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO memory(value,status) VALUES(?,'superseded')",
        [("m",)] * 40)
    connection.executemany(
        "INSERT INTO companion_message(role,content) VALUES('assistant',?)",
        [("hi",)] * 40)
    connection.commit(); connection.close()
    state = backup_prompt.prompt_state(cfg)
    assert not state["show"]
    assert state["counts"]["memories"] == 0
    assert state["counts"]["chat_messages"] == 0


def test_chat_and_investigation_thresholds_prompt(tmp_path):
    cfg = _cfg(tmp_path)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO companion_message(role,content) VALUES('user',?)",
        [("hi",)] * backup_prompt.CHAT_MESSAGE_THRESHOLD)
    connection.commit(); connection.close()
    assert backup_prompt.prompt_state(cfg)["show"]

    cfg2 = _cfg(tmp_path / "second")
    connection = _create_db(cfg2)
    connection.executemany(
        "INSERT INTO curiosity_item(text) VALUES(?)",
        [("q",)] * backup_prompt.INVESTIGATION_ITEM_THRESHOLD)
    connection.commit(); connection.close()
    assert backup_prompt.prompt_state(cfg2)["show"]


def test_configured_backup_suppresses_prompt(tmp_path):
    cfg = _cfg(tmp_path, instance_backup_enabled=True)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO memory(value) VALUES(?)", [("m",)] * 50)
    connection.commit(); connection.close()
    state = backup_prompt.prompt_state(cfg)
    assert state["configured"] and not state["show"]
    assert not backup_prompt.prompt_state(
        _cfg(tmp_path, instance_backup_primary_dir="D:\\Backups"))["show"]


def test_snooze_suppresses_until_expiry(tmp_path):
    cfg = _cfg(tmp_path)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO memory(value) VALUES(?)", [("m",)] * 50)
    connection.commit(); connection.close()
    assert backup_prompt.prompt_state(cfg)["show"]
    snoozed = backup_prompt.snooze_prompt(cfg)
    assert snoozed["ok"] and snoozed["snoozed_until"].endswith("Z")
    assert not backup_prompt.prompt_state(cfg)["show"]

    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    connection = sqlite3.connect(cfg.memory_db_path)
    connection.execute(
        "UPDATE meta SET value=? WHERE key='backup_prompt_snoozed_until'",
        (expired,))
    connection.commit(); connection.close()
    assert backup_prompt.prompt_state(cfg)["show"]


def test_dismiss_is_permanent(tmp_path):
    cfg = _cfg(tmp_path)
    connection = _create_db(cfg)
    connection.executemany(
        "INSERT INTO memory(value) VALUES(?)", [("m",)] * 50)
    connection.commit(); connection.close()
    assert backup_prompt.dismiss_prompt(cfg)["ok"]
    state = backup_prompt.prompt_state(cfg)
    assert state["dismissed"] and not state["show"]
