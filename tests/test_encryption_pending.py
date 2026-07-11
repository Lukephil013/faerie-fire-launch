"""Tests for field-level encryption and the pending-review flow."""
import importlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_crypto(passphrase=None, salt_dir=None):
    if passphrase is None:
        os.environ.pop("LIVINGPC_DB_KEY", None)
    else:
        os.environ["LIVINGPC_DB_KEY"] = passphrase
    if salt_dir:
        os.environ["LIVINGPC_SALT_FILE"] = os.path.join(salt_dir, "secret.salt")
    from livingpc import crypto
    importlib.reload(crypto)
    return crypto


def test_passthrough_without_key():
    crypto = _fresh_crypto(None)
    assert crypto.enabled() is False
    assert crypto.enc("hello world") == "hello world"
    assert crypto.dec("hello world") == "hello world"


def test_roundtrip_with_key():
    with tempfile.TemporaryDirectory() as d:
        crypto = _fresh_crypto("correct horse battery staple", d)
        assert crypto.enabled() is True
        token = crypto.enc("Jinx, Jhin — secret on-screen text")
        assert crypto.is_encrypted(token)
        assert "Jinx" not in token                      # actually encrypted
        assert crypto.dec(token) == "Jinx, Jhin — secret on-screen text"
        assert crypto.dec("plaintext stays") == "plaintext stays"  # mixed data ok


def test_wrong_key_does_not_crash():
    with tempfile.TemporaryDirectory() as d:
        crypto = _fresh_crypto("right-key", d)
        token = crypto.enc("sensitive")
        crypto2 = _fresh_crypto("wrong-key", d)   # same salt, different passphrase
        out = crypto2.dec(token)
        assert "sensitive" not in out             # can't read it
        assert out.startswith("[")                # placeholder, no exception
    _fresh_crypto(None)  # reset env for other tests


def test_encryption_failure_never_falls_back_to_plaintext():
    with tempfile.TemporaryDirectory() as d:
        crypto = _fresh_crypto("required-key", d)
        original = crypto._fernet
        crypto._fernet = lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with pytest.raises(crypto.EncryptionError):
                crypto.enc("sensitive")
        finally:
            crypto._fernet = original
    _fresh_crypto(None)


def test_encrypted_storage_decrypts_on_read():
    """End-to-end: write via EventLog with a key, aggregation should see plaintext."""
    with tempfile.TemporaryDirectory() as d:
        _fresh_crypto("a-passphrase", d)
        # import after key is set so modules pick up the active crypto
        from livingpc.storage import EventLog
        from livingpc.triage.aggregate import build_day_summary
        import livingpc.storage as storage_mod
        import livingpc.triage.aggregate as agg_mod
        importlib.reload(storage_mod)
        importlib.reload(agg_mod)

        ev = storage_mod.EventLog(os.path.join(d, "e.db"))
        sid = ev.start_session("game.exe", "in-game", ts="2026-06-24T10:00:00")
        ev.end_session(sid, "2026-06-24T10:30:00")
        ev.log_event("ocr", app="game.exe", window_title="in-game",
                     text_payload="Caitlyn headshot", session_id=sid,
                     ts="2026-06-24T10:10:00")

        # stored value on disk must be ciphertext...
        raw = ev.conn.execute(
            "SELECT text_payload FROM events WHERE type='ocr'"
        ).fetchone()[0]
        assert "Caitlyn" not in raw
        # ...but aggregation decrypts it back
        summary = agg_mod.build_day_summary(ev, "2026-06-24")
        assert "Caitlyn headshot" in summary
        ev.close()
    _fresh_crypto(None)


def test_existing_sensitive_tables_migrate_idempotently():
    with tempfile.TemporaryDirectory() as d:
        _fresh_crypto(None)
        from livingpc.config import Config
        from livingpc.curiosity import CuriosityStore
        from livingpc.inference import InferenceStore
        from livingpc.memory import MemoryStore
        from livingpc.storage import EventLog

        cfg = Config(db_path=os.path.join(d, "events.db"),
                     memory_db_path=os.path.join(d, "memory.db"))
        events = EventLog(cfg.db_path)
        events.log_event("ocr", app="App.exe", window_title="Secret title",
                         text_payload="Secret payload")
        events.close()
        memory = MemoryStore(cfg.memory_db_path)
        memory.add("private", "note", "Secret fact")
        memory.close()
        inference = InferenceStore(cfg.memory_db_path)
        inference.add_candidate("private", "Secret belief", confidence=0.9)
        inference.close()
        curiosity = CuriosityStore(cfg.memory_db_path)
        curiosity.add_curiosity("Secret goal", "private")
        curiosity.close()

        crypto = _fresh_crypto("migration-key", d)
        from encrypt_db import encrypt_existing
        first = encrypt_existing(cfg)
        second = encrypt_existing(cfg)
        assert first["event_fields"] >= 2
        assert first["memory_fields"] >= 3
        assert second == {"event_fields": 0, "memory_fields": 0, "blobs": 0}

        memory = MemoryStore(cfg.memory_db_path)
        try:
            raw = memory.conn.execute("SELECT value FROM memory").fetchone()[0]
            assert crypto.is_encrypted(raw)
            assert memory.active_as_dicts()[0]["value"] == "Secret fact"
        finally:
            memory.close()
    _fresh_crypto(None)


def test_pending_flow():
    _fresh_crypto(None)
    with tempfile.TemporaryDirectory() as d:
        import livingpc.memory as mem_mod
        importlib.reload(mem_mod)
        mem = mem_mod.MemoryStore(os.path.join(d, "m.db"))
        mem.add_pending("statement",
                        {"category": "LoL", "attribute": "pool", "value": "Jinx"},
                        "2026-06-24")
        assert mem.count_pending() == 1
        rows = mem.list_pending()
        assert rows[0]["kind"] == "statement"
        mem.clear_pending(rows[0]["id"])
        assert mem.count_pending() == 0
        mem.close()


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
