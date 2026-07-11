import os
import sqlite3
import tempfile
from types import SimpleNamespace

from livingpc.backup import backup_memory
from livingpc.curiosity import CuriosityStore
from livingpc.forget import forget_memory
from livingpc.memory import MemoryStore
from livingpc.storage import EventLog


def test_forget_removes_fact_links_source_answer_and_backups():
    with tempfile.TemporaryDirectory() as directory:
        memory_db = os.path.join(directory, "memory.db")
        event_db = os.path.join(directory, "events.db")
        backup_dir = os.path.join(directory, "backups")
        mem = MemoryStore(memory_db)
        first = mem.add(
            "health", "routine", "walks", raw_source="I walk",
            source_refs=[{
                "kind": "triage", "window_start": "2026-07-01T09:00:00",
                "window_end": "2026-07-01T10:00:00",
            }],
        )
        second = mem.add("health", "goal", "strength")
        mem.add_association(first, second, status="approved")
        curiosities = CuriosityStore(memory_db)
        curiosity_id = curiosities.add_curiosity("health", "health")
        item_id = curiosities.add_item(curiosity_id, "question", "What do you do?")
        curiosities.mark_answered(item_id, "I walk", first)
        curiosities.close()
        mem.close()
        events = EventLog(event_db)
        events.log_event("ocr", app="App.exe", text_payload="I walk",
                         ts="2026-07-01T09:30:00")
        events.close()
        backup_memory(memory_db, backup_dir)

        cfg = SimpleNamespace(
            db_path=event_db, memory_db_path=memory_db, backup_dir=backup_dir,
            notion_sync_enabled=False,
        )
        result = forget_memory(cfg, first)
        assert result["backups_removed"] == 1
        assert result["source_events_removed"] == 1
        assert os.listdir(backup_dir) == []

        mem = MemoryStore(memory_db)
        try:
            assert mem.get(first) is None
            assert mem.get(second) is not None
            assert mem.list_associations(active_only=False) == []
        finally:
            mem.close()
        conn = sqlite3.connect(memory_db)
        try:
            answer, resulting = conn.execute(
                "SELECT answer, resulting_memory_id FROM curiosity_item WHERE id=?",
                (item_id,),
            ).fetchone()
            assert answer is None and resulting is None
        finally:
            conn.close()
        events = EventLog(event_db)
        try:
            assert events.count() == 0
        finally:
            events.close()
