"""Idempotent migration: encrypt existing plaintext sensitive data in place.

Windows uses the automatic DPAPI-protected key unless LIVINGPC_DB_KEY is set.
Rows and blobs already encrypted are skipped, so this is safe to re-run.

    setx LIVINGPC_DB_KEY "your-passphrase"   (reopen terminal)
    python encrypt_db.py
"""
from __future__ import annotations

import os

from livingpc.config import load
from livingpc.storage import EventLog
from livingpc.memory import MemoryStore
from livingpc import crypto


def _encrypt_columns(conn, columns: list[tuple[str, str]]) -> int:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    changed = 0
    for table, column in columns:
        if table not in tables:
            continue
        available = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in available:
            continue
        rows = conn.execute(
            f"SELECT rowid, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} != ''"
        ).fetchall()
        for rowid, value in rows:
            if not crypto.is_encrypted(value):
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (crypto.enc(value), rowid),
                )
                changed += 1
    conn.commit()
    return changed


def encrypt_existing(cfg) -> dict:
    ev = EventLog(cfg.db_path)
    try:
        event_fields = _encrypt_columns(ev.conn, [
            ("events", "window_title"), ("events", "text_payload"),
            ("sessions", "window_title"),
        ])
        blobs = 0
        rows = ev.conn.execute(
            "SELECT id, blob_ref FROM events WHERE blob_ref IS NOT NULL"
        ).fetchall()
        for row in rows:
            path = row["blob_ref"]
            if not path or path.endswith(".enc") or not os.path.exists(path):
                continue
            encrypted_path = path + ".enc"
            with open(path, "rb") as source:
                payload = crypto.enc_bytes(source.read())
            with open(encrypted_path, "xb") as target:
                target.write(payload)
            os.remove(path)
            ev.conn.execute(
                "UPDATE events SET blob_ref=? WHERE id=?", (encrypted_path, row["id"]))
            blobs += 1
        ev.conn.commit()
    finally:
        ev.close()

    mem = MemoryStore(cfg.memory_db_path)
    try:
        memory_fields = _encrypt_columns(mem.conn, [
            ("memory", "value"), ("memory", "source_text"),
            ("rejected", "label"), ("inference", "statement"),
            ("evidence", "observation"), ("feedback_note", "questions"),
            ("feedback_note", "user_text"), ("feedback_note", "lesson"),
            ("feedback_note", "refs"), ("curiosity", "directive"),
            ("inference_inquiry", "prompt"),
            ("inference_inquiry", "draft_claim"),
            ("inference_inquiry_message", "content"),
            ("curiosity", "label"), ("curiosity_item", "text"),
            ("curiosity_item", "answer"), ("clarification", "question"),
            ("clarification", "answer"), ("curiosity_metric_checkin", "note"),
            ("companion_chat", "title"), ("companion_message", "content"),
            ("goal_node", "title"), ("goal_node", "description"),
            ("goal_node", "notes"), ("goal_evidence_link", "label"),
            ("goal_plan_session", "draft_json"), ("goal_plan_session", "summary"),
            ("goal_plan_message", "content"), ("goal_agent_state", "brief"),
            ("goal_agent_state", "evidence_summary"), ("goal_agent_state", "blockers"),
            ("goal_agent_state", "next_focus"), ("goal_agent_assessment", "report_json"),
            ("goal_agent_question", "text"), ("goal_agent_question", "answer"),
            ("goal_agent_proposal", "payload_json"), ("goal_agent_proposal", "rationale"),
            ("goal_agent_message", "content"),
            ("goal_harvest", "draft_json"),
            ("goal_harvest_route", "reason"),
            ("goal_agent_memory_candidate", "category"),
            ("goal_agent_memory_candidate", "attribute"),
            ("goal_agent_memory_candidate", "value"),
            ("goal_agent_memory_candidate", "source_text"),
        ])
    finally:
        mem.close()
    return {"event_fields": event_fields, "memory_fields": memory_fields,
            "blobs": blobs}


def main() -> None:
    if not crypto.enabled():
        print("At-rest encryption is unavailable on this platform/configuration.")
        return

    cfg = load("config.toml")

    counts = encrypt_existing(cfg)
    print(f"Encrypted {counts['event_fields']} event field(s), "
          f"{counts['memory_fields']} memory field(s), and "
          f"{counts['blobs']} blob(s).")
    print("Keep data/secret.key and data/secret.salt with encrypted backups.")


if __name__ == "__main__":
    main()
