"""Encrypted persistent multi-chat history for the Companion."""
from __future__ import annotations

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
"""


class ChatStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = db_connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(self, title: str = "New chat") -> str:
        chat_id, timestamp = uuid.uuid4().hex, _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO companion_chat (id,title,created_at,updated_at) VALUES (?,?,?,?)",
                (chat_id, crypto.enc(title), timestamp, timestamp),
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
                "SELECT id,title,created_at,updated_at FROM companion_chat "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [{"id": row["id"], "title": crypto.dec(row["title"]),
                 "created_at": row["created_at"], "updated_at": row["updated_at"]}
                for row in rows]

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
            conn.execute("DELETE FROM companion_message WHERE chat_id=?", (str(chat_id),))
            conn.execute("DELETE FROM companion_chat WHERE id=?", (str(chat_id),))
        return True
