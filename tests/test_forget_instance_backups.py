from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from livingpc.forget import forget_memory
from livingpc.memory import MemoryStore


def _profile(tmp_path):
    memory_db = str(tmp_path / "memory.db")
    event_db = str(tmp_path / "events.db")
    memory = MemoryStore(memory_db)
    try:
        memory_id = memory.add("self", "preference", "tea")
    finally:
        memory.close()
    cfg = SimpleNamespace(
        memory_db_path=memory_db,
        db_path=event_db,
        backup_dir=str(tmp_path / "legacy"),
        notion_sync_enabled=False,
        instance_backup_enabled=True,
        instance_backup_primary_dir=str(tmp_path / "portable"),
        instance_backup_secondary_dir="",
    )
    return cfg, memory_id


def _engine(monkeypatch, *, pending=()):
    calls = []
    module = ModuleType("livingpc.instance_backup")
    module.backup_status = lambda _cfg: SimpleNamespace(ok=True, privacy_epoch=4)

    def purge(_cfg, epoch):
        calls.append(("purge", epoch))
        return SimpleNamespace(
            ok=True, privacy_epoch=epoch, removed=3,
            pending_destinations=tuple(pending), error_code="")

    def create(_cfg, reason):
        calls.append(("create", reason))
        return SimpleNamespace(ok=True, verified=True, error_code="")

    module.purge_managed_backups = purge
    module.create_instance_backup = create
    monkeypatch.setitem(sys.modules, "livingpc.instance_backup", module)
    return calls


def test_forget_advances_epoch_purges_and_creates_fresh_baseline(tmp_path,
                                                                 monkeypatch):
    cfg, memory_id = _profile(tmp_path)
    calls = _engine(monkeypatch)

    result = forget_memory(cfg, memory_id)

    assert calls == [("purge", 5), ("create", "post_forget")]
    assert result["backup_privacy_epoch"] == 5
    assert result["managed_backups_removed"] == 3
    assert result["post_forget_baseline"] is True
    assert result["backup_purge_pending"] == []


def test_offline_purge_pending_suppresses_new_generation(tmp_path, monkeypatch):
    cfg, memory_id = _profile(tmp_path)
    calls = _engine(monkeypatch, pending=("secondary",))

    result = forget_memory(cfg, memory_id)

    assert calls == [("purge", 5)]
    assert result["backup_purge_pending"] == ["secondary"]
    assert result["post_forget_baseline"] is False
    assert "instance_backup_purge:pending" in result["warnings"]

