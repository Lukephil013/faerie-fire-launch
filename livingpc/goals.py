"""Actualized Self goal tree and resumable suggestion-planning workflow.

Private titles, notes, planner messages, drafts, and evidence labels are encrypted
at rest. Structural fields remain queryable so progress can be computed without
decrypting payloads. Nothing in this module reads passive capture or completes a
task automatically.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import crypto
from .db import connect as db_connect
from .diagnostics import log_diag
from .lang import T


NODE_TYPES = {"umbrella", "overgoal", "subgoal", "task"}
NODE_STATUSES = {"active", "paused", "completed", "archived"}
PRIORITIES = {"low", "normal", "high"}
SESSION_STATUSES = {"active", "ready", "implemented", "abandoned"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_node (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    node_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'normal',
    due_date TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (node_type IN ('umbrella','overgoal','subgoal','task')),
    CHECK (status IN ('active','paused','completed','archived')),
    CHECK (priority IN ('low','normal','high')),
    FOREIGN KEY (parent_id) REFERENCES goal_node(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_parent ON goal_node(parent_id, position, id);

CREATE TABLE IF NOT EXISTS goal_curiosity_link (
    goal_id INTEGER NOT NULL,
    curiosity_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, curiosity_id),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id),
    FOREIGN KEY (curiosity_id) REFERENCES curiosity(id)
);

CREATE TABLE IF NOT EXISTS goal_evidence_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT,
    label TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (goal_id, source_kind, source_id),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id)
);

CREATE TABLE IF NOT EXISTS goal_origin (
    goal_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_id TEXT,
    source_proposal_id INTEGER,
    source_label TEXT,
    summary TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES goal_node(id)
);

CREATE TABLE IF NOT EXISTS goal_plan_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_id INTEGER,
    target_parent_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    draft_json TEXT,
    summary TEXT,
    committed_goal_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (status IN ('active','ready','implemented','abandoned')),
    FOREIGN KEY (source_item_id) REFERENCES curiosity_item(id),
    FOREIGN KEY (target_parent_id) REFERENCES goal_node(id),
    FOREIGN KEY (committed_goal_id) REFERENCES goal_node(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_plan_source ON goal_plan_session(source_item_id, status);

CREATE TABLE IF NOT EXISTS goal_plan_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (role IN ('user','assistant')),
    FOREIGN KEY (session_id) REFERENCES goal_plan_session(id)
);

CREATE TABLE IF NOT EXISTS mastery_subject_profile (
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    dimensions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    PRIMARY KEY (subject_type, subject_id),
    CHECK (subject_type IN ('curiosity','goal')),
    CHECK (status IN ('draft','approved'))
);

CREATE TABLE IF NOT EXISTS mastery_subject_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    dimension_slug TEXT NOT NULL,
    observed_score REAL,
    confidence REAL NOT NULL DEFAULT 0,
    source_kind TEXT NOT NULL,
    source_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (subject_type, subject_id, source_kind, source_id)
);
"""


def _clean_date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


class GoalStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate_curiosity_items()
        self._migrate_curiosity_mastery()
        self.root_id = self._ensure_root()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _mark_goal_ai_dirty(self, *node_ids: int | None) -> None:
        """Mark changed nodes and ancestors when the optional GoalAI schema exists."""
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_agent_state'"
        ).fetchone()
        if not exists:
            return
        columns = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(goal_agent_state)").fetchall()}
        now = _now()
        for node_id in node_ids:
            current = int(node_id) if node_id else 0
            while current:
                self.conn.execute(
                    "INSERT OR IGNORE INTO goal_agent_state (node_id,updated_at) VALUES (?,?)",
                    (current, now))
                if {"dirty_reason", "deferred"}.issubset(columns):
                    self.conn.execute(
                        "UPDATE goal_agent_state SET dirty=1,dirty_reason='goal changed',"
                        "deferred=0,updated_at=? WHERE node_id=?", (now, current))
                else:
                    self.conn.execute(
                        "UPDATE goal_agent_state SET dirty=1,updated_at=? WHERE node_id=?",
                        (now, current))
                row = self.conn.execute(
                    "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
                current = int(row["parent_id"]) if row and row["parent_id"] else 0

    def _migrate_curiosity_items(self) -> None:
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curiosity_item'"
        ).fetchone()
        if not table:
            return
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(curiosity_item)")}
        for name, declaration in {
            "implementation_session_id": "INTEGER",
            "implementation_goal_id": "INTEGER",
        }.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE curiosity_item ADD COLUMN {name} {declaration}")

    def _migrate_curiosity_mastery(self) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='curiosity_metric_profile'"
        ).fetchone()
        if not exists:
            return
        rows = self.conn.execute(
            "SELECT curiosity_id,status,dimensions_json,created_at,approved_at "
            "FROM curiosity_metric_profile"
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO mastery_subject_profile "
                "(subject_type,subject_id,status,dimensions_json,created_at,approved_at) "
                "VALUES ('curiosity',?,?,?,?,?)",
                (row["curiosity_id"], row["status"], row["dimensions_json"],
                 row["created_at"], row["approved_at"]),
            )
        event_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='curiosity_metric_event'"
        ).fetchone()
        if event_table:
            for row in self.conn.execute(
                "SELECT curiosity_id,dimension_slug,observed_score,confidence,"
                "event_type,source_key,created_at FROM curiosity_metric_event "
                "WHERE dimension_slug IS NOT NULL"
            ).fetchall():
                self.conn.execute(
                    "INSERT OR IGNORE INTO mastery_subject_event "
                    "(subject_type,subject_id,dimension_slug,observed_score,confidence,"
                    "source_kind,source_id,created_at) VALUES ('curiosity',?,?,?,?,?,?,?)",
                    (row["curiosity_id"], row["dimension_slug"], row["observed_score"],
                     row["confidence"], row["event_type"], row["source_key"],
                     row["created_at"]),
                )

    def _ensure_root(self) -> int:
        rows = self.conn.execute(
            "SELECT id FROM goal_node WHERE node_type='umbrella' ORDER BY id"
        ).fetchall()
        if rows:
            return int(rows[0]["id"])
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO goal_node "
            "(parent_id,node_type,title,status,priority,position,created_at,updated_at) "
            "VALUES (NULL,'umbrella',?,'active','normal',0,?,?)",
            (crypto.enc(T("Actualized Self", "실현된 나")), now, now),
        )
        return int(cur.lastrowid)

    def _row(self, row) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]), "parent_id": row["parent_id"],
            "type": row["node_type"], "title": crypto.dec(row["title"]),
            "description": crypto.dec(row["description"]) or "",
            "notes": crypto.dec(row["notes"]) or "", "status": row["status"],
            "priority": row["priority"], "due_date": row["due_date"],
            "position": row["position"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "completed_at": row["completed_at"],
        }

    def get(self, goal_id: int) -> dict | None:
        node = self._row(self.conn.execute(
            "SELECT * FROM goal_node WHERE id=?", (int(goal_id),)).fetchone())
        if node:
            node["origin"] = self.origin(node["id"])
        return node

    def _origin_dict(self, row) -> dict | None:
        if not row:
            return None
        return {
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "source_proposal_id": row["source_proposal_id"],
            "source_label": crypto.dec(row["source_label"]) or "",
            "summary": crypto.dec(row["summary"]) or "",
            "detail": crypto.dec(row["detail"]) or "",
            "created_at": row["created_at"],
        }

    def origin(self, goal_id: int) -> dict | None:
        return self._origin_dict(self.conn.execute(
            "SELECT * FROM goal_origin WHERE goal_id=?", (int(goal_id),)).fetchone())

    def set_origin(self, goal_id: int, *, source_kind: str, source_id: str | int | None = None,
                   source_proposal_id: int | None = None, source_label: str = "",
                   summary: str = "", detail: str = "") -> None:
        if not self.conn.execute("SELECT 1 FROM goal_node WHERE id=?", (int(goal_id),)).fetchone():
            raise ValueError("goal not found")
        now = _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO goal_origin "
            "(goal_id,source_kind,source_id,source_proposal_id,source_label,summary,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(goal_id), str(source_kind), None if source_id is None else str(source_id),
             None if source_proposal_id is None else int(source_proposal_id),
             crypto.enc(source_label), crypto.enc(summary), crypto.enc(detail), now))
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()

    def _goal_path_titles(self, goal_id: int) -> list[str]:
        titles: list[str] = []
        seen: set[int] = set()
        current = int(goal_id)
        while current and current not in seen:
            seen.add(current)
            row = self.conn.execute(
                "SELECT parent_id,title FROM goal_node WHERE id=?", (current,)).fetchone()
            if not row:
                break
            titles.append(crypto.dec(row["title"]) or "")
            current = int(row["parent_id"]) if row["parent_id"] else 0
        return list(reversed([title for title in titles if title]))

    def catalog(self, max_nodes: int = 200) -> list[dict]:
        """Compact, bounded id+type+title+path listing of active nodes — for
        handing an AI enough to reference a real node id without ever handing
        it 'the whole tree' (descriptions, evidence, etc. stay out of this)."""
        rows = self.conn.execute(
            "SELECT id,node_type,title,status FROM goal_node "
            "WHERE status!='archived' ORDER BY parent_id,position,id "
            "LIMIT ?", (int(max_nodes),)).fetchall()
        type_label = {"umbrella": "Soul", "overgoal": "Root",
                      "subgoal": "Branch", "task": "Leaf"}
        return [{
            "id": int(r["id"]),
            "type": type_label.get(r["node_type"], r["node_type"]),
            "title": crypto.dec(r["title"]) or "",
            "path": " › ".join(self._goal_path_titles(int(r["id"]))),
            "status": r["status"],
        } for r in rows]

    def _linked_curiosity_origin_bits(self, goal_id: int) -> tuple[list[str], list[str]]:
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('curiosity','curiosity_item','goal_curiosity_link')")}
        if not {"curiosity", "goal_curiosity_link"}.issubset(tables):
            return [], []
        rows = self.conn.execute(
            "SELECT c.id,c.label,c.directive FROM goal_curiosity_link l "
            "JOIN curiosity c ON c.id=l.curiosity_id WHERE l.goal_id=? ORDER BY c.id",
            (int(goal_id),)).fetchall()
        labels: list[str] = []
        details: list[str] = []
        for row in rows[:5]:
            label = crypto.dec(row["label"]) or ""
            directive = crypto.dec(row["directive"]) or ""
            if label:
                labels.append(label)
            detail = f"{label}: {directive}".strip(": ")
            if detail:
                details.append(detail)
            if "curiosity_item" in tables:
                answered = self.conn.execute(
                    "SELECT text,answer FROM curiosity_item WHERE curiosity_id=? "
                    "AND kind='question' AND status='answered' ORDER BY id DESC LIMIT 3",
                    (int(row["id"]),)).fetchall()
                for item in reversed(answered):
                    question = crypto.dec(item["text"]) or ""
                    answer = crypto.dec(item["answer"]) or ""
                    if question or answer:
                        details.append(f"Q: {question}\nA: {answer}".strip())
        return labels, details

    def backfill_missing_origins(self) -> int:
        """Create best-effort origin recaps for older nodes.

        This does not infer new truths. It only summarizes existing node text,
        path, and attached investigations so the Growth page has something
        durable to explain why legacy nodes exist.
        """
        rows = self.conn.execute(
            "SELECT g.* FROM goal_node g LEFT JOIN goal_origin o ON o.goal_id=g.id "
            "WHERE o.goal_id IS NULL ORDER BY g.parent_id,g.position,g.id").fetchall()
        now = _now()
        backfilled: list[int] = []
        for row in rows:
            node = self._row(row)
            if not node:
                continue
            labels, linked = self._linked_curiosity_origin_bits(int(node["id"]))
            path = " › ".join(self._goal_path_titles(int(node["id"])))
            title = str(node["title"] or "")
            description = str(node["description"] or "").strip()
            type_label = {
                "umbrella": "Soul", "overgoal": "Root",
                "subgoal": "Branch", "task": "Leaf",
            }.get(str(node["type"]), str(node["type"]))
            if node["type"] == "umbrella":
                summary = description or (
                    f"{title} is the overall purpose of the map. Add Soul Calibration "
                    "to turn this into a clearer personal objective.")
            elif node["type"] == "overgoal":
                summary = f"Existing Root “{title}”."
                if description:
                    summary += f"\nCurrent goal: {description}"
            elif node["type"] == "subgoal":
                summary = f"Existing Branch “{title}”."
                if description:
                    summary += f"\nProblem/context: {description}"
            else:
                summary = f"Existing Leaf “{title}”."
                if description:
                    summary += f"\nConcrete action/context: {description}"
            if labels:
                summary += "\nAttached investigations: " + ", ".join(labels[:4])
            detail_parts = [
                f"Backfilled from existing {type_label} node.",
                f"Path: {path}",
            ]
            if description:
                detail_parts.append(f"Stored description:\n{description}")
            if linked:
                detail_parts.append("Attached investigation context:\n" + "\n\n".join(linked[:8]))
            self.conn.execute(
                "INSERT OR IGNORE INTO goal_origin "
                "(goal_id,source_kind,source_id,source_proposal_id,source_label,summary,detail,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(node["id"]), "backfill", str(node["id"]), None,
                 crypto.enc(", ".join(labels[:3]) or "Existing tree"),
                 crypto.enc(summary), crypto.enc("\n\n".join(detail_parts)), now))
            backfilled.append(int(node["id"]))
        if backfilled:
            self._mark_goal_ai_dirty(*backfilled)
        self.conn.commit()
        return len(backfilled)

    def _validate_parent(self, node_type: str, parent_id: int | None,
                         moving_id: int | None = None) -> int | None:
        if node_type == "umbrella":
            if parent_id is not None:
                raise ValueError("the umbrella cannot have a parent")
            return None
        if parent_id is None:
            raise ValueError("non-umbrella goals require a parent")
        parent = self.get(parent_id)
        if not parent:
            raise ValueError("parent goal not found")
        allowed = {
            "overgoal": {"umbrella"},
            "subgoal": {"overgoal", "subgoal"},
            "task": {"overgoal", "subgoal"},
        }[node_type]
        if parent["type"] not in allowed:
            raise ValueError(f"{node_type} cannot be placed under {parent['type']}")
        if moving_id is not None:
            cursor = parent
            while cursor:
                if cursor["id"] == moving_id:
                    raise ValueError("a goal cannot be moved beneath itself")
                cursor = self.get(cursor["parent_id"]) if cursor["parent_id"] else None
        return int(parent_id)

    def create(self, node_type: str, title: str, *, parent_id: int | None = None,
               description: str = "", notes: str = "", priority: str = "normal",
               due_date: str | None = None, status: str = "active",
               _commit: bool = True) -> int:
        node_type = str(node_type).lower()
        if node_type not in NODE_TYPES or node_type == "umbrella":
            raise ValueError("new nodes must be overgoals, subgoals, or tasks")
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        if priority not in PRIORITIES or status not in NODE_STATUSES:
            raise ValueError("invalid priority or status")
        parent_id = self._validate_parent(node_type, parent_id or self.root_id)
        position = int(self.conn.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM goal_node WHERE parent_id=?",
            (parent_id,),).fetchone()[0])
        now = _now()
        completed = now if status == "completed" else None
        cur = self.conn.execute(
            "INSERT INTO goal_node (parent_id,node_type,title,description,notes,status,"
            "priority,due_date,position,created_at,updated_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (parent_id, node_type, crypto.enc(title), crypto.enc(description),
             crypto.enc(notes), status, priority, _clean_date(due_date), position,
            now, now, completed),
        )
        self._mark_goal_ai_dirty(int(cur.lastrowid), parent_id)
        if _commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def update(self, goal_id: int, **changes) -> dict:
        node = self.get(goal_id)
        if not node:
            raise ValueError("goal not found")
        allowed = {"title", "description", "notes", "priority", "due_date", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported goal fields: {', '.join(sorted(unknown))}")
        if node["type"] == "umbrella" and changes.get("status") in {"completed", "archived"}:
            raise ValueError("the umbrella cannot be completed or archived")
        values, sql = [], []
        for key, value in changes.items():
            if key == "title":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("title is required")
                value = crypto.enc(value)
            elif key in {"description", "notes"}:
                value = crypto.enc(str(value or ""))
            elif key == "priority" and value not in PRIORITIES:
                raise ValueError("invalid priority")
            elif key == "status":
                if value not in NODE_STATUSES:
                    raise ValueError("invalid status")
                sql.append("completed_at=?")
                values.append(_now() if value == "completed" else None)
            elif key == "due_date":
                value = _clean_date(value)
            sql.append(f"{key}=?")
            values.append(value)
        sql.append("updated_at=?")
        values.append(_now())
        values.append(int(goal_id))
        self.conn.execute(f"UPDATE goal_node SET {','.join(sql)} WHERE id=?", values)
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()
        return self.get(goal_id)  # type: ignore[return-value]

    def move(self, goal_id: int, parent_id: int, position: int | None = None) -> None:
        node = self.get(goal_id)
        if not node or node["type"] == "umbrella":
            raise ValueError("goal cannot be moved")
        parent_id = self._validate_parent(node["type"], int(parent_id), int(goal_id))  # type: ignore[assignment]
        old_parent = node["parent_id"]
        siblings = [int(r["id"]) for r in self.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND id!=? ORDER BY position,id",
            (parent_id, int(goal_id))).fetchall()]
        insert_at = len(siblings) if position is None else min(len(siblings), max(0, int(position)))
        siblings.insert(insert_at, int(goal_id))
        self.conn.execute(
            "UPDATE goal_node SET parent_id=?,updated_at=? WHERE id=?",
            (parent_id, _now(), int(goal_id)))
        for index, sibling_id in enumerate(siblings):
            self.conn.execute("UPDATE goal_node SET position=? WHERE id=?", (index, sibling_id))
        if old_parent != parent_id:
            old_siblings = self.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=? ORDER BY position,id",
                (old_parent,)).fetchall()
            for index, sibling in enumerate(old_siblings):
                self.conn.execute("UPDATE goal_node SET position=? WHERE id=?",
                                  (index, sibling["id"]))
        self._mark_goal_ai_dirty(int(goal_id), old_parent, parent_id)
        self.conn.commit()

    def delete_subtree(self, goal_id: int) -> int:
        """'Delete' a node and everything under it. Always a soft archive
        (status='archived'), never a hard row delete — reversible, and
        consistent with how catalog()/tree() already hide archived nodes.
        Returns the count of nodes archived."""
        node = self.get(goal_id)
        if not node:
            raise ValueError("goal not found")
        if node["type"] == "umbrella":
            raise ValueError("the umbrella cannot be archived")
        to_visit = [int(goal_id)]
        ids: list[int] = []
        while to_visit:
            current = to_visit.pop()
            ids.append(current)
            rows = self.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=?", (current,)).fetchall()
            to_visit.extend(int(r["id"]) for r in rows)
        now = _now()
        self.conn.executemany(
            "UPDATE goal_node SET status='archived',updated_at=? WHERE id=?",
            [(now, node_id) for node_id in ids])
        self._mark_goal_ai_dirty(*ids)
        self.conn.commit()
        return len(ids)

    def link_curiosity(self, goal_id: int, curiosity_id: int) -> None:
        if not self.get(goal_id):
            raise ValueError("goal not found")
        if not self.conn.execute("SELECT 1 FROM curiosity WHERE id=?", (curiosity_id,)).fetchone():
            raise ValueError("curiosity not found")
        self.conn.execute(
            "INSERT OR IGNORE INTO goal_curiosity_link VALUES (?,?,?)",
            (int(goal_id), int(curiosity_id), _now()))
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()

    def unlink_curiosity(self, goal_id: int, curiosity_id: int) -> None:
        self.conn.execute("DELETE FROM goal_curiosity_link WHERE goal_id=? AND curiosity_id=?",
                          (int(goal_id), int(curiosity_id)))
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()

    def _auto_attach_matching_root_curiosities(self) -> int:
        """Link one unambiguous exact-name curiosity to its matching Root."""
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curiosity'"
        ).fetchone():
            return 0
        curiosities = self.conn.execute(
            "SELECT id,label FROM curiosity WHERE status='active' ORDER BY id"
        ).fetchall()
        by_name: dict[str, list[int]] = {}
        for row in curiosities:
            by_name.setdefault(_slug(crypto.dec(row["label"]) or ""), []).append(int(row["id"]))
        created = 0
        for row in self.conn.execute(
            "SELECT id,title FROM goal_node WHERE node_type='overgoal' AND status!='archived'"
        ).fetchall():
            matches = by_name.get(_slug(crypto.dec(row["title"]) or ""), [])
            if len(matches) != 1:
                continue
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO goal_curiosity_link VALUES (?,?,?)",
                (int(row["id"]), matches[0], _now()))
            if cur.rowcount:
                created += 1
                self._mark_goal_ai_dirty(int(row["id"]))
        # Always end the implicit transaction: an ignored INSERT still opens a
        # write transaction, and leaving it open holds the WAL write lock for
        # this connection's remaining lifetime (e.g. across GoalAI model calls
        # 30+ seconds long — the root cause of 'database is locked' storms).
        self.conn.commit()
        return created

    def add_evidence(self, goal_id: int, source_kind: str, source_id: str | None,
                     label: str = "") -> int:
        if not self.get(goal_id):
            raise ValueError("goal not found")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO goal_evidence_link "
            "(goal_id,source_kind,source_id,label,created_at) VALUES (?,?,?,?,?)",
            (int(goal_id), str(source_kind), None if source_id is None else str(source_id),
             crypto.enc(label), _now()))
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def enable_mastery(self, goal_id: int, dimensions: list[dict | str]) -> dict:
        node = self.get(goal_id)
        if not node or node["type"] not in {"overgoal", "subgoal"}:
            raise ValueError("mastery can only be enabled for an overgoal or subgoal")
        cleaned = []
        for item in dimensions:
            label = str(item if isinstance(item, str) else item.get("label", "")).strip()
            if not label:
                continue
            slug = _slug(label)
            if slug and slug not in {d["slug"] for d in cleaned}:
                cleaned.append({"slug": slug, "label": label})
        if not cleaned:
            raise ValueError("at least one mastery dimension is required")
        now = _now()
        self.conn.execute(
            "INSERT INTO mastery_subject_profile "
            "(subject_type,subject_id,status,dimensions_json,created_at,approved_at) "
            "VALUES ('goal',?,'approved',?,?,?) "
            "ON CONFLICT(subject_type,subject_id) DO UPDATE SET "
            "status='approved',dimensions_json=excluded.dimensions_json,approved_at=excluded.approved_at",
            (int(goal_id), json.dumps(cleaned), now, now))
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()
        return self.mastery(goal_id) or {}

    def record_mastery(self, goal_id: int, dimension_slug: str, score: float,
                       confidence: float, source_kind: str, source_id: str | None = None) -> None:
        profile = self.mastery(goal_id)
        if not profile or dimension_slug not in {d["slug"] for d in profile["dimensions"]}:
            raise ValueError("approved mastery dimension not found")
        self.conn.execute(
            "INSERT OR IGNORE INTO mastery_subject_event "
            "(subject_type,subject_id,dimension_slug,observed_score,confidence,source_kind,source_id,created_at) "
            "VALUES ('goal',?,?,?,?,?,?,?)",
            (int(goal_id), dimension_slug, max(0.0, min(100.0, float(score))),
             max(0.0, min(1.0, float(confidence))), source_kind, source_id, _now()))
        self._mark_goal_ai_dirty(int(goal_id))
        self.conn.commit()

    def mastery(self, goal_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM mastery_subject_profile WHERE subject_type='goal' AND subject_id=?",
            (int(goal_id),)).fetchone()
        if not row:
            return None
        dimensions = json.loads(row["dimensions_json"])
        scores = {}
        for dimension in dimensions:
            events = self.conn.execute(
                "SELECT observed_score,confidence FROM mastery_subject_event "
                "WHERE subject_type='goal' AND subject_id=? AND dimension_slug=? "
                "AND observed_score IS NOT NULL ORDER BY id",
                (int(goal_id), dimension["slug"]),).fetchall()
            value = None
            certainty = 0.0
            for event in events:
                weight = float(event["confidence"])
                value = float(event["observed_score"]) if value is None else value + .25 * weight * (float(event["observed_score"]) - value)
                certainty = 1.0 - (1.0 - certainty) * (1.0 - min(.9, weight / 3.0))
            scores[dimension["slug"]] = {
                "mastery": None if value is None else round(value, 2),
                "confidence": round(certainty, 4), "evidence_count": len(events),
            }
        return {"status": row["status"], "dimensions": dimensions, "scores": scores}

    def tree(self) -> dict:
        self._auto_attach_matching_root_curiosities()
        rows = [self._row(r) for r in self.conn.execute(
            "SELECT * FROM goal_node ORDER BY parent_id,position,id").fetchall()]
        nodes = {r["id"]: r for r in rows if r is not None}
        links = self.conn.execute(
            "SELECT l.goal_id,c.id,c.label,c.status FROM goal_curiosity_link l "
            "JOIN curiosity c ON c.id=l.curiosity_id ORDER BY c.id").fetchall()
        for node in nodes.values():
            node["children"] = []
            node["curiosities"] = []
            node["evidence"] = []
            node["origin"] = None
            node["mastery"] = self.mastery(node["id"])
        for row in self.conn.execute("SELECT * FROM goal_origin ORDER BY goal_id"):
            if row["goal_id"] in nodes:
                nodes[row["goal_id"]]["origin"] = self._origin_dict(row)
        for link in links:
            if link["goal_id"] in nodes:
                nodes[link["goal_id"]]["curiosities"].append({
                    "id": link["id"], "label": crypto.dec(link["label"]),
                    "status": link["status"], "inherited_from_id": None,
                    "inherited_from_title": "",
                })
        for row in self.conn.execute("SELECT * FROM goal_evidence_link ORDER BY id"):
            if row["goal_id"] in nodes:
                nodes[row["goal_id"]]["evidence"].append({
                    "id": row["id"], "source_kind": row["source_kind"],
                    "source_id": row["source_id"], "label": crypto.dec(row["label"]) or "",
                })
        for node in nodes.values():
            if node["parent_id"] in nodes:
                nodes[node["parent_id"]]["children"].append(node)

        def inherit_curiosities(node: dict, inherited: list[dict]) -> None:
            direct_ids = {item["id"] for item in node["curiosities"]}
            visible = list(node["curiosities"])
            for item in inherited:
                if item["id"] not in direct_ids:
                    visible.append({**item,
                                    "inherited_from_id": item["source_node_id"],
                                    "inherited_from_title": item["source_node_title"]})
            node["curiosities"] = visible
            flowing = []
            for item in visible:
                source_id = item.get("inherited_from_id") or node["id"]
                source_title = item.get("inherited_from_title") or node["title"]
                flowing.append({"id": item["id"], "label": item["label"],
                                "status": item["status"], "source_node_id": source_id,
                                "source_node_title": source_title})
            for child in node["children"]:
                inherit_curiosities(child, flowing)

        def progress(node: dict) -> tuple[int, int]:
            if node["type"] == "task":
                if node["status"] in {"paused", "archived"}:
                    return 0, 0
                return (1 if node["status"] == "completed" else 0), 1
            done = total = 0
            for child in node["children"]:
                child_done, child_total = progress(child)
                done += child_done
                total += child_total
            node["completion"] = {
                "done": done, "total": total,
                "percent": None if not total else round(done * 100.0 / total, 1),
            }
            return done, total

        root = nodes.get(self.root_id)
        if root:
            inherit_curiosities(root, [])
            progress(root)
        return root or {}

    # --- planner persistence ---------------------------------------------
    def start_plan(self, source_item_id: int | None, target_parent_id: int | None,
                   first_message: str, draft: dict | None = None) -> dict:
        target = self.get(target_parent_id or self.root_id)
        if not target or target["type"] not in {"umbrella", "overgoal", "subgoal"}:
            raise ValueError("planning target must accept child goals")
        if source_item_id is not None:
            existing = self.conn.execute(
                "SELECT id FROM goal_plan_session WHERE source_item_id=? "
                "AND status IN ('active','ready') ORDER BY id DESC LIMIT 1",
                (int(source_item_id),)).fetchone()
            if existing:
                return self.plan_session(existing["id"])
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO goal_plan_session "
            "(source_item_id,target_parent_id,status,draft_json,created_at,updated_at) "
            "VALUES (?,?,'active',?,?,?)",
            (source_item_id, target["id"], crypto.enc(json.dumps(draft or {})), now, now))
        session_id = int(cur.lastrowid)
        self.add_plan_message(session_id, "assistant", first_message, commit=False)
        if source_item_id is not None:
            self.conn.execute(
                "UPDATE curiosity_item SET implementation_session_id=? WHERE id=?",
                (session_id, int(source_item_id)))
        self.conn.commit()
        return self.plan_session(session_id)

    def add_plan_message(self, session_id: int, role: str, content: str,
                         *, commit: bool = True) -> None:
        if role not in {"user", "assistant"} or not str(content).strip():
            raise ValueError("valid planner role and content are required")
        self.conn.execute(
            "INSERT INTO goal_plan_message (session_id,role,content,created_at) VALUES (?,?,?,?)",
            (int(session_id), role, crypto.enc(str(content).strip()), _now()))
        self.conn.execute("UPDATE goal_plan_session SET updated_at=? WHERE id=?",
                          (_now(), int(session_id)))
        if commit:
            self.conn.commit()

    def set_plan_draft(self, session_id: int, draft: dict, summary: str | None = None,
                       ready: bool = False) -> None:
        self.conn.execute(
            "UPDATE goal_plan_session SET draft_json=?,summary=COALESCE(?,summary),"
            "status=?,updated_at=? WHERE id=? AND status IN ('active','ready')",
            (crypto.enc(json.dumps(draft)), crypto.enc(summary) if summary is not None else None,
             "ready" if ready else "active", _now(), int(session_id)))
        self.conn.commit()

    def plan_session(self, session_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM goal_plan_session WHERE id=?", (int(session_id),)).fetchone()
        if not row:
            raise ValueError("planning session not found")
        messages = self.conn.execute(
            "SELECT role,content,created_at FROM goal_plan_message WHERE session_id=? ORDER BY id",
            (int(session_id),)).fetchall()
        raw = crypto.dec(row["draft_json"]) or "{}"
        try:
            draft = json.loads(raw)
        except json.JSONDecodeError:
            draft = {}
        return {
            "id": row["id"], "source_item_id": row["source_item_id"],
            "target_parent_id": row["target_parent_id"], "status": row["status"],
            "draft": draft, "summary": crypto.dec(row["summary"]) or "",
            "committed_goal_id": row["committed_goal_id"],
            "messages": [{"role": m["role"], "content": crypto.dec(m["content"]),
                          "created_at": m["created_at"]} for m in messages],
        }

    def commit_plan(self, session_id: int) -> dict:
        session = self.plan_session(session_id)
        if session["status"] == "implemented":
            return {"goal_id": session["committed_goal_id"], "already_implemented": True}
        if session["status"] != "ready":
            raise ValueError("summarize and review the plan before creating it")
        nodes = session["draft"].get("nodes") or []
        if not nodes:
            raise ValueError("draft has no goals to create")

        def add(raw: dict, parent_id: int) -> int:
            node_type = str(raw.get("type") or "subgoal")
            new_id = self.create(
                node_type, str(raw.get("title") or "").strip(), parent_id=parent_id,
                description=str(raw.get("description") or ""),
                notes=str(raw.get("notes") or ""),
                priority=str(raw.get("priority") or "normal"),
                due_date=raw.get("due_date"), _commit=False)
            for child in raw.get("children") or []:
                add(child, new_id)
            return new_id

        try:
            self.conn.execute("BEGIN")
            created = add(nodes[0], session["target_parent_id"])
            now = _now()
            self.conn.execute(
                "UPDATE goal_plan_session SET status='implemented',committed_goal_id=?,updated_at=? "
                "WHERE id=?", (created, now, int(session_id)))
            if session["source_item_id"] is not None:
                self.conn.execute(
                    "UPDATE curiosity_item SET status='tried',resolved_at=?,implementation_goal_id=? "
                    "WHERE id=?", (now, created, int(session["source_item_id"])))
                self.conn.execute(
                    "INSERT OR IGNORE INTO goal_evidence_link "
                    "(goal_id,source_kind,source_id,label,created_at) VALUES (?,?,?,?,?)",
                    (created, "curiosity_suggestion", str(session["source_item_id"]),
                     crypto.enc("Implemented suggestion"), now))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"goal_id": created, "already_implemented": False}

    def abandon_plan(self, session_id: int) -> None:
        session = self.plan_session(session_id)
        if session["status"] == "implemented":
            raise ValueError("implemented plans cannot be abandoned")
        self.conn.execute(
            "UPDATE goal_plan_session SET status='abandoned',updated_at=? WHERE id=?",
            (_now(), int(session_id)))
        if session["source_item_id"] is not None:
            self.conn.execute(
                "UPDATE curiosity_item SET implementation_session_id=NULL WHERE id=?",
                (int(session["source_item_id"]),))
        self.conn.commit()


PLANNER_SYSTEM = """You help turn one grounded suggestion into an actionable goal plan.
Ask exactly one decision-bearing question at a time. Briefly recommend a current
approach, then ask the question. Never activate goals yourself. Return strict JSON:
{"message": str, "draft": {"rationale": str, "nodes": [goal nodes]}}.
Goal nodes use type overgoal|subgoal|task, title, description, priority
low|normal|high, due_date YYYY-MM-DD or null, and children. Tasks have no children.
"""

SUMMARY_SYSTEM = """Turn this planning dialogue into one concise editable goal tree.
Use the user's decisions as authoritative. Return strict JSON:
{"summary": str, "draft": {"rationale": str, "nodes": [goal nodes]}}.
The first node must fit below the supplied target: overgoal below umbrella,
otherwise subgoal below an overgoal/subgoal. Include concrete tasks when known;
do not invent dates. Nodes use type, title, description, priority, due_date, children.
"""


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", (text or "").strip(), re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


class StubGoalPlanner:
    def first(self, suggestion: str, target: dict) -> tuple[str, dict]:
        message = (f'I can turn “{suggestion}” into a plan. My starting approach is to make '
                   "the smallest useful outcome explicit first. What would success look like?")
        return message, {"rationale": suggestion, "nodes": []}

    def reply(self, session: dict, answer: str, target: dict) -> tuple[str, dict]:
        draft = dict(session.get("draft") or {})
        draft["success"] = answer.strip()
        return ("That gives the plan a finish line. I would start with one small experiment "
                "and one review task. What is the first concrete action you want to take?", draft)

    def summarize(self, session: dict, target: dict) -> tuple[str, dict]:
        suggestion = session.get("draft", {}).get("rationale") or "Implement the idea"
        success = session.get("draft", {}).get("success") or "Define a useful outcome"
        first_type = "overgoal" if target["type"] == "umbrella" else "subgoal"
        draft = {"rationale": suggestion, "nodes": [{
            "type": first_type, "title": suggestion[:80], "description": success,
            "priority": "normal", "due_date": None, "children": [{
                "type": "task", "title": "Take the first concrete step",
                "description": "", "priority": "normal", "due_date": None,
                "children": [],
            }],
        }]}
        return f"Plan: {suggestion}. Success means {success}.", draft


class ClaudeGoalPlanner:
    def __init__(self, config):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from anthropic import Anthropic
        self.model = getattr(config, "curiosity_model", "claude-haiku-4-5")
        self.client = Anthropic(api_key=key,
                                timeout=getattr(config, "llm_timeout_seconds", 60.0))

    def _call(self, system: str, prompt: str) -> dict:
        log_diag("prompt", f"surface=goal-planner model={self.model} input_chars={len(prompt)}")
        msg = self.client.messages.create(
            model=self.model, max_tokens=1200, system=system,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = _json_object(text)
        if not data:
            raise ValueError("planner returned invalid JSON")
        return data

    def first(self, suggestion: str, target: dict) -> tuple[str, dict]:
        data = self._call(PLANNER_SYSTEM,
                          f"TARGET TYPE: {target['type']}\nSUGGESTION: {suggestion}")
        return str(data.get("message") or "What outcome do you want?"), data.get("draft") or {}

    def reply(self, session: dict, answer: str, target: dict) -> tuple[str, dict]:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"])
        data = self._call(PLANNER_SYSTEM,
                          f"TARGET TYPE: {target['type']}\nDRAFT: {json.dumps(session['draft'])}"
                          f"\nDIALOGUE:\n{transcript}\nuser: {answer}")
        return str(data.get("message") or "What should we decide next?"), data.get("draft") or session["draft"]

    def summarize(self, session: dict, target: dict) -> tuple[str, dict]:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"])
        data = self._call(SUMMARY_SYSTEM,
                          f"TARGET TYPE: {target['type']}\nDIALOGUE:\n{transcript}\n"
                          f"CURRENT DRAFT: {json.dumps(session['draft'])}")
        return str(data.get("summary") or "Review this plan."), data.get("draft") or session["draft"]


def get_goal_planner(config):
    backend = (getattr(config, "curiosity_backend", "") or
               getattr(config, "inference_backend", "claude")).lower()
    return StubGoalPlanner() if backend == "stub" else ClaudeGoalPlanner(config)


def start_planning(store: GoalStore, planner, source_item_id: int,
                   target_parent_id: int | None = None) -> dict:
    row = store.conn.execute("SELECT * FROM curiosity_item WHERE id=?",
                             (int(source_item_id),)).fetchone()
    if not row:
        raise ValueError("suggestion not found")
    if row["kind"] != "suggestion" or row["status"] != "open":
        raise ValueError("only an open suggestion can be implemented")
    suggestion = crypto.dec(row["text"])
    if target_parent_id is None:
        linked = store.conn.execute(
            "SELECT g.id FROM goal_curiosity_link l JOIN goal_node g ON g.id=l.goal_id "
            "WHERE l.curiosity_id=? AND g.node_type IN ('umbrella','overgoal','subgoal') "
            "AND g.status!='archived' ORDER BY g.id LIMIT 1",
            (row["curiosity_id"],)).fetchone()
        target_parent_id = int(linked["id"]) if linked else store.root_id
    target = store.get(target_parent_id or store.root_id)
    message, draft = planner.first(suggestion, target)
    return store.start_plan(int(source_item_id), target["id"], message, draft)


def continue_planning(store: GoalStore, planner, session_id: int, answer: str) -> dict:
    session = store.plan_session(session_id)
    if session["status"] not in {"active", "ready"}:
        raise ValueError("planning session is not active")
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("answer is required")
    target = store.get(session["target_parent_id"])
    message, draft = planner.reply(session, answer, target)
    store.add_plan_message(session_id, "user", answer)
    store.add_plan_message(session_id, "assistant", message)
    store.set_plan_draft(session_id, draft)
    return store.plan_session(session_id)


def summarize_plan(store: GoalStore, planner, session_id: int) -> dict:
    session = store.plan_session(session_id)
    if session["status"] not in {"active", "ready"}:
        raise ValueError("planning session is not active")
    target = store.get(session["target_parent_id"])
    summary, draft = planner.summarize(session, target)
    store.set_plan_draft(session_id, draft, summary=summary, ready=True)
    return store.plan_session(session_id)
