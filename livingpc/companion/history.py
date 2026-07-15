"""Encrypted persistent multi-chat history for the Companion."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import crypto
from ..db import connect as db_connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_chat (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    proposals_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companion_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES companion_chat(id),
    CHECK (role IN ('user','assistant'))
);
CREATE INDEX IF NOT EXISTS idx_companion_message_chat
ON companion_message(chat_id, id);
CREATE TABLE IF NOT EXISTS companion_pending_proposal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, position),
    FOREIGN KEY (chat_id) REFERENCES companion_chat(id)
);
CREATE INDEX IF NOT EXISTS idx_companion_pending_proposal_chat
ON companion_pending_proposal(chat_id, position);
"""


class ChatStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute(
                "PRAGMA table_info(companion_chat)").fetchall()}
            if "proposals_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE companion_chat ADD COLUMN "
                    "proposals_enabled INTEGER NOT NULL DEFAULT 1")

    @contextmanager
    def _connect(self):
        conn = db_connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(self, title: str = "New chat", *, proposals_enabled: bool = True) -> str:
        chat_id, timestamp = uuid.uuid4().hex, _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO companion_chat "
                "(id,title,proposals_enabled,created_at,updated_at) VALUES (?,?,?,?,?)",
                (chat_id, crypto.enc(title), int(bool(proposals_enabled)),
                 timestamp, timestamp),
            )
        return chat_id

    def ensure(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM companion_chat ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        # The very first chat this store ever creates is the standing "Intro"
        # thread — every later chat still gets auto-titled from its first
        # message as usual (see title_from_first_message).
        return row["id"] if row else self.create(title="Intro")

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,title,proposals_enabled,created_at,updated_at FROM companion_chat "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [{"id": row["id"], "title": crypto.dec(row["title"]),
                 "proposals_enabled": bool(row["proposals_enabled"]),
                 "created_at": row["created_at"], "updated_at": row["updated_at"]}
                for row in rows]

    def proposals_enabled(self, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT proposals_enabled FROM companion_chat WHERE id=?", (str(chat_id),)
            ).fetchone()
        return bool(row and row["proposals_enabled"])

    def set_proposals_enabled(self, chat_id: str, enabled: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM companion_chat WHERE id=?", (str(chat_id),)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE companion_chat SET proposals_enabled=?,updated_at=? WHERE id=?",
                (int(bool(enabled)), _now(), str(chat_id)),
            )
            if not enabled:
                conn.execute(
                    "DELETE FROM companion_pending_proposal WHERE chat_id=?", (str(chat_id),))
        return True

    def pending_proposals(self, chat_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM companion_pending_proposal WHERE chat_id=? "
                "ORDER BY position,id", (str(chat_id),)
            ).fetchall()
        proposals = []
        for row in rows:
            try:
                value = json.loads(crypto.dec(row["payload"]) or "")
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                proposals.append(value)
        return proposals

    def replace_pending_proposals(self, chat_id: str, proposals: list[dict]) -> None:
        timestamp = _now()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM companion_pending_proposal WHERE chat_id=?", (str(chat_id),))
            for position, proposal in enumerate(list(proposals or [])[:3]):
                if not isinstance(proposal, dict):
                    continue
                payload = crypto.enc(json.dumps(proposal, ensure_ascii=False))
                conn.execute(
                    "INSERT INTO companion_pending_proposal "
                    "(chat_id,position,payload,created_at) VALUES (?,?,?,?)",
                    (str(chat_id), int(position), payload, timestamp),
                )

    def pop_pending_proposal(self, chat_id: str, index: int) -> dict | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,payload FROM companion_pending_proposal WHERE chat_id=? "
                "ORDER BY position,id", (str(chat_id),)
            ).fetchall()
            try:
                row = rows[int(index)]
            except (IndexError, TypeError, ValueError):
                return None
            conn.execute(
                "DELETE FROM companion_pending_proposal WHERE id=?", (int(row["id"]),))
            remaining = conn.execute(
                "SELECT id FROM companion_pending_proposal WHERE chat_id=? "
                "ORDER BY position,id", (str(chat_id),)
            ).fetchall()
            for position, item in enumerate(remaining):
                conn.execute(
                    "UPDATE companion_pending_proposal SET position=? WHERE id=?",
                    (int(position), int(item["id"])),
                )
        try:
            value = json.loads(crypto.dec(row["payload"]) or "")
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def messages(self, chat_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role,content FROM companion_message WHERE chat_id=? ORDER BY id",
                (chat_id,),
            ).fetchall()
        return [{"role": row["role"], "content": crypto.dec(row["content"])}
                for row in rows]

    def recent_user_messages(self, limit: int = 80) -> list[dict]:
        """Recent user-authored context across chats, newest first.

        Consumers must still relevance-filter and bound this data. Assistant
        replies are intentionally excluded so model prose never becomes user
        evidence merely because it appeared in chat.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,chat_id,content,created_at FROM companion_message "
                "WHERE role='user' ORDER BY id DESC LIMIT ?", (max(0, int(limit)),)
            ).fetchall()
        return [{"id": int(row["id"]), "chat_id": row["chat_id"],
                 "content": crypto.dec(row["content"]) or "",
                 "created_at": row["created_at"]} for row in rows]

    def append(self, chat_id: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError(f"invalid chat role: {role}")
        timestamp = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO companion_message (chat_id,role,content,created_at) "
                "VALUES (?,?,?,?)", (chat_id, role, crypto.enc(content), timestamp),
            )
            conn.execute(
                "UPDATE companion_chat SET updated_at=? WHERE id=?", (timestamp, chat_id),
            )

    def title_from_first_message(self, chat_id: str, text: str) -> None:
        title = " ".join((text or "").strip().split())[:42] or "New chat"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) count FROM companion_message WHERE chat_id=? AND role='user'",
                (chat_id,),
            ).fetchone()
            if row["count"] != 1:
                return
            # The very first chat ever created (see ensure()) keeps its
            # standing "Intro" title rather than being renamed from content.
            oldest = conn.execute(
                "SELECT id FROM companion_chat ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if oldest and oldest["id"] == chat_id:
                return
            conn.execute(
                "UPDATE companion_chat SET title=? WHERE id=?",
                (crypto.enc(title), chat_id),
            )

    def rename(self, chat_id: str, title: str) -> bool:
        title = " ".join((title or "").strip().split())[:80] or "New chat"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM companion_chat WHERE id=?", (str(chat_id),),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE companion_chat SET title=? WHERE id=?",
                (crypto.enc(title), str(chat_id)),
            )
        return True

    def exists(self, chat_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM companion_chat WHERE id=?", (chat_id,),
            ).fetchone() is not None

    def delete(self, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM companion_chat WHERE id=?", (str(chat_id),),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "DELETE FROM companion_pending_proposal WHERE chat_id=?", (str(chat_id),))
            conn.execute("DELETE FROM companion_message WHERE chat_id=?", (str(chat_id),))
            conn.execute("DELETE FROM companion_chat WHERE id=?", (str(chat_id),))
        return True
