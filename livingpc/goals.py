"""Actualized Self goal tree and resumable suggestion-planning workflow.

Private titles, notes, planner messages, drafts, and evidence labels are encrypted
at rest. Structural fields remain queryable so progress can be computed without
decrypting payloads. Nothing in this module reads passive capture or completes a
task automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from . import crypto
from .db import connect as db_connect
from .diagnostics import log_diag
from .lang import T


NODE_TYPES = {"umbrella", "overgoal", "subgoal", "task"}
NODE_STATUSES = {"active", "paused", "completed", "archived"}
PRIORITIES = {"low", "normal", "high"}
PROJECT_SIGNAL_KINDS = {"highest_priority", "currently_working"}
# One-Leaf model: a plan commits a single concrete NOW Leaf per project; the
# next Leaf is created just-in-time from the main chat completion debrief.
PLAN_LEAF_HORIZON = 1
SESSION_STATUSES = {"active", "ready", "implemented", "abandoned"}

STARTER_ROOTS = (
    {
        "key": "work", "title_en": "Work & Contribution", "title_ko": "일과 기여",
        "description_en": "Career, livelihood, building, service, and meaningful contribution.",
        "description_ko": "커리어, 생계, 만들기, 봉사와 의미 있는 기여.",
        "keywords": {"work", "career", "business", "building", "client", "직업", "커리어", "일"},
    },
    {
        "key": "health", "title_en": "Health & Energy", "title_ko": "건강과 에너지",
        "description_en": "Physical health, mental wellbeing, energy, sleep, food, and movement.",
        "description_ko": "신체 건강, 마음의 안정, 에너지, 수면, 음식과 움직임.",
        "keywords": {"health", "energy", "wellbeing", "sleep", "food", "건강", "에너지", "수면"},
    },
    {
        "key": "relationships", "title_en": "Relationships & Belonging",
        "title_ko": "관계와 소속감",
        "description_en": "Close relationships, family, friendship, community, and belonging.",
        "description_ko": "가까운 관계, 가족, 우정, 공동체와 소속감.",
        "keywords": {"relationship", "relationships", "family", "friend", "community", "관계", "가족", "친구"},
    },
    {
        "key": "learning", "title_en": "Learning & Growth", "title_ko": "배움과 성장",
        "description_en": "Languages, study, skills, understanding, and personal development.",
        "description_ko": "언어, 공부, 기술, 이해와 개인적 성장.",
        "keywords": {"learning", "language", "education", "study", "korean", "배움", "언어", "교육", "한국어"},
    },
    {
        "key": "creativity", "title_en": "Creativity & Play", "title_ko": "창작과 놀이",
        "description_en": "Creative expression, hobbies, games, experimentation, and restorative play.",
        "description_ko": "창의적 표현, 취미, 게임, 실험과 회복을 위한 놀이.",
        "keywords": {"creativity", "creative", "play", "gaming", "games", "hobby", "창작", "놀이", "게임", "취미"},
    },
    {
        "key": "home", "title_en": "Home & Environment", "title_ko": "집과 환경",
        "description_en": "Home, surroundings, routines, possessions, and the environments you inhabit.",
        "description_ko": "집, 주변 환경, 일상, 소유물과 생활 공간.",
        "keywords": {"home", "environment", "house", "space", "routine", "집", "환경", "공간", "일상"},
    },
    {
        "key": "resources", "title_en": "Money & Resources", "title_ko": "돈과 자원",
        "description_en": "Income, spending, saving, financial resilience, and practical resources.",
        "description_ko": "수입, 지출, 저축, 재정적 회복력과 실용적 자원.",
        "keywords": {"money", "finance", "finances", "income", "saving", "돈", "재정", "수입", "저축"},
    },
)


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

CREATE TABLE IF NOT EXISTS experiment_outcome (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    curiosity_id INTEGER,
    source_item_id INTEGER,
    result TEXT NOT NULL,
    what_happened TEXT NOT NULL,
    expected_obstacle TEXT,
    surprise TEXT,
    helpfulness REAL,
    changed_understanding TEXT,
    next_adjustment TEXT,
    created_at TEXT NOT NULL,
    CHECK (result IN ('completed','attempted','avoided','abandoned')),
    CHECK (helpfulness IS NULL OR (helpfulness>=0 AND helpfulness<=10)),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id),
    FOREIGN KEY (curiosity_id) REFERENCES curiosity(id),
    FOREIGN KEY (source_item_id) REFERENCES curiosity_item(id)
);
CREATE INDEX IF NOT EXISTS idx_experiment_outcome_goal
ON experiment_outcome(goal_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_experiment_outcome_curiosity
ON experiment_outcome(curiosity_id,id DESC);

CREATE TABLE IF NOT EXISTS goal_restructure_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    proposal_id INTEGER,
    old_parent_id INTEGER NOT NULL,
    new_parent_id INTEGER NOT NULL,
    old_node_type TEXT NOT NULL,
    new_node_type TEXT NOT NULL,
    retained_counts_json TEXT NOT NULL,
    rationale TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES goal_node(id),
    FOREIGN KEY (old_parent_id) REFERENCES goal_node(id),
    FOREIGN KEY (new_parent_id) REFERENCES goal_node(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_restructure_history_goal
ON goal_restructure_history(goal_id,id DESC);

CREATE TABLE IF NOT EXISTS goal_semantic_role (
    goal_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    rationale TEXT,
    source TEXT NOT NULL DEFAULT 'user',
    updated_at TEXT NOT NULL,
    CHECK (role IN ('area','project','stage')),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id)
);

CREATE TABLE IF NOT EXISTS goal_semantic_role_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    old_role TEXT,
    new_role TEXT,
    rationale TEXT,
    source TEXT NOT NULL,
    proposal_id INTEGER,
    created_at TEXT NOT NULL,
    CHECK (old_role IS NULL OR old_role IN ('area','project','stage')),
    CHECK (new_role IS NULL OR new_role IN ('area','project','stage')),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_semantic_role_history_goal
ON goal_semantic_role_history(goal_id,id DESC);

CREATE TABLE IF NOT EXISTS goal_project_signal (
    kind TEXT PRIMARY KEY,
    goal_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (kind IN ('highest_priority','currently_working')),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goal_archive_snapshot (
    archive_root_id INTEGER NOT NULL,
    goal_id INTEGER NOT NULL,
    prior_status TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (archive_root_id, goal_id),
    CHECK (prior_status IN ('active','paused','completed','archived')),
    FOREIGN KEY (archive_root_id) REFERENCES goal_node(id),
    FOREIGN KEY (goal_id) REFERENCES goal_node(id)
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


def normalize_leaf_title(value: str) -> str:
    """Return the stable title key used by AI Leaf-horizon checks."""
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold()))


class LeafHorizonError(ValueError):
    """A proposed AI Leaf would make a project's short horizon unsafe."""

    def __init__(self, message: str, *, code: str,
                 conflicting_leaf_id: int | None = None,
                 similarity: float | None = None):
        super().__init__(message)
        self.code = str(code)
        self.conflicting_leaf_id = conflicting_leaf_id
        self.similarity = similarity


def _derived_branch_role(title: str, description: str, *, parent_type: str,
                         parent_role: str = "", has_branch_children: bool = False) -> str:
    """Choose a presentation role for legacy Branches without persisting an inference."""
    text = f"{title} {description}".casefold()
    words = set(re.findall(r"[\w]+", text))
    first_word = next(iter(re.findall(r"[\w]+", str(title or "").casefold())), "")
    finite_action_words = {
        "map", "track", "design", "capture", "decide", "analyze", "analyse",
        "build", "create", "launch", "test", "evaluate", "implement", "publish",
    }
    project_words = {
        "project", "experiment", "prototype", "launch", "migration", "redesign",
        "interface", "app", "upwork", "automation", "product", "gig", "test",
        "프로젝트", "실험", "출시", "자동화", "앱", "인터페이스",
    }
    area_words = {
        "career", "work", "health", "wellbeing", "relationship", "relationships",
        "family", "language", "korean", "learning", "education", "finance",
        "finances", "creativity", "creative", "gaming", "games", "hobby",
        "recreation", "league", "legends", "커리어", "직업", "건강", "관계",
        "가족", "언어", "한국어", "학습", "교육", "재정", "창작", "게임", "취미",
    }
    if parent_type == "overgoal":
        if words & project_words or first_word in finite_action_words:
            return "project"
        if words & area_words or has_branch_children:
            return "area"
        return "project"
    if parent_role == "area":
        return "project"
    return "stage"


class GoalStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
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

    def semantic_role(self, goal_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT role,rationale,source,updated_at FROM goal_semantic_role WHERE goal_id=?",
            (int(goal_id),)).fetchone()
        if not row:
            return None
        return {"role": row["role"], "rationale": crypto.dec(row["rationale"]) or "",
                "source": row["source"], "updated_at": row["updated_at"]}

    def _set_semantic_role(self, goal_id: int, role: str, *, rationale: str = "",
                           source: str = "ai", proposal_id: int | None = None,
                           commit: bool = True) -> None:
        node = self.get(int(goal_id))
        prior = self.semantic_role(int(goal_id))
        role = str(role or "").strip().lower()
        if not node or node["type"] != "subgoal":
            raise ValueError("semantic roles apply only to Branches")
        if role not in {"area", "project", "stage"}:
            raise ValueError("Branch role must be Area, Project, or Stage")
        self._validate_semantic_placement(
            "subgoal", role, int(node["parent_id"]),
            nested_stage_justification=rationale)
        self.conn.execute(
            "INSERT INTO goal_semantic_role (goal_id,role,rationale,source,updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET role=excluded.role,"
            "rationale=excluded.rationale,source=excluded.source,updated_at=excluded.updated_at",
            (int(goal_id), role, crypto.enc(str(rationale or "")), str(source or "ai")[:24], _now()))
        if not prior or prior["role"] != role:
            self.conn.execute(
                "INSERT INTO goal_semantic_role_history "
                "(goal_id,old_role,new_role,rationale,source,proposal_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (int(goal_id), prior["role"] if prior else None,
                 role, crypto.enc(str(rationale or "")), str(source or "ai")[:24],
                 proposal_id, _now()))
        if role != "project":
            self.conn.execute(
                "DELETE FROM goal_project_signal WHERE goal_id=? AND kind='currently_working'",
                (int(goal_id),))
        if role != "area":
            self.conn.execute(
                "DELETE FROM goal_project_signal WHERE goal_id=? AND kind='highest_priority'",
                (int(goal_id),))
        self._mark_goal_ai_dirty(int(goal_id))
        if commit:
            self.conn.commit()

    def resolved_semantic_role(self, goal_id: int) -> str | None:
        """Return a stored role, or the same deterministic role used by tree rendering."""
        node = self.get(int(goal_id))
        if not node or node["type"] != "subgoal":
            return None
        stored = self.semantic_role(int(goal_id))
        if stored:
            return str(stored["role"])
        parent = self.get(int(node["parent_id"])) if node.get("parent_id") else None
        parent_role = (self.resolved_semantic_role(int(parent["id"]))
                       if parent and parent["type"] == "subgoal" else "")
        has_branch_children = bool(self.conn.execute(
            "SELECT 1 FROM goal_node WHERE parent_id=? AND node_type='subgoal' "
            "AND status!='archived' LIMIT 1", (int(goal_id),)).fetchone())
        return _derived_branch_role(
            node.get("title", ""), node.get("description", ""),
            parent_type=str(parent.get("type") if parent else ""),
            parent_role=str(parent_role or ""),
            has_branch_children=has_branch_children)

    def project_signals(self) -> dict[str, int | None]:
        """Return the singleton Area priority and Project work signals.

        Stale rows are ignored rather than silently reclassifying a node. A
        later explicit selection atomically replaces the singleton for that
        signal kind.
        """
        result: dict[str, int | None] = {
            "highest_priority": None,
            "currently_working": None,
        }
        for row in self.conn.execute(
                "SELECT kind,goal_id FROM goal_project_signal ORDER BY kind"):
            kind = str(row["kind"])
            node = self.get(int(row["goal_id"]))
            expected_role = ("area" if kind == "highest_priority" else "project")
            if (kind in PROJECT_SIGNAL_KINDS and node
                    and node.get("status") == "active"
                    and node.get("type") == "subgoal"
                    and self.resolved_semantic_role(int(node["id"])) == expected_role):
                result[kind] = int(node["id"])
        return result

    def project_focus(self, goal_id: int) -> dict[str, bool]:
        signals = self.project_signals()
        goal_id = int(goal_id)
        return {
            "highest_priority": signals["highest_priority"] == goal_id,
            "currently_working": signals["currently_working"] == goal_id,
            "auto_current": (signals["currently_working"] is None and
                             self.effective_current_project_id() == goal_id),
        }

    def effective_current_project_id(self) -> int | None:
        signals = self.project_signals()
        current = signals["currently_working"]
        area = signals["highest_priority"]
        if current is not None and (area is None or self._is_descendant(current, area)):
            return int(current)
        if area is None:
            return None
        queue = [int(row["id"]) for row in self.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND status IN ('active','paused') "
            "ORDER BY position,id", (int(area),)).fetchall()]
        while queue:
            goal_id = queue.pop(0)
            node = self.get(goal_id)
            if not node:
                continue
            if (node["type"] == "subgoal" and
                    self.resolved_semantic_role(goal_id) == "project"):
                return goal_id
            queue[0:0] = [int(row["id"]) for row in self.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=? AND status IN ('active','paused') "
                "ORDER BY position,id", (goal_id,)).fetchall()]
        return None

    def set_project_signal(self, goal_id: int, kind: str,
                           enabled: bool = True) -> dict[str, int | None]:
        """Set or clear the global Area-priority or Project-current signal."""
        goal_id = int(goal_id)
        kind = str(kind or "").strip().lower()
        if kind not in PROJECT_SIGNAL_KINDS:
            raise ValueError("unknown attention signal")
        node = self.get(goal_id)
        expected_role = "area" if kind == "highest_priority" else "project"
        if (not node or node.get("status") != "active"
                or node.get("type") != "subgoal"
                or self.resolved_semantic_role(goal_id) != expected_role):
            raise ValueError(f"only an active {expected_role.title()} can carry {kind}")
        if not self.semantic_role(goal_id):
            self._set_semantic_role(
                goal_id, expected_role,
                rationale=f"User selected this {expected_role.title()} for explicit attention.",
                source="user", commit=False)
        previous = self.conn.execute(
            "SELECT goal_id FROM goal_project_signal WHERE kind=?", (kind,)).fetchone()
        if enabled:
            if kind == "currently_working":
                area = self.conn.execute(
                    "SELECT goal_id FROM goal_project_signal WHERE kind='highest_priority'").fetchone()
                if area and not self._is_descendant(goal_id, int(area["goal_id"])):
                    raise ValueError("Current Project must live inside the Highest priority Area")
            self.conn.execute(
                "INSERT INTO goal_project_signal (kind,goal_id,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(kind) DO UPDATE SET goal_id=excluded.goal_id,"
                "updated_at=excluded.updated_at",
                (kind, goal_id, _now()))
            if kind == "highest_priority":
                current = self.conn.execute(
                    "SELECT goal_id FROM goal_project_signal WHERE kind='currently_working'").fetchone()
                if current and not self._is_descendant(int(current["goal_id"]), goal_id):
                    self.conn.execute(
                        "DELETE FROM goal_project_signal WHERE kind='currently_working'")
        else:
            self.conn.execute(
                "DELETE FROM goal_project_signal WHERE kind=? AND goal_id=?",
                (kind, goal_id))
        self._mark_goal_ai_dirty(
            goal_id, int(previous["goal_id"]) if previous else None)
        self.conn.commit()
        return self.project_signals()

    def _is_descendant(self, goal_id: int, ancestor_id: int) -> bool:
        current, seen = int(goal_id), set()
        while current and current not in seen:
            if current == int(ancestor_id):
                return True
            seen.add(current)
            row = self.conn.execute(
                "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
            current = int(row["parent_id"] or 0) if row else 0
        return False

    def _validate_semantic_placement(self, node_type: str, semantic_role: str | None,
                                     parent_id: int, *,
                                     nested_stage_justification: str = "") -> None:
        """Enforce one unambiguous Area → Project → Stage hierarchy."""
        role = str(semantic_role or "").strip().lower() or None
        if str(node_type) != "subgoal" or role not in {"area", "project", "stage"}:
            return
        parent = self.get(int(parent_id))
        if not parent:
            raise ValueError("semantic destination parent not found")
        parent_role = (self.resolved_semantic_role(int(parent_id))
                       if parent["type"] == "subgoal" else None)
        if role == "area" and not (parent["type"] == "overgoal" or parent_role == "area"):
            raise ValueError("an Area must live beneath a Root or another Area")
        if role == "project" and not (parent["type"] == "overgoal" or parent_role == "area"):
            raise ValueError("a Project must live beneath a Root or Area")
        if role == "stage" and parent_role != "project":
            raise ValueError("a Stage must live directly beneath a Project")

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
                   summary: str = "", detail: str = "", _commit: bool = True) -> None:
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
        if _commit:
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
        signals = self.project_signals()
        catalog = []
        for row in rows:
            goal_id = int(row["id"])
            semantic_role = (self.resolved_semantic_role(goal_id)
                             if row["node_type"] == "subgoal" else None)
            project_focus = ({
                "highest_priority": signals["highest_priority"] == goal_id,
                "currently_working": signals["currently_working"] == goal_id,
            } if semantic_role == "project" else None)
            catalog.append({
                "id": goal_id,
                "type": type_label.get(row["node_type"], row["node_type"]),
                "semantic_role": semantic_role,
                "project_focus": project_focus,
                "title": crypto.dec(row["title"]) or "",
                "path": " › ".join(self._goal_path_titles(goal_id)),
                "status": row["status"],
            })
        return catalog

    def leaf_horizon(self, *, recent_days: int = 7, max_projects: int = 6,
                     max_leaves: int = 4, max_chars: int = 220) -> list[dict]:
        """Per-project view of open Leaves plus recent completions.

        This is the just-in-time planning context: which projects have work
        in flight, what the next (possibly provisional) step is, and what
        just finished. Unlike catalog(), it carries bounded descriptions so
        an AI can judge whether the plan still fits — but never evidence,
        notes, or the whole tree."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(0, int(recent_days)))).isoformat()
        rows = self.conn.execute(
            "SELECT id,parent_id,title,description,status,priority,position,"
            "completed_at FROM goal_node WHERE node_type='task' "
            "AND status!='archived' ORDER BY parent_id,position,id").fetchall()
        by_parent: dict[int, dict] = {}
        for row in rows:
            status = row["status"]
            completed_at = row["completed_at"] or ""
            if status == "completed" and completed_at < cutoff:
                continue
            parent_id = int(row["parent_id"])
            group = by_parent.setdefault(parent_id, {"open": [], "recent_done": []})
            leaf = {
                "id": int(row["id"]),
                "title": crypto.dec(row["title"]) or "",
                "description": (crypto.dec(row["description"]) or "")[:max_chars],
                "status": status,
                "priority": row["priority"],
            }
            if status == "completed":
                leaf["completed_at"] = completed_at
                group["recent_done"].append(leaf)
            else:
                group["open"].append(leaf)
        projects = []
        for parent_id, group in by_parent.items():
            parent = self.get(parent_id)
            if not parent or parent.get("status") == "archived":
                continue
            semantic_role = (self.resolved_semantic_role(parent_id)
                             if parent.get("type") == "subgoal" else None)
            project_focus = (self.project_focus(parent_id)
                             if semantic_role == "project" else {
                                 "highest_priority": False,
                                 "currently_working": False,
                             })
            group["recent_done"].sort(key=lambda l: l.get("completed_at") or "",
                                      reverse=True)
            projects.append({
                "project_id": parent_id,
                "project_title": parent["title"],
                "path": " › ".join(self._goal_path_titles(parent_id)),
                "project_status": parent.get("status"),
                "semantic_role": semantic_role,
                "project_focus": project_focus,
                "attention_active": bool(
                    project_focus["highest_priority"]
                    or project_focus["currently_working"]),
                "open": group["open"][:max_leaves],
                "recent_done": group["recent_done"][:max_leaves],
            })
        # Projects with in-flight work first, then those needing a next step.
        projects.sort(key=lambda p: (not p["attention_active"],
                                     not p["open"], not p["recent_done"],
                                     p["project_id"]))
        return projects[:max_projects]

    def open_leaf_count(self, parent_id: int) -> int:
        """Open (active or paused) Leaves directly under one node."""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM goal_node WHERE parent_id=? "
            "AND node_type='task' AND status IN ('active','paused')",
            (int(parent_id),)).fetchone()[0])

    def _pending_ref_exclusions(self, exclude_refs) -> set[str]:
        if exclude_refs is None:
            return set()
        if isinstance(exclude_refs, (str, bytes, Mapping)):
            values = [exclude_refs]
        else:
            try:
                values = list(exclude_refs)
            except TypeError:
                values = [exclude_refs]
        excluded: set[str] = set()
        for raw in values:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            if isinstance(raw, str):
                value = raw.strip().lower()
                if value:
                    excluded.add(value)
                continue
            if isinstance(raw, Mapping):
                kind = str(raw.get("kind") or raw.get("source_kind") or
                           raw.get("source") or "").strip().lower()
                source_id = raw.get("id", raw.get("source_id"))
                suffix = raw.get("position", raw.get("step"))
                if kind and source_id not in (None, ""):
                    value = f"{kind}:{source_id}"
                    if suffix not in (None, ""):
                        value += f":{suffix}"
                    excluded.add(value.lower())
                continue
            if isinstance(raw, (tuple, list)) and len(raw) >= 2:
                value = ":".join(str(part) for part in raw if part not in (None, ""))
                if value:
                    excluded.add(value.lower())
        return excluded

    @staticmethod
    def _pending_ref_is_excluded(excluded: set[str], *refs: str) -> bool:
        for ref in refs:
            value = str(ref or "").strip().lower()
            if not value:
                continue
            if value in excluded:
                return True
            # Excluding a whole source (for example planning:12) also excludes
            # its individual steps (planning:12:0, planning:12:1, ...).
            if any(value.startswith(item + ":") for item in excluded):
                return True
        return False

    def _decode_pending_payload(self, value) -> dict:
        try:
            decoded = crypto.dec(value) or "{}"
            payload = json.loads(decoded)
        except (TypeError, ValueError, json.JSONDecodeError, crypto.EncryptionError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _pending_leaf_operations(self, parent_id: int, *, exclude_refs=None) -> list[dict]:
        """Internal cross-surface scan; source metadata never leaves this method."""
        parent_id = int(parent_id)
        excluded = self._pending_ref_exclusions(exclude_refs)
        tables = {str(row["name"]) for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        operations: list[dict] = []

        def add(source: str, row_id, reservation: Mapping[str, Any], *,
                refs: Iterable[str] = (), **metadata) -> None:
            all_refs = [f"{source}:{row_id}", *[str(ref) for ref in refs]]
            if self._pending_ref_is_excluded(excluded, *all_refs):
                return
            try:
                reservation_parent = int(reservation.get("parent_id"))
            except (TypeError, ValueError):
                return
            if reservation_parent != parent_id:
                return
            status = str(reservation.get("status") or "active").strip().lower()
            replaces = reservation.get("replaces_leaf_id")
            try:
                replaces = int(replaces) if replaces not in (None, "") else None
            except (TypeError, ValueError):
                replaces = None
            title = str(reservation.get("title") or "").strip()
            if status in {"active", "paused"} and not normalize_leaf_title(title):
                return
            operations.append({
                "source": source, "row_id": row_id,
                "refs": tuple(all_refs),
                "reservation": {
                    "parent_id": parent_id,
                    "title": title,
                    "description": str(reservation.get("description") or ""),
                    "status": status,
                    "replaces_leaf_id": replaces,
                },
                **metadata,
            })

        def node(goal_id) -> dict | None:
            try:
                return self.get(int(goal_id))
            except (TypeError, ValueError):
                return None

        def normalized_type(value, *, parent_type: str = "") -> str:
            raw = str(value or "").strip().lower()
            mapped = {"root": "overgoal", "branch": "subgoal", "leaf": "task"}.get(raw, raw)
            if mapped in {"overgoal", "subgoal", "task"}:
                return mapped
            return "overgoal" if parent_type == "umbrella" else "task"

        # GoalAI proposals. Replacement mappings model the proposed final
        # Leaf, so a rename/update does not consume a second horizon slot.
        if "goal_agent_proposal" in tables:
            rows = self.conn.execute(
                "SELECT id,target_node_id,proposal_type,payload_json FROM "
                "goal_agent_proposal WHERE status='open' ORDER BY id").fetchall()
            for row in rows:
                proposal_id = int(row["id"])
                payload = self._decode_pending_payload(row["payload_json"])
                kind = str(row["proposal_type"] or "")
                target = node(row["target_node_id"])
                if kind == "create_child" and target:
                    child_type = normalized_type(payload.get("type"), parent_type=target["type"])
                    if child_type == "task":
                        add("goal_ai", proposal_id, {
                            "parent_id": target["id"], "title": payload.get("title"),
                            "description": payload.get("description"), "status": "active",
                        })
                    continue
                if kind in {"update_fields", "pause", "archive"} and target and target["type"] == "task":
                    add("goal_ai", proposal_id, {
                        "parent_id": target.get("parent_id"),
                        "title": payload.get("title") or target.get("title"),
                        "description": payload.get("description") or target.get("description"),
                        "status": ("paused" if kind == "pause" else
                                   "archived" if kind == "archive" else target.get("status")),
                        "replaces_leaf_id": target["id"],
                    })
                    continue
                changes = ([payload] if kind == "restructure_node" else
                           list(payload.get("changes") or []) if kind == "restructure_tree" else [])
                for change_index, change in enumerate(changes):
                    if not isinstance(change, Mapping):
                        continue
                    target_leaf = target if kind == "restructure_node" else node(change.get("goal_id"))
                    if not target_leaf or target_leaf["type"] != "task":
                        continue
                    old_parent = int(target_leaf.get("parent_id") or 0)
                    try:
                        new_parent = int(change.get("parent_id"))
                    except (TypeError, ValueError):
                        new_parent = old_parent
                    new_type = normalized_type(change.get("new_type") or target_leaf["type"])
                    step_refs = [f"goal_ai:{proposal_id}:{change_index}"]
                    if old_parent != new_parent or new_type != "task":
                        add("goal_ai", proposal_id, {
                            "parent_id": old_parent, "status": "removed",
                            "replaces_leaf_id": target_leaf["id"],
                        }, refs=step_refs)
                    if new_type == "task":
                        add("goal_ai", proposal_id, {
                            "parent_id": new_parent, "title": target_leaf["title"],
                            "description": target_leaf["description"],
                            "status": target_leaf["status"],
                            "replaces_leaf_id": target_leaf["id"],
                        }, refs=step_refs)

        # Companion cards persist independently in every chat.
        if "companion_pending_proposal" in tables:
            rows = self.conn.execute(
                "SELECT id,chat_id,position,payload FROM companion_pending_proposal "
                "ORDER BY chat_id,position,id").fetchall()
            for row in rows:
                row_id = int(row["id"])
                chat_id = str(row["chat_id"])
                position = int(row["position"])
                refs = [f"companion:{chat_id}:{position}", f"companion:{chat_id}"]
                payload = self._decode_pending_payload(row["payload"])
                action = str(payload.get("action") or "").strip().lower()
                target = node(payload.get("target_node_id"))
                if action == "create_leaf" and target:
                    add("companion", row_id, {
                        "parent_id": target["id"], "title": payload.get("label"),
                        "description": payload.get("directive"), "status": "active",
                    }, refs=refs, chat_id=chat_id)
                    continue
                if action == "replan_project" and target:
                    for step_index, step in enumerate(list(payload.get("steps") or [])):
                        if not isinstance(step, Mapping):
                            continue
                        op = str(step.get("op") or "").lower()
                        step_refs = [*refs, f"companion:{chat_id}:{position}:{step_index}"]
                        if op == "create":
                            add("companion", row_id, {
                                "parent_id": target["id"], "title": step.get("title"),
                                "description": step.get("description"), "status": "active",
                            }, refs=step_refs, chat_id=chat_id)
                            continue
                        target_leaf = node(step.get("leaf_id"))
                        if not target_leaf or target_leaf["type"] != "task":
                            continue
                        final_title = (step.get("new_title") if op == "rename"
                                       else target_leaf.get("title"))
                        final_description = (step.get("description") if op in {"rename", "update"}
                                             and step.get("description") else
                                             target_leaf.get("description"))
                        final_status = ("archived" if op == "archive" else
                                        "completed" if op == "complete" else
                                        target_leaf.get("status"))
                        add("companion", row_id, {
                            "parent_id": target["id"], "title": final_title,
                            "description": final_description, "status": final_status,
                            "replaces_leaf_id": target_leaf["id"],
                        }, refs=step_refs, chat_id=chat_id)
                    continue
                if not target or target["type"] != "task" or not target.get("parent_id"):
                    continue
                old_parent = int(target["parent_id"])
                if action == "rename_node":
                    add("companion", row_id, {
                        "parent_id": old_parent, "title": payload.get("new_title"),
                        "description": target.get("description"), "status": target.get("status"),
                        "replaces_leaf_id": target["id"],
                    }, refs=refs, chat_id=chat_id)
                elif action == "delete_node":
                    add("companion", row_id, {
                        "parent_id": old_parent, "status": "removed",
                        "replaces_leaf_id": target["id"],
                    }, refs=refs, chat_id=chat_id)
                elif action == "move_node":
                    new_parent_node = node(payload.get("new_parent_id"))
                    if not new_parent_node:
                        continue
                    new_parent = int(new_parent_node["id"])
                    if new_parent != old_parent:
                        add("companion", row_id, {
                            "parent_id": old_parent, "status": "removed",
                            "replaces_leaf_id": target["id"],
                        }, refs=refs, chat_id=chat_id)
                    add("companion", row_id, {
                        "parent_id": new_parent, "title": target.get("title"),
                        "description": target.get("description"), "status": target.get("status"),
                        "replaces_leaf_id": target["id"] if new_parent == old_parent else None,
                    }, refs=refs, chat_id=chat_id)

        # GoalAI gardening cards can rewrite one Leaf into a different final
        # identity, split it into siblings, or merge sibling Leaves. Model only
        # their final open/removed effects; rationale and evidence stay private.
        if "goal_gardening_proposal" in tables:
            rows = self.conn.execute(
                "SELECT id,target_node_id,proposal_type,payload_json FROM "
                "goal_gardening_proposal WHERE status IN ('open','refined') ORDER BY id"
            ).fetchall()
            for row in rows:
                proposal_id = int(row["id"])
                target = node(row["target_node_id"])
                if not target or target["type"] != "task" or not target.get("parent_id"):
                    continue
                payload = self._decode_pending_payload(row["payload_json"])
                kind = str(row["proposal_type"] or "")
                parent = int(target["parent_id"])
                if kind == "rewrite":
                    add("gardening", proposal_id, {
                        "parent_id": parent, "title": payload.get("title") or target["title"],
                        "description": payload.get("description") or target["description"],
                        "status": target["status"], "replaces_leaf_id": target["id"],
                    })
                elif kind == "split":
                    parts = [part for part in list(payload.get("parts") or [])
                             if isinstance(part, Mapping) and
                             normalize_leaf_title(str(part.get("title") or ""))]
                    if not parts:
                        continue
                    add("gardening", proposal_id, {
                        "parent_id": parent, "title": parts[0].get("title"),
                        "description": parts[0].get("description"),
                        "status": target["status"], "replaces_leaf_id": target["id"],
                    }, refs=[f"gardening:{proposal_id}:0"])
                    for index, part in enumerate(parts[1:], 1):
                        add("gardening", proposal_id, {
                            "parent_id": parent, "title": part.get("title"),
                            "description": part.get("description"), "status": "active",
                        }, refs=[f"gardening:{proposal_id}:{index}"])
                elif kind == "merge":
                    add("gardening", proposal_id, {
                        "parent_id": parent, "title": payload.get("title") or target["title"],
                        "description": payload.get("description") or target["description"],
                        "status": target["status"], "replaces_leaf_id": target["id"],
                    })
                    for source_id in list(payload.get("source_node_ids") or [])[:6]:
                        source = node(source_id)
                        if source and source["type"] == "task" and source.get("parent_id") == parent:
                            add("gardening", proposal_id, {
                                "parent_id": parent, "status": "removed",
                                "replaces_leaf_id": source["id"],
                            })
                elif kind in {"pause", "archive"}:
                    add("gardening", proposal_id, {
                        "parent_id": parent, "title": target["title"],
                        "description": target["description"],
                        "status": "paused" if kind == "pause" else "archived",
                        "replaces_leaf_id": target["id"],
                    })

        if "goal_leaf_workspace_proposal" in tables:
            rows = self.conn.execute(
                "SELECT id,node_id,proposal_type,payload_json FROM "
                "goal_leaf_workspace_proposal WHERE status='open' ORDER BY id"
            ).fetchall()
            for row in rows:
                proposal_id = int(row["id"])
                target = node(row["node_id"])
                if not target or target["type"] != "task" or not target.get("parent_id"):
                    continue
                payload = self._decode_pending_payload(row["payload_json"])
                kind = str(row["proposal_type"] or "")
                workspace_action = str(payload.get("_workspace_action") or "")
                if kind == "complete_leaf":
                    final_status = "completed"
                elif workspace_action in {"reshape", "reopen"}:
                    final_status = "active"
                else:
                    continue
                add("leaf_workspace", proposal_id, {
                    "parent_id": target["parent_id"], "title": target["title"],
                    "description": target["description"], "status": final_status,
                    "replaces_leaf_id": target["id"],
                })

        if "curiosity_classification_proposal" in tables:
            rows = self.conn.execute(
                "SELECT id,payload_json FROM curiosity_classification_proposal "
                "WHERE status='open' AND proposal_type='create_leaf' ORDER BY id").fetchall()
            for row in rows:
                proposal_id = int(row["id"])
                envelope = self._decode_pending_payload(row["payload_json"])
                payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
                add("curiosity", proposal_id, {
                    "parent_id": payload.get("parent_id"), "title": payload.get("title"),
                    "description": payload.get("description"), "status": "active",
                })

        # A planning session can reserve only a direct child of its persisted
        # target. Descendants live under not-yet-created parents and are checked
        # as an in-draft sibling group before commit.
        if "goal_plan_session" in tables:
            rows = self.conn.execute(
                "SELECT id,target_parent_id,draft_json FROM goal_plan_session "
                "WHERE status IN ('active','ready') ORDER BY id").fetchall()
            for row in rows:
                session_id = int(row["id"])
                draft = self._decode_pending_payload(row["draft_json"])
                nodes = list(draft.get("nodes") or [])
                if not nodes or not isinstance(nodes[0], Mapping):
                    continue
                raw = nodes[0]
                target = node(row["target_parent_id"])
                children = list(raw.get("children") or [])
                if (not target or target["type"] == "umbrella"
                        or normalized_type(raw.get("type"), parent_type=target["type"]) != "task"
                        or children):
                    continue
                add("planning", session_id, {
                    "parent_id": target["id"], "title": raw.get("title"),
                    "description": raw.get("description"), "status": "active",
                }, refs=[f"planning:{session_id}:0"], session_id=session_id)
        return operations

    def pending_leaf_reservations(self, parent_id: int, *, exclude_refs=None) -> list[dict]:
        """Return privacy-minimal pending Leaf effects across all proposal surfaces."""
        return [dict(item["reservation"])
                for item in self._pending_leaf_operations(
                    int(parent_id), exclude_refs=exclude_refs)]

    def retire_pending_leaf_operations(self, parent_id: int, *, exclude_refs=None) -> dict:
        """Retire cross-surface Leaf cards invalidated by an approved replan.

        The caller must exclude the card currently being approved when it is
        still persisted. The result contains counts only, never proposal text.
        """
        operations = self._pending_leaf_operations(
            int(parent_id), exclude_refs=exclude_refs)
        sources: dict[str, set] = {name: set() for name in (
            "goal_ai", "companion", "gardening", "leaf_workspace",
            "curiosity", "planning")}
        chats: set[str] = set()
        for item in operations:
            sources[item["source"]].add(item["row_id"])
            if item.get("chat_id"):
                chats.add(str(item["chat_id"]))
        now = _now()
        savepoint = "faerie_retire_leaf_operations"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            if sources["goal_ai"]:
                ids = sorted(int(value) for value in sources["goal_ai"])
                self.conn.execute(
                    f"UPDATE goal_agent_proposal SET status='stale',resolved_at=? "
                    f"WHERE status='open' AND id IN ({','.join('?' for _ in ids)})",
                    [now, *ids])
            if sources["curiosity"]:
                ids = sorted(int(value) for value in sources["curiosity"])
                self.conn.execute(
                    f"UPDATE curiosity_classification_proposal SET status='dismissed',resolved_at=? "
                    f"WHERE status='open' AND id IN ({','.join('?' for _ in ids)})",
                    [now, *ids])
            if sources["gardening"]:
                ids = sorted(int(value) for value in sources["gardening"])
                self.conn.execute(
                    f"UPDATE goal_gardening_proposal SET status='stale',resolved_at=? "
                    f"WHERE status IN ('open','refined') AND id IN "
                    f"({','.join('?' for _ in ids)})", [now, *ids])
            if sources["leaf_workspace"]:
                ids = sorted(int(value) for value in sources["leaf_workspace"])
                self.conn.execute(
                    f"UPDATE goal_leaf_workspace_proposal SET status='rejected',resolved_at=? "
                    f"WHERE status='open' AND id IN "
                    f"({','.join('?' for _ in ids)})", [now, *ids])
            if sources["companion"]:
                ids = sorted(int(value) for value in sources["companion"])
                self.conn.execute(
                    f"DELETE FROM companion_pending_proposal WHERE id IN "
                    f"({','.join('?' for _ in ids)})", ids)
                for chat_id in chats:
                    rows = self.conn.execute(
                        "SELECT id FROM companion_pending_proposal WHERE chat_id=? "
                        "ORDER BY position,id", (chat_id,)).fetchall()
                    for position, row in enumerate(rows):
                        self.conn.execute(
                            "UPDATE companion_pending_proposal SET position=? WHERE id=?",
                            (position, int(row["id"])))
            if sources["planning"]:
                ids = sorted(int(value) for value in sources["planning"])
                self.conn.execute(
                    f"UPDATE goal_plan_session SET status='abandoned',updated_at=? "
                    f"WHERE status IN ('active','ready') AND id IN "
                    f"({','.join('?' for _ in ids)})", [now, *ids])
                has_curiosity_items = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='curiosity_item'").fetchone()
                if has_curiosity_items:
                    self.conn.execute(
                        f"UPDATE curiosity_item SET implementation_session_id=NULL "
                        f"WHERE implementation_session_id IN "
                        f"({','.join('?' for _ in ids)})", ids)
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        by_source = {name: len(values) for name, values in sources.items()}
        return {"retired": sum(by_source.values()), "by_source": by_source}

    def validate_leaf_candidate(
            self, parent_id: int, title: str, *, description: str = "",
            reservations: Iterable[Mapping[str, Any]] | None = None,
            horizon: int = 2,
            exclude_leaf_ids: Iterable[int] | None = None) -> dict:
        """Validate one AI-proposed open Leaf against committed and staged work.

        ``reservations`` are other not-yet-applied open Leaf candidates for the
        same parent. Low-level ``create`` deliberately remains unrestricted for
        imports, manual edits, and test setup; AI entry points call this method
        while staging and call ``create_ai_leaf`` at approval time.
        """
        try:
            parent_id = int(parent_id)
            limit = min(2, int(horizon))
        except (TypeError, ValueError):
            raise LeafHorizonError(
                "the Leaf needs a valid parent and horizon",
                code="invalid_parent")
        if limit < 1:
            raise ValueError("Leaf horizon must be at least one")
        parent = self.get(parent_id)
        if (not parent or parent.get("status") == "archived"
                or parent.get("type") not in {"overgoal", "subgoal"}):
            raise LeafHorizonError(
                "the Leaf parent is missing, archived, or cannot contain Leaves",
                code="invalid_parent")
        candidate_title = str(title or "").strip()
        candidate_key = normalize_leaf_title(candidate_title)
        if not candidate_key:
            raise LeafHorizonError("Leaf title is required", code="invalid_title")
        excluded: set[int] = set()
        for value in exclude_leaf_ids or ():
            try:
                excluded.add(int(value))
            except (TypeError, ValueError):
                continue
        reserved: list[dict] = []
        replacement_ids: set[int] = set()
        for raw in reservations or ():
            if not isinstance(raw, Mapping):
                continue
            raw_parent = raw.get("parent_id", raw.get("target_node_id"))
            if raw_parent not in (None, ""):
                try:
                    if int(raw_parent) != parent_id:
                        continue
                except (TypeError, ValueError):
                    continue
            replaces = raw.get("replaces_leaf_id")
            replacement_id = None
            try:
                if replaces not in (None, ""):
                    replacement_id = int(replaces)
                    replacement_ids.add(replacement_id)
            except (TypeError, ValueError):
                pass
            if str(raw.get("status") or "active").lower() not in {"active", "paused"}:
                continue
            reserved_title = str(raw.get("title") or raw.get("label") or "").strip()
            if not normalize_leaf_title(reserved_title):
                continue
            reserved.append({
                "id": replacement_id,
                "title": reserved_title,
                "description": str(raw.get("description") or raw.get("directive") or ""),
            })
        excluded.update(replacement_ids)
        rows = self.conn.execute(
            "SELECT id,title,description FROM goal_node WHERE parent_id=? "
            "AND node_type='task' AND status IN ('active','paused') "
            "ORDER BY position,id", (parent_id,)).fetchall()
        committed = [{
            "id": int(row["id"]),
            "title": crypto.dec(row["title"]) or "",
            "description": crypto.dec(row["description"]) or "",
        } for row in rows if int(row["id"]) not in excluded]
        from .inference import concept_similarity
        for other in [*committed, *reserved]:
            other_title = str(other.get("title") or "")
            if normalize_leaf_title(other_title) == candidate_key:
                raise LeafHorizonError(
                    "an open Leaf with the same normalized title already exists",
                    code="duplicate_title",
                    conflicting_leaf_id=other.get("id"))
            similarity = float(concept_similarity(candidate_title, other_title))
            if similarity >= 0.55:
                raise LeafHorizonError(
                    "the proposed Leaf strongly overlaps another open Leaf",
                    code="semantic_overlap",
                    conflicting_leaf_id=other.get("id"), similarity=similarity)
        existing_count = len(committed)
        reserved_count = len(reserved)
        if existing_count + reserved_count + 1 > limit:
            raise LeafHorizonError(
                "the project's open Leaf horizon is already reserved",
                code="horizon_full")
        return {
            "parent_id": parent_id,
            "horizon": limit,
            "committed_open": existing_count,
            "reserved_open": reserved_count,
            "open_after_create": existing_count + reserved_count + 1,
        }

    def _project_subtree_plan_nodes(
            self, project_id: int) -> tuple[list[dict], list[dict]]:
        """(leaves, stages) of one project's live plan subtree.

        Leaves are every non-archived task reachable through non-archived
        Branch descendants, in depth-first (position,id) order — the same
        flat execution order the focus path reads. Stages are the traversed
        Branch descendants themselves, in the same order.
        """
        leaves: list[dict] = []
        stages: list[dict] = []

        def walk(parent_id: int) -> None:
            rows = self.conn.execute(
                "SELECT * FROM goal_node WHERE parent_id=? AND status!='archived' "
                "ORDER BY position,id", (int(parent_id),)).fetchall()
            for row in rows:
                node = self._row(row)
                if node["type"] == "task":
                    leaves.append(node)
                elif node["type"] == "subgoal":
                    stages.append(node)
                    walk(int(node["id"]))

        walk(int(project_id))
        return leaves, stages

    def project_subtree_leaves(self, project_id: int) -> list[dict]:
        """Every live Leaf of one project's plan, including under Stages."""
        leaves, _stages = self._project_subtree_plan_nodes(project_id)
        return leaves

    def subtree_open_leaf_count(self, project_id: int) -> int:
        """Open (active or paused) Leaves anywhere in one project's subtree."""
        return sum(1 for leaf in self.project_subtree_leaves(project_id)
                   if leaf.get("status") in {"active", "paused"})

    def replan_expected_versions(self, project_id: int) -> dict[str, str]:
        """Version snapshot for one project and its whole live plan subtree.

        Replan cards persist this snapshot when staged. Approval compares it
        inside the same savepoint as the apply so a newer edit can never be
        overwritten by an older card. Stages are included so a structural
        edit under the project invalidates older cards too.
        """
        project_id = int(project_id)
        anchor = self.conn.execute(
            "SELECT id FROM goal_node WHERE id=?", (project_id,)).fetchone()
        if not anchor:
            raise ValueError("replan target not found")
        leaves, stages = self._project_subtree_plan_nodes(project_id)
        ids = [project_id] + [int(node["id"]) for node in [*stages, *leaves]]
        rows = self.conn.execute(
            "SELECT id,parent_id,node_type,title,description,status,priority,due_date,"
            f"position,updated_at,completed_at FROM goal_node WHERE id IN "
            f"({','.join('?' for _ in ids)}) ORDER BY id", ids).fetchall()
        columns = ("id", "parent_id", "node_type", "title", "description",
                   "status", "priority", "due_date", "position", "updated_at",
                   "completed_at")
        return {
            str(int(row["id"])): hashlib.sha256(
                json.dumps([str(row[column] or "") for column in columns],
                           ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            for row in rows
        }

    def validate_replan_project(
            self, project_id: int, steps: Iterable[Mapping[str, Any]], *,
            project_update: Mapping[str, Any] | None = None,
            expected_versions: Mapping[str | int, str] | None = None,
            horizon: int = 2) -> dict:
        """Validate and normalize one complete direct-Leaf replan."""
        try:
            project_id = int(project_id)
            limit = min(2, int(horizon))
        except (TypeError, ValueError):
            raise ValueError("replan needs a valid project and horizon")
        if limit < 1:
            raise ValueError("Leaf horizon must be at least one")
        project = self.get(project_id)
        if (not project or project.get("status") == "archived"
                or project.get("type") not in {"overgoal", "subgoal"}):
            raise ValueError("replan target must be an active Root or Branch")
        update = dict(project_update or {})
        unknown_update = set(update) - {"title", "description"}
        if unknown_update:
            raise ValueError("project_update supports only title and description")
        if "title" in update:
            update["title"] = str(update.get("title") or "").strip()
            if not update["title"]:
                raise ValueError("project_update title cannot be blank")
        if "description" in update:
            update["description"] = str(update.get("description") or "")

        # The replan owns the project's whole live plan: Leaves under Stages
        # are addressed too, and the applied plan is flat (see apply).
        direct = {int(node["id"]): node
                  for node in self.project_subtree_leaves(project_id)}
        if expected_versions is not None:
            expected = {str(key): str(value) for key, value in
                        dict(expected_versions).items()}
            current = self.replan_expected_versions(project_id)
            if expected != current:
                raise ValueError(
                    "replan is stale because the project or one of its Leaves changed")
        raw_steps = list(steps or ())
        if not raw_steps:
            raise ValueError("replan needs at least one Leaf operation")
        allowed_ops = {"keep", "rename", "update", "archive", "complete", "create"}
        seen_ids: set[int] = set()
        normalized_steps: list[dict] = []
        create_titles: list[str] = []
        final_open: list[dict] = []
        from .inference import concept_similarity

        def reject_overlap(title_value: str, candidates: list[dict], *,
                           duplicate_code: str = "duplicate_title") -> None:
            key = normalize_leaf_title(title_value)
            for candidate in candidates:
                other_title = str(candidate.get("title") or "")
                if normalize_leaf_title(other_title) == key:
                    raise LeafHorizonError(
                        "the replan contains duplicate open Leaf titles",
                        code=duplicate_code,
                        conflicting_leaf_id=candidate.get("id"))
                score = float(concept_similarity(title_value, other_title))
                if score >= 0.55:
                    raise LeafHorizonError(
                        "the replan contains strongly overlapping open Leaves",
                        code="semantic_overlap",
                        conflicting_leaf_id=candidate.get("id"), similarity=score)

        for raw in raw_steps:
            if not isinstance(raw, Mapping):
                raise ValueError("every replan step must be an object")
            op = str(raw.get("op") or "").strip().lower()
            if op not in allowed_ops:
                raise ValueError("replan contains an unsupported Leaf operation")
            if op == "create":
                title_value = str(raw.get("title") or "").strip()
                if not normalize_leaf_title(title_value):
                    raise ValueError("create steps need a Leaf title")
                reject_overlap(title_value, [
                    {"id": None, "title": value} for value in create_titles
                ], duplicate_code="duplicate_create")
                create_titles.append(title_value)
                priority = str(raw.get("priority") or "normal").strip().lower()
                if priority not in PRIORITIES:
                    priority = "normal"
                due_date = _clean_date(raw.get("due_date"))
                normalized = {
                    "op": "create", "title": title_value,
                    "description": str(raw.get("description") or "").strip(),
                    "priority": priority, "due_date": due_date,
                }
                normalized_steps.append(normalized)
                final_open.append({"id": None, "title": title_value})
                continue
            try:
                leaf_id = int(raw.get("leaf_id"))
            except (TypeError, ValueError):
                raise ValueError(f"{op} steps need a valid Leaf id")
            if leaf_id in seen_ids:
                raise ValueError("a Leaf can appear only once in a replan")
            node = direct.get(leaf_id)
            if not node:
                referenced = self.get(leaf_id)
                if referenced and referenced.get("type") == "task":
                    raise ValueError("referenced Leaf does not belong to the target project")
                raise ValueError("referenced Leaf is missing, archived, or not a Leaf")
            seen_ids.add(leaf_id)
            normalized = {"op": op, "leaf_id": leaf_id}
            final_title = str(node.get("title") or "")
            if op == "rename":
                final_title = str(raw.get("new_title") or "").strip()
                if not normalize_leaf_title(final_title):
                    raise ValueError("rename steps need a new Leaf title")
                normalized["new_title"] = final_title
                if str(raw.get("description") or "").strip():
                    normalized["description"] = str(raw.get("description") or "").strip()
            elif op == "update":
                description_value = str(raw.get("description") or "").strip()
                if not description_value:
                    raise ValueError("update steps need a Leaf description")
                normalized["description"] = description_value
            normalized_steps.append(normalized)
            final_status = ("archived" if op == "archive" else
                            "completed" if op == "complete" else node.get("status"))
            if final_status in {"active", "paused"}:
                final_open.append({"id": leaf_id, "title": final_title})

        expected_ids = set(direct)
        if seen_ids != expected_ids:
            missing = sorted(expected_ids - seen_ids)
            raise ValueError(
                "every non-archived Leaf of the project, including those "
                "under its Stages, must appear exactly once"
                + (f" (missing: {', '.join(map(str, missing))})" if missing else ""))
        if len(final_open) > limit:
            raise LeafHorizonError(
                "the replan ends above the open Leaf horizon",
                code="horizon_full")
        checked: list[dict] = []
        for candidate in final_open:
            reject_overlap(str(candidate["title"]), checked)
            checked.append(candidate)
        return {
            "project_id": project_id,
            "project_update": update,
            "steps": normalized_steps,
            "final_open": final_open,
            "horizon": limit,
        }

    def apply_replan_project(
            self, project_id: int, steps: Iterable[Mapping[str, Any]], *,
            project_update: Mapping[str, Any] | None = None,
            expected_versions: Mapping[str | int, str] | None = None,
            horizon: int = 2,
            origin: Mapping[str, Any] | None = None) -> dict:
        """Revalidate and apply a complete Leaf replan as one transaction."""
        savepoint = "faerie_leaf_replan"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            plan = self.validate_replan_project(
                project_id, steps, project_update=project_update,
                expected_versions=expected_versions, horizon=horizon)
            project_id = int(plan["project_id"])
            if plan["project_update"]:
                self.update(project_id, _commit=False, **plan["project_update"])
            ordered: list[int] = []
            created: list[int] = []
            counts = {name: 0 for name in (
                "create", "rename", "update", "archive", "keep", "complete")}
            origin_values = dict(origin or {})
            for step in plan["steps"]:
                op = step["op"]
                if op == "create":
                    new_id = self.create(
                        "task", step["title"], parent_id=project_id,
                        description=step.get("description", ""),
                        priority=step.get("priority", "normal"),
                        due_date=step.get("due_date"), _commit=False)
                    if origin is not None:
                        self.set_origin(
                            new_id,
                            source_kind=str(origin_values.get("source_kind") or "ai"),
                            source_id=origin_values.get("source_id"),
                            source_proposal_id=origin_values.get("source_proposal_id"),
                            source_label=str(origin_values.get("source_label") or step["title"]),
                            summary=str(origin_values.get("summary") or
                                        step.get("description") or ""),
                            detail=str(origin_values.get("detail") or ""),
                            _commit=False)
                    ordered.append(new_id)
                    created.append(new_id)
                    counts["create"] += 1
                    continue
                leaf_id = int(step["leaf_id"])
                node = self.get(leaf_id)
                if op == "archive":
                    self.delete_subtree(leaf_id, _commit=False)
                    counts["archive"] += 1
                    continue
                # The applied plan is flat: a Leaf kept from under a Stage
                # becomes a direct ordered step of the project.
                if node and int(node.get("parent_id") or 0) != project_id:
                    self.conn.execute(
                        "UPDATE goal_node SET parent_id=?,updated_at=? WHERE id=?",
                        (project_id, _now(), leaf_id))
                    self.conn.execute(
                        "INSERT INTO goal_restructure_history "
                        "(goal_id,proposal_id,old_parent_id,new_parent_id,"
                        "old_node_type,new_node_type,retained_counts_json,"
                        "rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (leaf_id, None, int(node.get("parent_id") or 0),
                         project_id, "task", "task",
                         json.dumps({}, sort_keys=True),
                         crypto.enc("replan pulled this Leaf up to a direct "
                                    "project step"), _now()))
                if op == "complete":
                    if node and node.get("status") != "completed":
                        self.update(leaf_id, status="completed", _commit=False)
                        counts["complete"] += 1
                    ordered.append(leaf_id)
                    continue
                changes: dict[str, Any] = {}
                if op == "rename":
                    if node and step.get("new_title") != node.get("title"):
                        changes["title"] = step["new_title"]
                    if step.get("description"):
                        changes["description"] = step["description"]
                elif op == "update":
                    changes["description"] = step["description"]
                if changes:
                    self.update(leaf_id, _commit=False, **changes)
                    counts[op] += 1
                elif op == "keep":
                    counts["keep"] += 1
                ordered.append(leaf_id)

            # A Stage whose every Leaf the plan archived or pulled up no
            # longer groups anything; archive it (reversible) so the applied
            # plan is genuinely flat. Deepest first so nested legacy Stages
            # empty outward.
            archived_stages: list[int] = []
            _leaves, stages = self._project_subtree_plan_nodes(project_id)
            for stage in reversed(stages):
                stage_id = int(stage["id"])
                remaining = self.conn.execute(
                    "SELECT COUNT(*) FROM goal_node WHERE parent_id=? "
                    "AND status!='archived'", (stage_id,)).fetchone()[0]
                if not int(remaining):
                    self.delete_subtree(stage_id, _commit=False)
                    archived_stages.append(stage_id)
            tail = [int(row["id"]) for row in self.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=? "
                + (f"AND id NOT IN ({','.join('?' for _ in ordered)}) " if ordered else "")
                + "ORDER BY position,id",
                ([project_id, *ordered] if ordered else [project_id])).fetchall()]
            for position, node_id in enumerate([*ordered, *tail]):
                self.conn.execute(
                    "UPDATE goal_node SET position=?,updated_at=? WHERE id=?",
                    (position, _now(), node_id))
            self._mark_goal_ai_dirty(project_id, *ordered, *created,
                                     *archived_stages)
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        open_leaf_ids = [int(row["id"]) for row in self.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND node_type='task' "
            "AND status IN ('active','paused') ORDER BY position,id",
            (int(project_id),)).fetchall()]
        return {
            "project": self.get(int(project_id)),
            "ordered_leaf_ids": ordered,
            "ordered_leaf_count": len(ordered),
            "open_leaf_ids": open_leaf_ids,
            "created_leaf_ids": created,
            "operation_counts": counts,
            "archived_stage_ids": archived_stages,
            "archived_stage_count": len(archived_stages),
        }

    def starter_root_catalog(self, language: str = "en") -> list[dict]:
        language = "ko" if str(language).lower().startswith("ko") else "en"
        roots = [self._row(row) for row in self.conn.execute(
            "SELECT * FROM goal_node WHERE node_type='overgoal' AND status!='archived' "
            "ORDER BY position,id")]
        origin_rows = self.conn.execute(
            "SELECT o.goal_id,o.source_id FROM goal_origin o JOIN goal_node g ON g.id=o.goal_id "
            "WHERE o.source_kind='starter_root' AND g.node_type='overgoal' "
            "AND g.status!='archived'"
        ).fetchall()
        by_source = {str(row["source_id"]): int(row["goal_id"]) for row in origin_rows}
        result = []
        for spec in STARTER_ROOTS:
            matched_id = by_source.get(spec["key"])
            if not matched_id:
                for root in roots:
                    text = f"{root['title']} {root.get('description', '')}".casefold()
                    words = set(re.findall(r"[\w]+", text))
                    if words & set(spec["keywords"]):
                        matched_id = int(root["id"])
                        break
            result.append({
                "key": spec["key"], "title": spec[f"title_{language}"],
                "description": spec[f"description_{language}"],
                "active": bool(matched_id), "root_id": matched_id,
            })
        return result

    def apply_starter_roots(self, keys: list[str], language: str = "en") -> dict:
        selected = list(dict.fromkeys(str(key or "").strip() for key in keys))[:len(STARTER_ROOTS)]
        specs = {spec["key"]: spec for spec in STARTER_ROOTS}
        if not selected or any(key not in specs for key in selected):
            raise ValueError("choose one or more valid starter Roots")
        language = "ko" if str(language).lower().startswith("ko") else "en"
        existing = {item["key"]: item for item in self.starter_root_catalog(language)}
        created: list[int] = []
        now = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for key in selected:
                if existing[key]["active"]:
                    continue
                spec = specs[key]
                title = spec[f"title_{language}"]
                description = spec[f"description_{language}"]
                goal_id = self.create(
                    "overgoal", title, parent_id=self.root_id,
                    description=description, _commit=False)
                self.conn.execute(
                    "INSERT OR REPLACE INTO goal_origin "
                    "(goal_id,source_kind,source_id,source_proposal_id,source_label,summary,detail,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (goal_id, "starter_root", key, None, crypto.enc(title),
                     crypto.enc("Selected from the optional starter Root catalog."),
                     crypto.enc(description), now))
                created.append(goal_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"created_goal_ids": created, "starters": self.starter_root_catalog(language),
                "tree": self.tree()}

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

    def create_ai_leaf(
            self, title: str, *, parent_id: int, description: str = "",
            priority: str = "normal", due_date: str | None = None,
            status: str = "active",
            reservations: Iterable[Mapping[str, Any]] | None = None,
            horizon: int = 2,
            origin: Mapping[str, Any] | None = None) -> int:
        """Atomically revalidate and create one approval-gated AI Leaf."""
        if status not in {"active", "paused"}:
            raise ValueError("AI-created horizon Leaves must be active or paused")
        savepoint = "faerie_create_ai_leaf"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.validate_leaf_candidate(
                parent_id, title, description=description,
                reservations=reservations, horizon=horizon)
            new_id = self.create(
                "task", title, parent_id=int(parent_id), description=description,
                priority=priority, due_date=due_date, status=status, _commit=False)
            if origin is not None:
                values = dict(origin)
                self.set_origin(
                    new_id,
                    source_kind=str(values.get("source_kind") or "ai"),
                    source_id=values.get("source_id"),
                    source_proposal_id=values.get("source_proposal_id"),
                    source_label=str(values.get("source_label") or title),
                    summary=str(values.get("summary") or description),
                    detail=str(values.get("detail") or ""),
                    _commit=False)
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return new_id
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def update(self, goal_id: int, *, _commit: bool = True, **changes) -> dict:
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
        if changes.get("status") not in (None, "active"):
            self.conn.execute(
                "DELETE FROM goal_project_signal WHERE goal_id=?", (int(goal_id),))
        self._mark_goal_ai_dirty(int(goal_id))
        if _commit:
            self.conn.commit()
        return self.get(goal_id)  # type: ignore[return-value]

    def move(self, goal_id: int, parent_id: int, position: int | None = None,
             *, _commit: bool = True) -> None:
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
        if _commit:
            self.conn.commit()

    def _subtree_ids(self, goal_id: int) -> list[int]:
        ids, pending = [int(goal_id)], [int(goal_id)]
        while pending:
            placeholders = ",".join("?" for _ in pending)
            children = [int(row["id"]) for row in self.conn.execute(
                f"SELECT id FROM goal_node WHERE parent_id IN ({placeholders})",
                pending).fetchall()]
            ids.extend(children)
            pending = children
        return ids

    def _restructure_retained_counts(self, scope_ids: list[int]) -> dict:
        tables = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        placeholders = ",".join("?" for _ in scope_ids)

        def count(table: str, column: str, *, extra: str = "", args: tuple = ()) -> int:
            if table not in tables:
                return 0
            return int(self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders}){extra}",
                [*scope_ids, *args]).fetchone()[0])

        completed = int(self.conn.execute(
            f"SELECT COUNT(*) FROM goal_node WHERE id IN ({placeholders}) AND status='completed'",
            scope_ids).fetchone()[0])
        return {
            "nodes": len(scope_ids), "descendants": max(0, len(scope_ids) - 1),
            "completed_nodes": completed,
            "investigation_links": count("goal_curiosity_link", "goal_id"),
            "evidence_links": count("goal_evidence_link", "goal_id"),
            "outcomes": count("experiment_outcome", "goal_id"),
            "origins": count("goal_origin", "goal_id"),
            "coaching_messages": count("goal_step_coach_message", "node_id"),
            "coached_steps": count("goal_step_coach_state", "node_id"),
            "agent_messages": count("goal_agent_message", "node_id"),
            "agent_assessments": count("goal_agent_assessment", "node_id"),
            "agent_questions": count("goal_agent_question", "node_id"),
            "memory_candidates": count("goal_agent_memory_candidate", "node_id"),
            "relevance_reviews": count("goal_relevance_review", "node_id"),
            "mastery_profiles": count(
                "mastery_subject_profile", "subject_id",
                extra=" AND subject_type=?", args=("goal",)),
            "mastery_events": count(
                "mastery_subject_event", "subject_id",
                extra=" AND subject_type=?", args=("goal",)),
            "implemented_suggestions": count("curiosity_item", "implementation_goal_id"),
            "committed_plans": count("goal_plan_session", "committed_goal_id"),
        }

    def restructure_preview(self, goal_id: int, new_type: str, parent_id: int,
                            position: int | None = None,
                            semantic_role: str | None = None, *,
                            nested_stage_justification: str = "") -> dict:
        node = self.get(int(goal_id))
        if not node or node["type"] == "umbrella" or node["status"] == "archived":
            raise ValueError("an active Root, Branch, or Leaf is required")
        new_type = str(new_type or "").strip().lower()
        if new_type not in {"overgoal", "subgoal", "task"}:
            raise ValueError("new type must be Root, Branch, or Leaf")
        requested_role = str(semantic_role or "").strip().lower() or None
        if requested_role and (new_type != "subgoal" or
                               requested_role not in {"area", "project", "stage"}):
            raise ValueError("Area, Project, and Stage roles require a Branch")
        current_role_row = self.semantic_role(int(goal_id)) if node["type"] == "subgoal" else None
        current_role = current_role_row["role"] if current_role_row else None
        proposed_role = (requested_role if requested_role is not None else
                         (current_role if new_type == "subgoal" else None))
        parent = self.get(int(parent_id))
        if not parent or parent["status"] == "archived":
            raise ValueError("active destination parent not found")
        self._validate_parent(new_type, int(parent_id), int(goal_id))
        self._validate_semantic_placement(
            new_type, proposed_role, int(parent_id),
            nested_stage_justification=nested_stage_justification)
        child_types = [row["node_type"] for row in self.conn.execute(
            "SELECT node_type FROM goal_node WHERE parent_id=? AND status!='archived'",
            (int(goal_id),)).fetchall()]
        allowed_children = {
            "overgoal": {"subgoal", "task"},
            "subgoal": {"subgoal", "task"},
            "task": set(),
        }[new_type]
        invalid_children = [kind for kind in child_types if kind not in allowed_children]
        if invalid_children:
            raise ValueError("the selected type cannot contain the node's current children")
        role_changed = proposed_role != current_role
        if (node["type"] == new_type and
                int(node["parent_id"] or 0) == int(parent_id) and not role_changed):
            requested_position = node["position"] if position is None else max(0, int(position))
            if requested_position == int(node["position"]):
                raise ValueError("the proposed structure is unchanged")
        siblings = self.conn.execute(
            "SELECT COUNT(*) FROM goal_node WHERE parent_id=? AND id!=?",
            (int(parent_id), int(goal_id))).fetchone()[0]
        proposed_position = min(int(siblings), max(0, int(position))) if position is not None else (
            int(node["position"]) if int(node["parent_id"] or 0) == int(parent_id) else int(siblings))
        scope_ids = self._subtree_ids(int(goal_id))
        labels = {"umbrella": "Soul", "overgoal": "Root",
                  "subgoal": "Branch", "task": "Leaf"}
        # The stored "area" role is shown to the user as "Branch".
        role_labels = {"area": "Branch", "project": "Project", "stage": "Stage"}
        current_path = " › ".join(self._goal_path_titles(int(goal_id)))
        proposed_path = " › ".join([*self._goal_path_titles(int(parent_id)), node["title"]])
        return {
            "goal_id": int(goal_id), "node_id_preserved": True,
            "current": {"type": node["type"],
                        "type_label": role_labels.get(current_role, labels[node["type"]]),
                        "semantic_role": current_role,
                        "parent_id": node["parent_id"], "path": current_path,
                        "position": int(node["position"])},
            "proposed": {"type": new_type,
                         "type_label": role_labels.get(proposed_role, labels[new_type]),
                         "semantic_role": proposed_role,
                         "parent_id": int(parent_id), "parent_title": parent["title"],
                         "path": proposed_path, "position": proposed_position},
            "retained_counts": self._restructure_retained_counts(scope_ids),
            "affected_node_ids": scope_ids,
        }

    def restructure(self, goal_id: int, new_type: str, parent_id: int,
                    position: int | None = None, *, semantic_role: str | None = None,
                    proposal_id: int | None = None,
                    rationale: str = "", commit: bool = True) -> dict:
        preview = self.restructure_preview(
            goal_id, new_type, parent_id, position, semantic_role,
            nested_stage_justification=rationale)
        old_parent = int(preview["current"]["parent_id"])
        new_parent = int(preview["proposed"]["parent_id"])
        insert_at = int(preview["proposed"]["position"])
        now = _now()
        started = False
        try:
            if commit and not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
                started = True
            self.conn.execute(
                "UPDATE goal_node SET parent_id=?,node_type=?,position=?,updated_at=? WHERE id=?",
                (new_parent, str(new_type), insert_at, now, int(goal_id)))
            proposed_role = preview["proposed"].get("semantic_role")
            if (str(new_type) == "subgoal" and proposed_role and
                    (preview["current"].get("semantic_role") != proposed_role or
                     preview["current"].get("type") != "subgoal")):
                self._set_semantic_role(
                    int(goal_id), proposed_role, rationale=str(rationale or ""),
                    source="manual", commit=False)
            elif str(new_type) != "subgoal":
                self.conn.execute(
                    "DELETE FROM goal_semantic_role WHERE goal_id=?", (int(goal_id),))
                self.conn.execute(
                    "DELETE FROM goal_project_signal WHERE goal_id=?", (int(goal_id),))
            new_siblings = [int(row["id"]) for row in self.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=? AND id!=? ORDER BY position,id",
                (new_parent, int(goal_id))).fetchall()]
            new_siblings.insert(min(len(new_siblings), insert_at), int(goal_id))
            for index, sibling_id in enumerate(new_siblings):
                self.conn.execute("UPDATE goal_node SET position=? WHERE id=?", (index, sibling_id))
            if old_parent != new_parent:
                old_siblings = self.conn.execute(
                    "SELECT id FROM goal_node WHERE parent_id=? ORDER BY position,id",
                    (old_parent,)).fetchall()
                for index, sibling in enumerate(old_siblings):
                    self.conn.execute("UPDATE goal_node SET position=? WHERE id=?",
                                      (index, sibling["id"]))
            self.conn.execute(
                "INSERT INTO goal_restructure_history "
                "(goal_id,proposal_id,old_parent_id,new_parent_id,old_node_type,new_node_type,"
                "retained_counts_json,rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(goal_id), proposal_id, old_parent, new_parent,
                 preview["current"]["type"], str(new_type),
                 json.dumps(preview["retained_counts"], sort_keys=True),
                 crypto.enc(str(rationale or "")), now))
            self._mark_goal_ai_dirty(*preview["affected_node_ids"], old_parent, new_parent)
            if commit:
                self.conn.commit()
        except Exception:
            if commit and (started or self.conn.in_transaction):
                self.conn.rollback()
            raise
        result = dict(preview)
        result["history_id"] = int(self.conn.execute(
            "SELECT id FROM goal_restructure_history WHERE goal_id=? ORDER BY id DESC LIMIT 1",
            (int(goal_id),)).fetchone()[0])
        return result

    def restructure_batch_preview(self, changes: list[dict],
                                  role_updates: list[dict] | None = None) -> dict:
        """Validate a whole-path normalization against the final graph.

        This intentionally keeps the four structural node types. Area, Project,
        and Stage are presentation roles attached only to Branches.
        """
        changes = list(changes or [])[:24]
        role_updates = list(role_updates or [])[:48]
        rows = [self._row(row) for row in self.conn.execute(
            "SELECT * FROM goal_node WHERE status!='archived' ORDER BY id")]
        nodes = {int(node["id"]): node for node in rows if node}
        final = {node_id: {"type": node["type"], "parent_id": node["parent_id"],
                           "position": int(node["position"])}
                 for node_id, node in nodes.items()}
        normalized: list[dict] = []
        seen: set[int] = set()
        for raw in changes:
            try:
                goal_id = int(raw.get("goal_id"))
                parent_id = int(raw.get("parent_id"))
            except (TypeError, ValueError):
                raise ValueError("every restructure change needs valid node and parent ids")
            if goal_id in seen:
                raise ValueError("a node can appear only once in a restructure plan")
            seen.add(goal_id)
            node = nodes.get(goal_id)
            parent = nodes.get(parent_id)
            new_type = str(raw.get("new_type") or "").strip().lower()
            if not node or node["type"] == "umbrella" or not parent:
                raise ValueError("restructure plan contains an unavailable node")
            if new_type not in {"overgoal", "subgoal", "task"}:
                raise ValueError("new type must be Root, Branch, or Leaf")
            position = raw.get("position")
            position = (None if position is None else max(0, int(position)))
            final[goal_id] = {"type": new_type, "parent_id": parent_id,
                              "position": node["position"] if position is None else position}
            if new_type != node["type"] or parent_id != int(node["parent_id"] or 0):
                normalized.append({"goal_id": goal_id, "new_type": new_type,
                                   "parent_id": parent_id, "position": position,
                                   "reason": str(raw.get("reason") or "")[:700]})

        allowed_parents = {
            "umbrella": set(), "overgoal": {"umbrella"},
            "subgoal": {"overgoal", "subgoal"},
            "task": {"overgoal", "subgoal"},
        }
        for goal_id, state in final.items():
            if state["type"] == "umbrella":
                if state["parent_id"] is not None:
                    raise ValueError("the Soul cannot have a parent")
                continue
            parent = final.get(int(state["parent_id"] or 0))
            if not parent or parent["type"] not in allowed_parents[state["type"]]:
                raise ValueError("the proposed tree contains an invalid parent relationship")
            visited, current = {goal_id}, int(state["parent_id"] or 0)
            while current:
                if current in visited:
                    raise ValueError("the proposed tree contains a cycle")
                visited.add(current)
                current = int(final[current]["parent_id"] or 0) if current in final else 0

        existing_roles = {int(row["goal_id"]): row["role"] for row in self.conn.execute(
            "SELECT goal_id,role FROM goal_semantic_role")}
        display_roles: dict[int, str] = {}
        children_by_parent: dict[int, list[int]] = {}
        for child_id, child in nodes.items():
            children_by_parent.setdefault(int(child.get("parent_id") or 0), []).append(child_id)

        def derive_roles(goal_id: int, parent_role: str = "") -> None:
            node = nodes[goal_id]
            next_parent_role = parent_role
            if node["type"] == "subgoal":
                role = existing_roles.get(goal_id)
                if not role:
                    parent = nodes.get(int(node.get("parent_id") or 0))
                    role = _derived_branch_role(
                        node.get("title", ""), node.get("description", ""),
                        parent_type=str(parent.get("type") if parent else ""),
                        parent_role=parent_role,
                        has_branch_children=any(
                            nodes[child]["type"] == "subgoal"
                            for child in children_by_parent.get(goal_id, [])))
                display_roles[goal_id] = role
                next_parent_role = role
            for child_id in children_by_parent.get(goal_id, []):
                derive_roles(child_id, next_parent_role)

        soul_id = next((node_id for node_id, node in nodes.items()
                        if node["type"] == "umbrella"), 0)
        if soul_id:
            derive_roles(soul_id)
        normalized_roles: list[dict] = []
        role_seen: set[int] = set()
        role_inputs: dict[int, dict] = {}
        for raw in role_updates:
            try:
                goal_id = int(raw.get("goal_id"))
            except (TypeError, ValueError):
                raise ValueError("every Branch role needs a valid node id")
            role = str(raw.get("role") or "").strip().lower()
            if goal_id in role_seen:
                raise ValueError("a Branch can receive only one semantic role")
            role_seen.add(goal_id)
            if goal_id not in final or final[goal_id]["type"] != "subgoal":
                raise ValueError("Area, Project, and Stage roles apply only to Branches")
            if role not in {"area", "project", "stage"}:
                raise ValueError("Branch role must be Area, Project, or Stage")
            nested_justification = str(
                raw.get("nested_stage_justification") or "")[:700]
            role_inputs[goal_id] = {
                "role": role, "nested_stage_justification": nested_justification}
            if existing_roles.get(goal_id) != role or nested_justification:
                normalized_roles.append({"goal_id": goal_id, "role": role,
                                         "reason": str(raw.get("reason") or "")[:700],
                                         "nested_stage_justification": nested_justification})

        final_roles = dict(display_roles)
        final_roles.update({goal_id: item["role"] for goal_id, item in role_inputs.items()})
        final_children: dict[int, list[int]] = {}
        for child_id, state in final.items():
            final_children.setdefault(int(state.get("parent_id") or 0), []).append(child_id)
        for goal_id, role_input in role_inputs.items():
            role = role_input["role"]
            parent_id = int(final[goal_id].get("parent_id") or 0)
            parent = final.get(parent_id, {})
            parent_role = final_roles.get(parent_id)
            if role == "area" and not (
                    parent.get("type") == "overgoal" or parent_role == "area"):
                raise ValueError("an Area must live beneath a Root or another Area")
            if role == "project" and not (
                    parent.get("type") == "overgoal" or parent_role == "area"):
                raise ValueError("a Project must live beneath a Root or Area")
            if role == "stage":
                if parent_role != "project":
                    raise ValueError("a Stage must live directly beneath a Project")
                if not any(final[child_id]["type"] == "task"
                           for child_id in final_children.get(goal_id, [])):
                    raise ValueError(
                        "a Stage must group at least one concrete Leaf; terminal work is a Leaf")
        if not normalized and not normalized_roles:
            raise ValueError("the proposed structure is unchanged")

        def path_for(goal_id: int, graph: dict[int, dict]) -> str:
            titles, seen_ids, current = [], set(), int(goal_id)
            while current and current not in seen_ids and current in nodes:
                seen_ids.add(current)
                titles.append(nodes[current]["title"])
                current = int(graph[current]["parent_id"] or 0)
            return " › ".join(reversed(titles))

        labels = {"umbrella": "Soul", "overgoal": "Root",
                  "subgoal": "Branch", "task": "Leaf"}
        structural = []
        for item in normalized:
            goal_id = item["goal_id"]
            node = nodes[goal_id]
            structural.append({
                **item, "title": node["title"],
                "current": {"type": node["type"], "type_label": labels[node["type"]],
                            "parent_id": node["parent_id"],
                            "path": " › ".join(self._goal_path_titles(goal_id))},
                "proposed": {"type": item["new_type"],
                             "type_label": labels[item["new_type"]],
                             "parent_id": item["parent_id"],
                             "path": path_for(goal_id, final)},
            })
        roles = [{**item, "title": nodes[item["goal_id"]]["title"],
                  "current_role": display_roles.get(item["goal_id"]),
                  "proposed_role": item["role"]} for item in normalized_roles]
        scope_ids: set[int] = set()
        for goal_id in {item["goal_id"] for item in [*normalized, *normalized_roles]}:
            scope_ids.update(self._subtree_ids(goal_id))
        return {
            "structural_changes": structural, "role_changes": roles,
            "retained_counts": self._restructure_retained_counts(sorted(scope_ids)),
            "affected_node_ids": sorted(scope_ids), "node_ids_preserved": True,
        }

    def restructure_batch(self, changes: list[dict], role_updates: list[dict] | None = None,
                          *, proposal_id: int | None = None, rationale: str = "",
                          commit: bool = True) -> dict:
        preview = self.restructure_batch_preview(changes, role_updates)
        structural = preview["structural_changes"]
        roles = preview["role_changes"]
        old_parents = {int(item["goal_id"]): int(item["current"]["parent_id"] or 0)
                       for item in structural}
        touched_parents = {int(item["proposed"]["parent_id"]) for item in structural}
        touched_parents.update(parent for parent in old_parents.values() if parent)
        now = _now()
        started = False
        try:
            if commit and not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
                started = True
            for item in structural:
                position = item["position"]
                if position is None:
                    position = int(self.conn.execute(
                        "SELECT COALESCE(MAX(position),-1)+1 FROM goal_node WHERE parent_id=?",
                        (int(item["parent_id"]),)).fetchone()[0])
                self.conn.execute(
                    "UPDATE goal_node SET parent_id=?,node_type=?,position=?,updated_at=? WHERE id=?",
                    (int(item["parent_id"]), item["new_type"], int(position), now,
                     int(item["goal_id"])))
                if item["new_type"] != "subgoal":
                    self.conn.execute("DELETE FROM goal_semantic_role WHERE goal_id=?",
                                      (int(item["goal_id"]),))
                    self.conn.execute("DELETE FROM goal_project_signal WHERE goal_id=?",
                                      (int(item["goal_id"]),))
                self.conn.execute(
                    "INSERT INTO goal_restructure_history "
                    "(goal_id,proposal_id,old_parent_id,new_parent_id,old_node_type,new_node_type,"
                    "retained_counts_json,rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(item["goal_id"]), proposal_id, int(item["current"]["parent_id"]),
                     int(item["proposed"]["parent_id"]), item["current"]["type"],
                     item["new_type"], json.dumps(preview["retained_counts"], sort_keys=True),
                     crypto.enc(item.get("reason") or rationale), now))
            for parent_id in touched_parents:
                siblings = self.conn.execute(
                    "SELECT id FROM goal_node WHERE parent_id=? ORDER BY position,id",
                    (parent_id,)).fetchall()
                for index, sibling in enumerate(siblings):
                    self.conn.execute("UPDATE goal_node SET position=? WHERE id=?",
                                      (index, int(sibling["id"])))
            for item in roles:
                self._set_semantic_role(
                    int(item["goal_id"]), item["proposed_role"],
                    rationale=(item.get("nested_stage_justification") or
                               item.get("reason") or rationale),
                    source="approved_restructure", proposal_id=proposal_id,
                    commit=False)
            self._mark_goal_ai_dirty(*preview["affected_node_ids"], *touched_parents)
            if commit:
                self.conn.commit()
        except Exception:
            if commit and (started or self.conn.in_transaction):
                self.conn.rollback()
            raise
        return preview

    def validate_merge_projects(self, source_id: int, target_id: int) -> dict:
        """Validate that the target Project can absorb the source Project."""
        try:
            source_id = int(source_id)
            target_id = int(target_id)
        except (TypeError, ValueError):
            raise ValueError("merging needs valid source and target Projects")
        if source_id == target_id:
            raise ValueError("a Project cannot be merged into itself")
        source = self.get(source_id)
        target = self.get(target_id)
        for name, node in (("absorbed", source), ("surviving", target)):
            if not node or node.get("status") == "archived":
                raise ValueError(f"the {name} Project is missing or archived")
            if (node["type"] != "subgoal"
                    or self.resolved_semantic_role(int(node["id"])) != "project"):
                raise ValueError(
                    f"the {name} node is not a Project — only two Projects "
                    "can be merged")
        if (target_id in self._subtree_ids(source_id)
                or source_id in self._subtree_ids(target_id)):
            raise ValueError("one of those Projects contains the other")
        return {"source": source, "target": target}

    def merge_projects(self, source_id: int, target_id: int, *,
                       proposal_id: int | None = None,
                       rationale: str = "") -> dict:
        """Fold one Project into another as a structural change.

        Every non-archived child (Leaves, Stages, completed work) moves to the
        end of the surviving Project's plan in its original order, and the
        emptied source is soft-archived (reversible via restore_subtree). Like
        restructure_batch, this does not enforce the open-Leaf horizon: the
        merged plan may temporarily exceed it and is trimmed by a later replan.
        """
        checked = self.validate_merge_projects(source_id, target_id)
        source, target = checked["source"], checked["target"]
        children = [self._row(row) for row in self.conn.execute(
            "SELECT * FROM goal_node WHERE parent_id=? AND status!='archived' "
            "ORDER BY position,id", (int(source["id"]),)).fetchall()]
        moved_scope: list[int] = []
        for child in children:
            moved_scope.extend(self._subtree_ids(int(child["id"])))
        retained = self._restructure_retained_counts(sorted(set(moved_scope)))
        now = _now()
        started = False
        try:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
                started = True
            base = int(self.conn.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM goal_node WHERE parent_id=?",
                (int(target["id"]),)).fetchone()[0])
            for offset, child in enumerate(children):
                self.conn.execute(
                    "UPDATE goal_node SET parent_id=?,position=?,updated_at=? "
                    "WHERE id=?",
                    (int(target["id"]), base + offset, now, int(child["id"])))
                self.conn.execute(
                    "INSERT INTO goal_restructure_history "
                    "(goal_id,proposal_id,old_parent_id,new_parent_id,"
                    "old_node_type,new_node_type,retained_counts_json,"
                    "rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(child["id"]), proposal_id, int(source["id"]),
                     int(target["id"]), child["type"], child["type"],
                     json.dumps(retained, sort_keys=True),
                     crypto.enc(rationale), now))
            archived = self.delete_subtree(int(source["id"]), _commit=False)
            self._mark_goal_ai_dirty(
                int(source["id"]), int(target["id"]),
                *[int(child["id"]) for child in children])
            self.conn.commit()
        except Exception:
            if started or self.conn.in_transaction:
                self.conn.rollback()
            raise
        return {"moved": len(children), "archived": archived,
                "source": source, "target": target,
                "retained_counts": retained}

    def delete_subtree(self, goal_id: int, *, _commit: bool = True) -> int:
        """'Delete' a node and everything under it. Always a soft archive
        (status='archived'), never a hard row delete — reversible, and
        consistent with how catalog()/tree() already hide archived nodes.
        Returns the count of nodes archived."""
        node = self.get(goal_id)
        if not node:
            raise ValueError("goal not found")
        if node["type"] == "umbrella":
            raise ValueError("the umbrella cannot be archived")
        if node["status"] == "archived":
            raise ValueError("this node is already archived")
        to_visit = [int(goal_id)]
        ids: list[int] = []
        while to_visit:
            current = to_visit.pop()
            ids.append(current)
            rows = self.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=?", (current,)).fetchall()
            to_visit.extend(int(r["id"]) for r in rows)
        rows = self.conn.execute(
            f"SELECT id,status FROM goal_node WHERE id IN ({','.join('?' for _ in ids)})",
            ids).fetchall()
        now = _now()
        started = False
        try:
            if _commit and not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
                started = True
            self.conn.execute(
                "DELETE FROM goal_archive_snapshot WHERE archive_root_id=?", (int(goal_id),))
            self.conn.executemany(
                "INSERT INTO goal_archive_snapshot "
                "(archive_root_id,goal_id,prior_status,archived_at) VALUES (?,?,?,?)",
                [(int(goal_id), int(row["id"]), row["status"], now) for row in rows])
            self.conn.executemany(
                "UPDATE goal_node SET status='archived',updated_at=? WHERE id=?",
                [(now, node_id) for node_id in ids])
            self.conn.execute(
                f"DELETE FROM goal_project_signal WHERE goal_id IN "
                f"({','.join('?' for _ in ids)})", ids)
            self._mark_goal_ai_dirty(*ids, node.get("parent_id"))
            if _commit:
                self.conn.commit()
        except Exception:
            if _commit and (started or self.conn.in_transaction):
                self.conn.rollback()
            raise
        return len(ids)

    def restore_subtree(self, goal_id: int) -> int:
        """Restore a soft-archived subtree to the exact statuses it had before archive."""
        node = self.get(int(goal_id))
        if not node or node["type"] == "umbrella":
            raise ValueError("archived node not found")
        if node["status"] != "archived":
            raise ValueError("this node is not archived")
        parent = self.get(int(node["parent_id"])) if node.get("parent_id") else None
        if parent and parent["status"] == "archived":
            raise ValueError("restore the archived parent first")
        rows = self.conn.execute(
            "SELECT goal_id,prior_status FROM goal_archive_snapshot "
            "WHERE archive_root_id=? ORDER BY goal_id", (int(goal_id),)).fetchall()
        now = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if rows:
                self.conn.executemany(
                    "UPDATE goal_node SET status=?,updated_at=? WHERE id=?",
                    [(row["prior_status"], now, int(row["goal_id"])) for row in rows])
                restored_ids = [int(row["goal_id"]) for row in rows]
            else:
                # Compatibility for nodes archived before snapshots were introduced.
                self.conn.execute(
                    "UPDATE goal_node SET status='active',updated_at=? WHERE id=?",
                    (now, int(goal_id)))
                restored_ids = [int(goal_id)]
            self.conn.execute(
                "DELETE FROM goal_archive_snapshot WHERE archive_root_id=?", (int(goal_id),))
            self._mark_goal_ai_dirty(*restored_ids, node.get("parent_id"))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(restored_ids)

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

    def orphaned_curiosities_for_archive(self, goal_id: int) -> list[dict]:
        """Investigations that would be left without an active home if this
        subtree were archived: linked to a node inside the subtree and to no
        non-archived node outside it. Call this BEFORE delete_subtree, while
        the subtree is still active. Returns [{"id","label","goal_id"}] where
        goal_id is the in-subtree node they were attached to (for messaging)."""
        tables = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"curiosity", "goal_curiosity_link"}.issubset(tables):
            return []
        subtree = self._subtree_ids(int(goal_id))
        placeholders = ",".join("?" for _ in subtree)
        rows = self.conn.execute(
            f"SELECT l.curiosity_id,l.goal_id,c.label FROM goal_curiosity_link l "
            f"JOIN curiosity c ON c.id=l.curiosity_id "
            f"WHERE l.goal_id IN ({placeholders}) AND c.status!='archived' "
            f"ORDER BY l.curiosity_id", subtree).fetchall()
        orphans: list[dict] = []
        seen: set[int] = set()
        for row in rows:
            cid = int(row["curiosity_id"])
            if cid in seen:
                continue
            seen.add(cid)
            survivor = self.conn.execute(
                f"SELECT 1 FROM goal_curiosity_link l JOIN goal_node g ON g.id=l.goal_id "
                f"WHERE l.curiosity_id=? AND g.status!='archived' "
                f"AND l.goal_id NOT IN ({placeholders}) LIMIT 1",
                [cid, *subtree]).fetchone()
            if survivor:
                continue
            orphans.append({"id": cid, "goal_id": int(row["goal_id"]),
                            "label": crypto.dec(row["label"]) or ""})
        return orphans

    def reroute_curiosity(self, curiosity_id: int, new_goal_id: int) -> dict:
        """Re-home an Investigation onto a new active goal node, dropping its
        links to any archived nodes so it stops dangling under a dead parent.
        Links to other active nodes are preserved."""
        target = self.get(int(new_goal_id))
        if not target or target["status"] == "archived":
            raise ValueError("the new home must be an active goal node")
        if target["type"] == "umbrella":
            raise ValueError("attach an Investigation to a Root or Branch, not the Soul")
        if not self.conn.execute(
                "SELECT 1 FROM curiosity WHERE id=?", (int(curiosity_id),)).fetchone():
            raise ValueError("curiosity not found")
        archived_links = [int(r["goal_id"]) for r in self.conn.execute(
            "SELECT l.goal_id FROM goal_curiosity_link l "
            "JOIN goal_node g ON g.id=l.goal_id "
            "WHERE l.curiosity_id=? AND g.status='archived'",
            (int(curiosity_id),)).fetchall()]
        now = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for gid in archived_links:
                self.conn.execute(
                    "DELETE FROM goal_curiosity_link WHERE goal_id=? AND curiosity_id=?",
                    (gid, int(curiosity_id)))
            self.conn.execute(
                "INSERT OR IGNORE INTO goal_curiosity_link VALUES (?,?,?)",
                (int(new_goal_id), int(curiosity_id), now))
            self._mark_goal_ai_dirty(int(new_goal_id), *archived_links)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"curiosity_id": int(curiosity_id), "new_goal_id": int(new_goal_id),
                "removed_from": archived_links}

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

    def _outcome_dict(self, row) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]), "goal_id": int(row["goal_id"]),
            "curiosity_id": row["curiosity_id"], "source_item_id": row["source_item_id"],
            "result": row["result"],
            "what_happened": crypto.dec(row["what_happened"]) or "",
            "expected_obstacle": crypto.dec(row["expected_obstacle"]) or "",
            "surprise": crypto.dec(row["surprise"]) or "",
            "helpfulness": row["helpfulness"],
            "changed_understanding": crypto.dec(row["changed_understanding"]) or "",
            "next_adjustment": crypto.dec(row["next_adjustment"]) or "",
            "created_at": row["created_at"],
        }

    def outcomes(self, goal_id: int | None = None, *,
                 curiosity_id: int | None = None, limit: int = 30) -> list[dict]:
        where, params = [], []
        if goal_id is not None:
            where.append("goal_id=?"); params.append(int(goal_id))
        if curiosity_id is not None:
            where.append("curiosity_id=?"); params.append(int(curiosity_id))
        params.append(max(1, min(200, int(limit))))
        sql = "SELECT * FROM experiment_outcome" + (
            " WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        return [self._outcome_dict(row) for row in
                self.conn.execute(sql, tuple(params)).fetchall()]

    def outcome(self, outcome_id: int) -> dict | None:
        return self._outcome_dict(self.conn.execute(
            "SELECT * FROM experiment_outcome WHERE id=?", (int(outcome_id),)).fetchone())

    def _outcome_links(self, goal_id: int) -> tuple[int | None, int | None]:
        source = self.conn.execute(
            "SELECT id,curiosity_id FROM curiosity_item WHERE implementation_goal_id=? "
            "ORDER BY id DESC LIMIT 1", (int(goal_id),)).fetchone() if self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curiosity_item'"
            ).fetchone() else None
        source_item_id = int(source["id"]) if source else None
        curiosity_id = int(source["curiosity_id"]) if source else None
        current, seen = int(goal_id), set()
        while curiosity_id is None and current and current not in seen:
            seen.add(current)
            link = self.conn.execute(
                "SELECT curiosity_id FROM goal_curiosity_link WHERE goal_id=? "
                "ORDER BY created_at DESC LIMIT 1", (current,)).fetchone()
            if link:
                curiosity_id = int(link["curiosity_id"])
                break
            parent = self.conn.execute(
                "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
            current = int(parent["parent_id"]) if parent and parent["parent_id"] else 0
        return curiosity_id, source_item_id

    def add_outcome(self, goal_id: int, result: str, what_happened: str, *,
                    expected_obstacle: str = "", surprise: str = "",
                    helpfulness: float | None = None,
                    changed_understanding: str = "", next_adjustment: str = "",
                    curiosity_id: int | None = None,
                    source_item_id: int | None = None) -> dict:
        node = self.get(goal_id)
        if not node or node["type"] != "task":
            raise ValueError("experiment outcomes belong to Leaves")
        result = str(result or "").strip().lower()
        if result not in {"completed", "attempted", "avoided", "abandoned"}:
            raise ValueError("invalid experiment outcome")
        what_happened = str(what_happened or "").strip()
        if not what_happened:
            raise ValueError("say what happened before saving the outcome")
        if helpfulness is not None:
            helpfulness = max(0.0, min(10.0, float(helpfulness)))
        inferred_curiosity, inferred_item = self._outcome_links(goal_id)
        curiosity_id = int(curiosity_id) if curiosity_id is not None else inferred_curiosity
        source_item_id = (int(source_item_id) if source_item_id is not None else inferred_item)
        if curiosity_id is not None and not self.conn.execute(
            "SELECT 1 FROM curiosity WHERE id=?", (curiosity_id,)).fetchone():
            raise ValueError("linked Investigation not found")
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO experiment_outcome "
            "(goal_id,curiosity_id,source_item_id,result,what_happened,expected_obstacle,"
            "surprise,helpfulness,changed_understanding,next_adjustment,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(goal_id), curiosity_id, source_item_id, result,
             crypto.enc(what_happened), crypto.enc(str(expected_obstacle or "").strip()),
             crypto.enc(str(surprise or "").strip()), helpfulness,
             crypto.enc(str(changed_understanding or "").strip()),
             crypto.enc(str(next_adjustment or "").strip()), now))
        outcome_id = int(cur.lastrowid)
        learning = str(changed_understanding or "").strip() or what_happened
        adjustment = str(next_adjustment or "").strip()
        label = f"{result.title()}: {learning}"
        if adjustment:
            label += f" Next adjustment: {adjustment}"
        self.conn.execute(
            "INSERT OR IGNORE INTO goal_evidence_link "
            "(goal_id,source_kind,source_id,label,created_at) VALUES (?,?,?,?,?)",
            (int(goal_id), "experiment_outcome", str(outcome_id),
             crypto.enc(label[:1000]), now))
        if result == "completed":
            self.conn.execute(
                "UPDATE goal_node SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                (now, now, int(goal_id)))
        self._mark_goal_ai_dirty(int(goal_id), node["parent_id"])
        self.conn.commit()
        return self._outcome_dict(self.conn.execute(
            "SELECT * FROM experiment_outcome WHERE id=?", (outcome_id,)).fetchone())

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
            node["outcomes"] = []
            node["origin"] = None
            node["mastery"] = self.mastery(node["id"])
            node["semantic_role"] = None
            node["semantic_role_source"] = ""
            node["project_focus"] = {
                "highest_priority": False,
                "currently_working": False,
                "auto_current": False,
            }
        for row in self.conn.execute(
                "SELECT goal_id,role,source FROM goal_semantic_role ORDER BY goal_id"):
            if row["goal_id"] in nodes:
                nodes[row["goal_id"]]["semantic_role"] = row["role"]
                nodes[row["goal_id"]]["semantic_role_source"] = row["source"]
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
        for row in self.conn.execute("SELECT * FROM experiment_outcome ORDER BY id DESC"):
            if row["goal_id"] in nodes:
                nodes[row["goal_id"]]["outcomes"].append(self._outcome_dict(row))
        for node in nodes.values():
            if node["parent_id"] in nodes:
                nodes[node["parent_id"]]["children"].append(node)

        def assign_semantic_roles(node: dict, parent_role: str = "") -> None:
            if node["type"] == "subgoal":
                if not node.get("semantic_role"):
                    parent = nodes.get(node.get("parent_id"))
                    node["semantic_role"] = _derived_branch_role(
                        node.get("title", ""), node.get("description", ""),
                        parent_type=str(parent.get("type") if parent else ""),
                        parent_role=parent_role,
                        has_branch_children=any(
                            child["type"] == "subgoal" for child in node["children"]))
                    node["semantic_role_source"] = "derived"
                parent_role = str(node["semantic_role"])
            for child in node["children"]:
                assign_semantic_roles(child, parent_role)

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
            assign_semantic_roles(root)
            signals = self.project_signals()
            for kind, goal_id in signals.items():
                if goal_id in nodes:
                    nodes[goal_id]["project_focus"][kind] = True
            effective_project = self.effective_current_project_id()
            if (signals["currently_working"] is None and effective_project in nodes):
                nodes[effective_project]["project_focus"]["auto_current"] = True
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
                "SELECT id,target_parent_id FROM goal_plan_session WHERE source_item_id=? "
                "AND status IN ('active','ready') ORDER BY id DESC LIMIT 1",
                (int(source_item_id),)).fetchone()
            if existing:
                if int(existing["target_parent_id"]) == int(target["id"]):
                    session = self.plan_session(existing["id"])
                    approved = (draft or {}).get("_placement")
                    if approved and not session["draft"].get("_placement"):
                        restored = dict(session["draft"])
                        restored["_placement"] = approved
                        self.set_plan_draft(existing["id"], restored,
                                            ready=session["status"] == "ready")
                    return self.plan_session(existing["id"])
                # A newly approved placement supersedes an earlier draft that
                # was opened under the wrong parent. Preserve its transcript as
                # abandoned history instead of silently reusing its location.
                self.conn.execute(
                    "UPDATE goal_plan_session SET status='abandoned',updated_at=? WHERE id=?",
                    (_now(), int(existing["id"])))
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
        # Placement is an approval boundary, not model-authored draft content.
        # Preserve it when the planner replies or the user edits the JSON review.
        row = self.conn.execute(
            "SELECT draft_json,target_parent_id FROM goal_plan_session WHERE id=?",
            (int(session_id),)).fetchone()
        if row:
            try:
                existing = json.loads(crypto.dec(row["draft_json"]) or "{}")
            except json.JSONDecodeError:
                existing = {}
            if existing.get("_placement"):
                draft = dict(draft or {})
                draft["_placement"] = existing["_placement"]
        if row:
            # One-Leaf model: keep only the first Leaf of each group so the
            # stored draft, its review card, and the eventual commit all agree
            # on a single NOW step per project.
            draft = self._trim_draft_leaves(
                int(row["target_parent_id"]), draft)
        if ready and row:
            self._validate_plan_draft_horizon(
                int(session_id), int(row["target_parent_id"]), draft,
                allow_incomplete=True)
        self.conn.execute(
            "UPDATE goal_plan_session SET draft_json=?,summary=COALESCE(?,summary),"
            "status=?,updated_at=? WHERE id=? AND status IN ('active','ready')",
            (crypto.enc(json.dumps(draft)), crypto.enc(summary) if summary is not None else None,
             "ready" if ready else "active", _now(), int(session_id)))
        self.conn.commit()

    def confirm_plan_placement(self, session_id: int, placement: dict) -> dict:
        """Persist the user's final placement choice after planning dialogue."""
        session = self.plan_session(int(session_id))
        if session["status"] != "ready":
            raise ValueError("summarize and review the plan before confirming placement")
        placement = dict(placement or {})
        try:
            target_parent_id = int(placement.get("target_parent_id"))
        except (TypeError, ValueError):
            raise ValueError("choose where this plan should live") from None
        target = self.get(target_parent_id)
        if (not target or target.get("status") != "active" or
                target.get("type") not in {"umbrella", "overgoal", "subgoal"}):
            raise ValueError("planning target must be an active Soul, Root, or Branch")
        if target["type"] == "umbrella":
            if (placement.get("mode") != "new_root" or
                    not placement.get("root_eligible") or
                    not str(placement.get("root_title") or "").strip() or
                    not str(placement.get("root_description") or "").strip()):
                raise ValueError("a new Root requires an approved durable life-domain placement")
        else:
            placement.update({
                "mode": "existing", "parent_id": target_parent_id,
                "parent_path": " › ".join(self._goal_path_titles(target_parent_id)),
            })
        placement["target_parent_id"] = target_parent_id
        placement["user_confirmed"] = True
        placement.pop("review_required", None)
        draft = dict(session.get("draft") or {})
        draft["_placement"] = placement
        self._validate_plan_draft_horizon(
            int(session_id), target_parent_id, draft, allow_incomplete=True)
        self.conn.execute(
            "UPDATE goal_plan_session SET target_parent_id=?,draft_json=?,updated_at=? "
            "WHERE id=? AND status='ready'",
            (target_parent_id, crypto.enc(json.dumps(draft)), _now(), int(session_id)))
        self.conn.commit()
        return self.plan_session(int(session_id))

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

    @staticmethod
    def _draft_effective_node_type(raw: Mapping[str, Any], parent_type: str) -> str:
        requested = str(raw.get("type") or "").strip().lower()
        children = list(raw.get("children") or [])
        if parent_type == "umbrella":
            return "overgoal"
        if requested == "task" and not children:
            return "task"
        if requested in {"subgoal", "task", "overgoal", "umbrella"} or children:
            return "subgoal"
        return "task"

    def _trim_draft_leaves(self, target_parent_id: int, draft: Mapping[str, Any],
                           *, horizon: int = PLAN_LEAF_HORIZON) -> dict:
        """Return a copy of the plan draft with at most `horizon` Leaf (task)
        nodes per sibling group; extra Leaves are dropped. One-Leaf model: the
        next Leaf is created just-in-time from the main chat after the current
        one completes, so a project never commits more than its open-Leaf
        horizon. Uses the same leaf-counting rule as _validate_plan_draft_horizon
        so validation can never see an over-horizon group afterward."""
        target = self.get(int(target_parent_id))
        target_type = str(target.get("type") or "") if target else ""

        def trim_group(nodes, parent_type: str) -> list:
            kept: list = []
            leaves = 0
            for raw in list(nodes or []):
                if not isinstance(raw, Mapping):
                    continue
                node = dict(raw)
                if self._draft_effective_node_type(raw, parent_type) == "task":
                    if leaves >= horizon:
                        continue
                    leaves += 1
                else:
                    node["children"] = trim_group(
                        raw.get("children") or [],
                        self._draft_effective_node_type(raw, parent_type))
                kept.append(node)
            return kept

        trimmed = dict(draft or {})
        trimmed["nodes"] = trim_group(trimmed.get("nodes") or [], target_type)
        return trimmed

    def _validate_plan_semantics(self, target_parent_id: int,
                                 draft: Mapping[str, Any]) -> None:
        target = self.get(int(target_parent_id))
        if not target:
            raise ValueError("planning target not found")
        target_role = (self.resolved_semantic_role(int(target_parent_id))
                       if target["type"] == "subgoal" else None)

        def validate(nodes, parent_type: str, parent_role: str | None) -> None:
            for raw in list(nodes or []):
                if not isinstance(raw, Mapping):
                    continue
                node_type = self._draft_effective_node_type(raw, parent_type)
                children = list(raw.get("children") or [])
                role = str(raw.get("semantic_role") or "").strip().lower() or None
                if node_type == "overgoal":
                    validate(children, "overgoal", None)
                    continue
                if node_type == "subgoal":
                    if role not in {"area", "project", "stage"}:
                        role = ("project" if parent_type == "overgoal" or parent_role == "area"
                                else "stage" if parent_role == "project" else None)
                    if role not in {"area", "project", "stage"}:
                        raise ValueError("every planned Branch needs an Area, Project, or Stage role")
                    if role == "area" and not (
                            parent_type == "overgoal" or parent_role == "area"):
                        raise ValueError("an Area must live beneath a Root or another Area")
                    if role == "project" and not (
                            parent_type == "overgoal" or parent_role == "area"):
                        raise ValueError("a Project must live beneath a Root or Area")
                    if role == "stage":
                        if parent_role != "project":
                            raise ValueError("a Stage must live directly beneath a Project")
                        if not any(self._draft_effective_node_type(child, "subgoal") == "task"
                                   for child in children if isinstance(child, Mapping)):
                            raise ValueError(
                                "a Stage must group at least one concrete Leaf; terminal work is a Leaf")
                    validate(children, node_type, role)
                elif children:
                    raise ValueError("a Leaf cannot contain child nodes")

        validate(draft.get("nodes") or [], str(target["type"] or ""), target_role)

    def _validate_plan_draft_horizon(
            self, session_id: int, target_parent_id: int,
            draft: Mapping[str, Any], *, allow_incomplete: bool = False,
            horizon: int = PLAN_LEAF_HORIZON) -> None:
        """Preflight every draft sibling group before any goal rows are written.
        Drafts are trimmed to `horizon` Leaves per group before this runs, so an
        over-horizon group here means a caller bypassed the trim."""
        target = self.get(int(target_parent_id))
        if not target:
            raise ValueError("planning target not found")
        from .inference import concept_similarity

        def validate_group(raw_nodes, parent_type: str, *, persisted_parent_id=None) -> None:
            candidates: list[dict] = []
            branches: list[tuple[Mapping[str, Any], str]] = []
            for raw in list(raw_nodes or []):
                if not isinstance(raw, Mapping):
                    continue
                node_type = self._draft_effective_node_type(raw, parent_type)
                if node_type == "task":
                    title = str(raw.get("title") or "").strip()
                    if not normalize_leaf_title(title):
                        if allow_incomplete:
                            continue
                        raise LeafHorizonError(
                            "planning draft contains a Leaf without a title",
                            code="invalid_title")
                    candidates.append({
                        "title": title,
                        "description": str(raw.get("description") or ""),
                        "status": "active",
                    })
                else:
                    branches.append((raw, node_type))
            if len(candidates) > horizon:
                raise LeafHorizonError(
                    f"planning draft exceeds the {horizon}-Leaf horizon",
                    code="horizon_full")
            checked: list[str] = []
            for candidate in candidates:
                key = normalize_leaf_title(candidate["title"])
                for other in checked:
                    if normalize_leaf_title(other) == key:
                        raise LeafHorizonError(
                            "planning draft contains duplicate Leaf titles",
                            code="duplicate_title")
                    score = float(concept_similarity(candidate["title"], other))
                    if score >= 0.55:
                        raise LeafHorizonError(
                            "planning draft contains overlapping Leaves",
                            code="semantic_overlap", similarity=score)
                checked.append(candidate["title"])
            if persisted_parent_id is not None:
                reservations = self.pending_leaf_reservations(
                    int(persisted_parent_id),
                    exclude_refs={f"planning:{int(session_id)}"})
                prior: list[dict] = []
                for candidate in candidates:
                    self.validate_leaf_candidate(
                        int(persisted_parent_id), candidate["title"],
                        description=candidate["description"],
                        reservations=[*reservations, *prior], horizon=horizon)
                    prior.append(candidate)
            for raw, node_type in branches:
                validate_group(raw.get("children") or [], node_type)

        validate_group(
            draft.get("nodes") or [], str(target.get("type") or ""),
            persisted_parent_id=int(target_parent_id))

    def commit_plan(self, session_id: int) -> dict:
        session = self.plan_session(session_id)
        if session["status"] == "implemented":
            return {"goal_id": session["committed_goal_id"], "already_implemented": True}
        if session["status"] != "ready":
            raise ValueError("summarize and review the plan before creating it")
        nodes = list(session["draft"].get("nodes") or [])
        if not nodes:
            raise ValueError("draft has no goals to create")
        target = self.get(session["target_parent_id"])
        placement = session["draft"].get("_placement") or {}
        if session.get("source_item_id") is not None and not placement.get("user_confirmed"):
            raise ValueError(
                "review and confirm where this plan should live before creating it")
        if target and target["type"] == "umbrella":
            if (placement.get("mode") != "new_root" or
                    not placement.get("root_eligible") or
                    not str(placement.get("root_title") or "").strip()):
                raise ValueError("a new Root requires an approved durable life-domain placement")
            root_title = str(placement["root_title"]).strip()
            root_description = str(placement.get("root_description") or "").strip()
            first = dict(nodes[0])
            if str(first.get("title") or "").strip().casefold() != root_title.casefold():
                # The planner described a temporary project as the top node. Keep
                # that work, but place it beneath the approved durable domain.
                nodes = [{
                    "type": "overgoal", "title": root_title,
                    "description": root_description, "priority": "normal",
                    "due_date": None, "children": nodes,
                }]
            else:
                first["type"] = "overgoal"
                first["title"] = root_title
                if root_description:
                    first["description"] = root_description
                nodes[0] = first

        commit_draft = dict(session["draft"])
        commit_draft["nodes"] = nodes
        # Defensive: the stored draft is already trimmed, but re-trim in case an
        # older session was persisted before the one-Leaf model.
        commit_draft = self._trim_draft_leaves(
            int(session["target_parent_id"]), commit_draft)
        nodes = list(commit_draft.get("nodes") or [])
        self._validate_plan_semantics(
            int(session["target_parent_id"]), commit_draft)
        self._validate_plan_draft_horizon(
            int(session_id), int(session["target_parent_id"]), commit_draft)
        persisted_reservations: list[dict] = []

        def add(raw: dict, parent_id: int) -> int:
            # Model drafts are user-reviewed JSON, not schema-guaranteed: coerce
            # the type onto a valid placement instead of failing the whole commit
            # (e.g. a top-level "umbrella", or a task placed under the Soul).
            requested = str(raw.get("type") or "").strip().lower()
            children = raw.get("children") or []
            parent = self.get(parent_id)
            if parent and parent["type"] == "umbrella":
                node_type = "overgoal"
            elif requested == "task" and not children:
                node_type = "task"
            elif requested in {"subgoal", "task", "overgoal", "umbrella"} or children:
                node_type = "subgoal"
            else:
                node_type = "task"
            priority = str(raw.get("priority") or "normal")
            if priority not in PRIORITIES:
                priority = "normal"
            title = str(raw.get("title") or "").strip()
            description = str(raw.get("description") or "")
            if node_type == "task":
                new_id = self.create_ai_leaf(
                    title, parent_id=parent_id, description=description,
                    priority=priority, due_date=raw.get("due_date"),
                    horizon=PLAN_LEAF_HORIZON,
                    reservations=(persisted_reservations
                                  if int(parent_id) == int(session["target_parent_id"])
                                  else None),
                    origin={"source_kind": "planning",
                            "source_id": int(session_id),
                            "source_label": title,
                            "summary": description})
                notes = str(raw.get("notes") or "")
                if notes:
                    self.update(new_id, notes=notes, _commit=False)
            else:
                new_id = self.create(
                    node_type, title, parent_id=parent_id,
                    description=description,
                    notes=str(raw.get("notes") or ""), priority=priority,
                    due_date=raw.get("due_date"), _commit=False)
                semantic_role = str(raw.get("semantic_role") or "").strip().lower()
                if node_type == "subgoal" and semantic_role in {"area", "project", "stage"}:
                    self._set_semantic_role(
                        new_id, semantic_role,
                        rationale=("Planner-defined structure: Areas are ongoing scopes, "
                                   "Projects are finite outcomes, and Stages are project phases."),
                        source="planning", commit=False)
            for child in children:
                add(child, new_id)
            return new_id

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            # Re-read pending cards after taking the write lock. The earlier
            # preflight gives fast feedback; this one is the approval boundary.
            self._validate_plan_draft_horizon(
                int(session_id), int(session["target_parent_id"]), commit_draft)
            persisted_reservations = self.pending_leaf_reservations(
                int(session["target_parent_id"]),
                exclude_refs={f"planning:{int(session_id)}"})
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


def propagate_experiment_outcome(config, outcome_id: int) -> dict:
    """Propagate one stored Leaf debrief into bounded learning contexts."""
    from .curiosity import CuriosityStore
    from .inference import InferenceStore, concept_similarity
    curiosities = CuriosityStore(config.memory_db_path)
    goals = GoalStore(config.memory_db_path)
    inferences = InferenceStore(config.memory_db_path)
    try:
        outcome = goals.outcome(int(outcome_id))
        if not outcome:
            raise ValueError("experiment outcome not found")
        goal_id = int(outcome["goal_id"])
        node = goals.get(goal_id)
        observation = (outcome["changed_understanding"] or outcome["what_happened"]).strip()
        evidence_text = (
            f"Experiment {node['title'] if node else goal_id} {outcome['result']}: {observation}" +
            (f" Helpfulness {float(outcome['helpfulness']):g}/10."
             if outcome["helpfulness"] is not None else "") +
            (f" Surprise: {outcome['surprise']}" if outcome["surprise"] else ""))
        matched_themes = []
        for belief in inferences.confirmed()[:40]:
            if concept_similarity(
                    evidence_text, f"{belief['theme']} {belief['statement']}") < .30:
                continue
            inferences.add_evidence(
                belief["theme"], evidence_text, weight=1.2,
                source_refs=[{"kind": "experiment_outcome", "id": outcome["id"],
                              "goal_id": int(goal_id)}])
            matched_themes.append(belief["theme"])
        if not matched_themes:
            theme = "experiment:" + _slug(node["title"] if node else "outcome")
            inferences.add_evidence(
                theme, evidence_text, weight=1.0,
                source_refs=[{"kind": "experiment_outcome", "id": outcome["id"],
                              "goal_id": int(goal_id)}])
            matched_themes.append(theme)

        from .memory import MemoryStore
        memories = MemoryStore(config.memory_db_path)
        try:
            memory_id = memories.add(
                "Experiments", "Leaf outcome", evidence_text,
                confidence=.95,
                source_refs=[{"kind": "experiment_outcome", "id": outcome["id"],
                              "goal_id": int(goal_id)}],
                raw_source=outcome["what_happened"])
        finally:
            memories.close()

        synthesis_drafted = False
        curiosity_id = outcome.get("curiosity_id")
        if curiosity_id is not None and outcome["helpfulness"] is not None and (
                float(outcome["helpfulness"]) <= 3):
            previous = curiosities.latest_synthesis(curiosity_id, status="approved")
            draft = curiosities.latest_synthesis(curiosity_id, status="draft")
            if previous and not draft:
                revised = json.loads(json.dumps(previous["payload"], ensure_ascii=False))
                old_confidence = float(revised.get("confidence") or 0)
                revised["confidence"] = round(
                    0.0 if old_confidence <= 0 else max(.05, old_confidence * .65), 4)
                counter = list(revised.get("counterevidence") or [])
                counter.append(
                    f"Experiment outcome #{outcome['id']} was rated "
                    f"{float(outcome['helpfulness']):g}/10 helpful: {observation}")
                revised["counterevidence"] = counter[-8:]
                revised["changed_since_previous"] = (
                    "A real-world experiment did not help as expected, so confidence "
                    "was reduced pending your review.")
                if outcome["next_adjustment"]:
                    experiments = list(revised.get("experiments") or [])
                    revised["experiments"] = [outcome["next_adjustment"], *experiments][:8]
                evidence = list(revised.get("supporting_evidence") or [])
                evidence.append({"item_id": None,
                                 "source_ref": f"experiment_outcome:{outcome['id']}",
                                 "summary": observation})
                revised["supporting_evidence"] = evidence[-10:]
                curiosities.add_synthesis(
                    curiosity_id, revised,
                    based_on_item_id=previous.get("based_on_item_id"),
                    based_on_outcome_id=outcome["id"])
                synthesis_drafted = True

        next_proposal_id = None
        if outcome["next_adjustment"] and node and node.get("parent_id"):
            from .goal_ai import AgentProposal, GoalAgentStore
            agents = GoalAgentStore(config.memory_db_path)
            try:
                title = outcome["next_adjustment"].splitlines()[0].strip()[:160]
                parent_id = int(node["parent_id"])
                description = (f"Proposed after outcome #{outcome['id']}: "
                               f"{observation}")[:1000]
                limit = min(2, max(1, int(getattr(
                    config, "goal_ai_leaf_horizon", 2))))
                reservations = agents.leaf_reservations(parent_id)
                try:
                    goals.validate_leaf_candidate(
                        parent_id, title, description=description,
                        reservations=reservations, horizon=limit)
                    next_proposal_id = agents.add_proposal(
                        parent_id, AgentProposal(
                            "create_child", parent_id,
                            {"type": "task", "title": title,
                             "description": description,
                             "outcome_id": outcome["id"]},
                            f"The prior experiment suggested this adjustment: {title}"),
                        goals=goals, leaf_horizon=limit)
                except LeafHorizonError as horizon_error:
                    # The next adjustment should adapt the horizon, not clone
                    # an overlapping Leaf or silently build a third step. If
                    # it overlaps, revise that exact Leaf; if capacity is full,
                    # revise the furthest (PROVISIONAL) open Leaf.
                    replacement_id = horizon_error.conflicting_leaf_id
                    if replacement_id is None and horizon_error.code == "horizon_full":
                        open_rows = goals.conn.execute(
                            "SELECT id FROM goal_node WHERE parent_id=? "
                            "AND node_type='task' AND status IN ('active','paused') "
                            "ORDER BY position,id", (parent_id,)).fetchall()
                        if open_rows:
                            replacement_id = int(open_rows[-1]["id"])
                    if replacement_id is not None:
                        next_proposal_id = agents.add_proposal(
                            parent_id, AgentProposal(
                                "update_fields", int(replacement_id),
                                {"title": title, "description": description,
                                 "outcome_id": outcome["id"],
                                 "adaptive_horizon": True},
                                "Adapt the existing horizon after the experiment "
                                f"instead of creating overlapping work: {title}"))
            finally:
                agents.close()

        return {"ok": True, "outcome": outcome,
                "synthesis_drafted": synthesis_drafted,
                "inference_themes": matched_themes,
                "memory_id": memory_id,
                "next_proposal_id": next_proposal_id,
                "review_due": True}
    finally:
        inferences.close(); goals.close(); curiosities.close()


def record_experiment_outcome(config, goal_id: int, payload: dict) -> dict:
    """Store a user-reported Leaf outcome and propagate it as bounded evidence."""
    goals = GoalStore(config.memory_db_path)
    try:
        helpfulness = payload.get("helpfulness")
        if helpfulness in {"", None}:
            helpfulness = None
        outcome = goals.add_outcome(
            int(goal_id), str(payload.get("result") or ""),
            str(payload.get("what_happened") or ""),
            expected_obstacle=str(payload.get("expected_obstacle") or ""),
            surprise=str(payload.get("surprise") or ""),
            helpfulness=float(helpfulness) if helpfulness is not None else None,
            changed_understanding=str(payload.get("changed_understanding") or ""),
            next_adjustment=str(payload.get("next_adjustment") or ""),
            curiosity_id=(int(payload["curiosity_id"])
                          if payload.get("curiosity_id") is not None else None),
            source_item_id=(int(payload["source_item_id"])
                            if payload.get("source_item_id") is not None else None))
    finally:
        goals.close()
    return propagate_experiment_outcome(config, int(outcome["id"]))


PLANNER_SYSTEM = """You help turn one grounded suggestion into an actionable goal plan.
While important decisions remain, ask exactly one decision-bearing question at a time and briefly
recommend a current approach first. Once the outcome, scope, and first concrete action are clear,
stop asking questions and explicitly tell the user: "The plan is ready—press Summarize & review
below to review the structure before anything is created." Never activate goals yourself.

Use the hierarchy by meaning, not by node count: a Root is a durable life domain; a Branch (emit
semantic_role "area") is an ongoing scope inside that domain which survives any one project; a
Project is one finite outcome; a Stage is a distinct phase inside that project; and a Leaf is one
concrete action or finishable outcome. Related phases of one outcome belong under one Project, not
as several sibling Projects. A finite action such as mapping, tracking, designing, testing, or
deciding is normally a Project, never a Branch merely because it has child phases. Add a Branch
wrapper only when its title names a genuinely ongoing scope beyond the current project. Skip Stage
when the Project only needs Leaves.

Return strict JSON:
{"message": str, "draft": {"rationale": str, "nodes": [goal nodes]}}.
Goal nodes use type overgoal|subgoal|task, semantic_role area|project|stage|null, title,
description, priority low|normal|high, due_date YYYY-MM-DD or null, and children. semantic_role is
required for subgoal nodes and null otherwise. Tasks have no children.
ONE-LEAF RULE: give each Project (or Stage) exactly ONE Leaf — the single concrete NOW action.
Never draft a second Leaf or a checklist of steps under the same parent: the next Leaf is created
later in the main chat once the current one is finished, carrying its handoff. If you can only
name a series of actions and not a single first one, ask the user which comes first instead.
Never use type "umbrella". Use TODAY from the prompt for any dates. Keep
descriptions short so the JSON reply never exceeds ~1500 tokens.
"""

PLACEMENT_SYSTEM = """Choose where a proposed plan belongs in an existing personal Growth tree.
This is a semantic ownership decision, not a wording-match exercise. Prefer the most specific
existing Root or Branch whose enduring purpose owns the proposed work. A temporary project,
experiment, milestone, product test, or task is not a Root. Recommend a new Root only for a
distinct durable life domain that should still matter after the proposed project ends.

Return strict JSON:
{"parent_id": int|null, "confidence": 0..1, "rationale": str,
 "question": str, "plan_title": str, "new_root":
 {"eligible": bool, "title": str, "description": str, "rationale": str}}.
Use only a supplied parent id. Set parent_id to null when a genuinely new Root is best or when
the evidence is too uncertain. Ask at most one short placement question when confidence is below
0.72; otherwise question must be empty. A new Root title must name a broad durable domain, never
the temporary proposal itself.
"""

INTAKE_SYSTEM = """Classify one thing a user wants to add to their personal Growth tree.
Users should not have to choose structural terminology. Roots are enduring life domains. A Branch
may be an Area (ongoing scope), Project (finite outcome), or Stage (phase inside a project). A
Leaf is one concrete action or finishable outcome. Skip unnecessary levels. Prefer the most
specific supplied parent that genuinely owns the addition. If substantially equivalent work
already exists, point to it instead of creating a duplicate. Recommend a new Root only when the
text clearly describes a durable life domain that remains after current projects finish.

Return strict JSON:
{"action":"propose"|"existing"|"uncertain", "parent_id":int|null,
 "new_type":"overgoal"|"subgoal"|"task"|null,
 "semantic_role":"area"|"project"|"stage"|null, "existing_goal_id":int|null,
 "title":str, "description":str, "confidence":0..1, "rationale":str, "question":str,
 "nested_stage_justification":str}.
Use only supplied ids. Ask at most one short question. Do not create or mutate anything.
Normally use Project → Stage or Stage → Leaf. Only propose Stage → Stage when the child is a
genuine substage of a distinct macro-stage, and explain that boundary in
nested_stage_justification. Otherwise leave that field empty.
"""

RESTRUCTURE_SYSTEM = """Review one existing node inside a personal Growth tree and decide whether
its structural role is misleading. Use meaning and durable ownership, not title overlap alone.
Soul is the whole person; Root is an enduring life domain; Branch is a project, strategy, area,
or experiment inside a domain; Leaf is one concrete outcome or action. A temporary project or
experiment must not be a Root. Prefer the most specific existing parent that genuinely owns the
work. Do not recommend change merely for cosmetic neatness.

Return strict JSON:
{"action":"keep"|"restructure"|"uncertain", "new_type":"overgoal"|"subgoal"|"task"|null,
 "parent_id":int|null, "confidence":0..1, "rationale":str, "question":str}.
Use only a supplied parent id. Ask at most one short plain-language question when uncertain.
"""

TREE_RESTRUCTURE_SYSTEM = """Review the selected node's ancestor path and descendant subtree as
one coherent Growth structure. Keep the stored types Soul, Root, Branch, and Leaf. Soul is the
whole person; a Root is a durable life domain; a Branch may be an Area, Project, or Stage; a Leaf
is one concrete outcome or action. Nested Branches are allowed only when their scopes are genuinely
different. Do not preserve a project-shaped Root or use several generic Branch labels when Area,
Project, and Stage explain the nesting. Prefer the smallest set of changes that makes the path
understandable. When a generic catch-all Root contains work that now has specific durable Root
owners, move its descendants to those supplied Roots instead of nesting the catch-all beneath one
of them. Leave the emptied catch-all Root unchanged and warn that the user may archive it after
review. Never delete, merge, archive, rename, or recreate a node.

Return strict JSON:
{"action":"keep"|"restructure"|"uncertain", "confidence":0..1, "rationale":str,
 "question":str, "nodes":[{"goal_id":int,"new_type":"overgoal"|"subgoal"|"task",
 "parent_id":int,"semantic_role":"area"|"project"|"stage"|null,"reason":str,
 "nested_stage_justification":str}],
 "warnings":[str]}.
Include a node only when its type, parent, or Branch role should change. Use only supplied ids.
Area/Project/Stage applies only when new_type is subgoal. Ask at most one short question when the
meaning is genuinely uncertain. Return at most 16 node changes.
Use a strict hierarchy: Area may live beneath Root/Area; Project may live beneath Root/Area;
Stage may live directly beneath Project; Project → Project and Stage → Stage are invalid. A Stage
must group concrete Leaves. Terminal tracking, capture, analysis, design, or decision work is a
Leaf, not an empty Stage. Prefer Project → Leaves when a phase would contain only one Leaf.
"""

SUMMARY_SYSTEM = """Turn this planning dialogue into one concise editable goal tree.
Use the user's decisions as authoritative. Return strict JSON:
{"summary": str, "draft": {"rationale": str, "nodes": [goal nodes]}}.
Use the hierarchy by meaning: Root = durable life domain; Branch (semantic_role "area") = ongoing
scope that survives any one project; Project = one finite outcome; Stage = a distinct project
phase; Leaf = one concrete action or finishable outcome. Related phases of a single outcome must be
nested under one Project, not emitted as several sibling Projects. Mapping, tracking, designing,
testing, and deciding are finite project work, not Branches. When the target is a Root, add a
Branch wrapper only if the dialogue supports a genuinely ongoing scope beyond this project;
otherwise create the Project directly.
Small Projects should connect directly to Leaves and omit Stage.
ONE-LEAF RULE: emit exactly ONE Leaf per Project (or per Stage) — the single concrete NOW action.
Never emit two Leaves under the same parent or a step-by-step checklist; the next Leaf is created
later in the main chat after the current one is finished. Use priority high only when the user
explicitly identified urgency; otherwise default every new node to normal.
The first node must fit below the supplied target: overgoal below umbrella,
otherwise subgoal below an overgoal/subgoal. Never use type "umbrella".
Include concrete tasks when known; do not invent dates, and derive any dates from TODAY in the
prompt. Nodes use type, semantic_role, title, description, priority, due_date, children.
semantic_role is required as area|project|stage for subgoal nodes and null otherwise. Keep
descriptions short so the JSON reply never exceeds ~1500 tokens.
"""


def _strip_json_fence(text: str) -> str:
    raw = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    return fence.group(1).strip() if fence else raw


def _json_object(text: str) -> dict:
    """Best-effort extraction of a JSON object from a model reply.

    Tolerates a ```json fence, prose wrapped around the object, and trailing
    text that itself contains braces. Returns {} only when no complete object
    can be recovered (e.g. the reply was truncated mid-object)."""
    raw = _strip_json_fence(text)
    if not raw:
        return {}
    try:
        whole = json.loads(raw)
        if isinstance(whole, dict):
            return whole
    except json.JSONDecodeError:
        pass
    # Scan for the first balanced, string-aware {...} object.
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break  # malformed here — advance to the next '{'
                    if isinstance(obj, dict):
                        return obj
                    break
        start = raw.find("{", start + 1)
    return {}


def _salvage_planner_message(text: str) -> str:
    """Recover a usable conversational reply when strict JSON parsing failed.

    Handles two model failure modes for the chat-style planner calls: a plain
    prose reply (no JSON at all), and a JSON reply truncated before it closed
    but whose "message" field is intact. Returns "" when nothing is usable."""
    raw = _strip_json_fence(text)
    if not raw:
        return ""
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
    if match:
        try:
            return str(json.loads('"' + match.group(1) + '"'))
        except json.JSONDecodeError:
            return match.group(1)
    # No JSON object at all — treat the whole prose reply as the message.
    return "" if raw.lstrip().startswith("{") else raw


class StubGoalPlanner:
    def place(self, suggestion: str, candidates: list[dict], soul: dict) -> dict:
        from .inference import concept_similarity
        domain_groups = [
            ({"career", "work", "job", "business", "freelance", "upwork", "client", "building", "automation"},
             "Career & Work", "An enduring domain for work, building, clients, and professional development."),
            ({"health", "energy", "food", "sleep", "exercise", "body", "mental"},
             "Health & Wellbeing", "An enduring domain for physical health, energy, and emotional wellbeing."),
            ({"relationship", "family", "friend", "social", "love"},
             "Relationships & Community", "An enduring domain for close relationships and community life."),
            ({"learning", "language", "korean", "study", "education"},
             "Learning & Education", "An enduring domain for learning, study, and skill development."),
            ({"creative", "art", "music", "writing", "aesthetic", "design"},
             "Creativity & Expression", "An enduring domain for creative practice and self-expression."),
            ({"finance", "money", "budget", "saving", "income"},
             "Finances", "An enduring domain for money, financial stability, and long-term resources."),
        ]
        suggestion_words = set(re.findall(r"[a-z0-9]+", suggestion.casefold()))
        matched_domain = next((spec for spec in domain_groups if suggestion_words & spec[0]), None)
        ranked: list[tuple[float, dict]] = []
        for candidate in candidates:
            comparison = "\n".join(filter(None, [
                candidate.get("path", ""), candidate.get("description", "")]))
            score = concept_similarity(suggestion, comparison)
            candidate_words = set(re.findall(r"[a-z0-9]+", comparison.casefold()))
            for group, _title, _description in domain_groups:
                if suggestion_words & group and candidate_words & group:
                    score += .32
            if candidate.get("node_type") == "subgoal":
                score += .02
            ranked.append((min(score, 1.0), candidate))
        ranked.sort(key=lambda item: item[0], reverse=True)
        best_score, best = ranked[0] if ranked else (0.0, None)
        confident = bool(best and best_score >= .30)
        propose_domain = bool(not confident and matched_domain)
        plan_title = " ".join(suggestion.split())[:72]
        return {
            "parent_id": int(best["id"]) if confident else None,
            "confidence": round(best_score, 3) if confident else (.78 if propose_domain else .35),
            "rationale": (f"This work fits the enduring purpose of {best['path']}."
                          if confident else (f"This belongs to the durable {matched_domain[1]} domain."
                          if propose_domain else "The existing tree does not provide a clear durable owner yet.")),
            "question": ("" if confident or propose_domain else
                         "Which existing area of your life should own this work?"),
            "plan_title": plan_title,
            "new_root": {
                "eligible": propose_domain,
                "title": matched_domain[1] if propose_domain else "",
                "description": matched_domain[2] if propose_domain else "",
                "rationale": ("This names an ongoing life domain that remains relevant after the "
                              "temporary proposal ends." if propose_domain else ""),
            },
        }

    def recommend_restructure(self, node: dict, candidates: list[dict], soul: dict) -> dict:
        text = "\n".join(filter(None, [node.get("title", ""), node.get("description", ""),
                                        node.get("path", "")]))
        # The Soul appears verbatim in every current path, so it is not a useful
        # semantic competitor when looking for a more specific enduring owner.
        placed = self.place(text, [c for c in candidates if c.get("node_type") != "umbrella"], soul)
        parent_id = placed.get("parent_id")
        has_children = bool(node.get("child_count"))
        new_type = "subgoal" if has_children or node.get("type") != "task" else "task"
        changed = parent_id is not None and (
            int(parent_id) != int(node.get("parent_id") or 0) or new_type != node.get("type"))
        return {
            "action": "restructure" if changed else ("keep" if node.get("type") == "overgoal" else "uncertain"),
            "new_type": new_type if changed else None,
            "parent_id": parent_id if changed else None,
            "confidence": max(.78, float(placed.get("confidence", .35))) if changed else placed.get("confidence", .35),
            "rationale": (placed.get("rationale", "") if changed else
                          "The current structure is not clearly wrong from the available context."),
            "question": "" if changed else "Which enduring part of your life should own this work?",
        }

    def recommend_tree_restructure(self, review: dict, candidates: list[dict],
                                   soul: dict) -> dict:
        nodes = {int(node["id"]): dict(node) for node in review.get("nodes", [])}
        durable_words = {"career", "work", "health", "wellbeing", "relationship",
                         "relationships", "learning", "education", "finance", "finances",
                         "creativity", "creative", "home", "family", "community",
                         "language", "korean", "gaming", "games", "hobby", "recreation",
                         "league", "legends"}
        project_words = {"project", "experiment", "test", "launch", "build", "upwork",
                         "automation", "product", "gig", "prototype", "migration",
                         "interface", "app"}
        stage_words = {"brainstorm", "evaluate", "select", "validate", "review", "prepare",
                       "plan", "write", "post", "log", "measure", "choose", "research"}
        generic_words = {"life", "lives", "overall", "actualized", "self", "world"}
        changes: list[dict] = []
        final = {node_id: {"type": node.get("type"), "parent_id": node.get("parent_id")}
                 for node_id, node in nodes.items()}
        root = nodes.get(int(review.get("scope_root_id") or 0))
        demoted_temporary_root = False
        if root:
            root_words = set(re.findall(r"[a-z0-9]+", root.get("title", "").casefold()))
            if root_words & project_words:
                single = self.recommend_restructure(
                    {**root, "path": root.get("path", ""),
                     "child_count": len(root.get("child_ids") or [])},
                    candidates, soul)
                if (single.get("action") == "restructure" and
                        single.get("new_type") == "subgoal" and single.get("parent_id")):
                    final[root["id"]] = {"type": "subgoal",
                                         "parent_id": int(single["parent_id"])}
                    changes.append({"goal_id": root["id"], "new_type": "subgoal",
                                    "parent_id": int(single["parent_id"]),
                                    "semantic_role": "project",
                                    "reason": str(single.get("rationale") or
                                                  "This temporary project belongs inside an enduring domain.")})
                    demoted_temporary_root = True
            selected_path_ids = [int(value) for value in review.get("selected_path_ids", [])]
            for goal_id in ([] if demoted_temporary_root else selected_path_ids):
                node = nodes.get(goal_id)
                if not node or node.get("type") != "subgoal" or node.get("parent_id") != root["id"]:
                    continue
                words = set(re.findall(r"[a-z0-9]+", node.get("title", "").casefold()))
                if words & durable_words and root_words & generic_words:
                    final[goal_id] = {"type": "overgoal", "parent_id": int(soul["id"])}
                    changes.append({"goal_id": goal_id, "new_type": "overgoal",
                                    "parent_id": int(soul["id"]), "semantic_role": None,
                                    "reason": (f"{node['title']} is an enduring life domain, while "
                                               f"{root['title']} is too broad to clarify ownership.")})
                    break

        def branch_role(goal_id: int) -> str:
            node = nodes[goal_id]
            words = set(re.findall(r"[a-z0-9]+", node.get("title", "").casefold()))
            if words & stage_words:
                return "stage"
            if words & project_words:
                return "project"
            parent_id = int(final[goal_id].get("parent_id") or 0)
            parent = final.get(parent_id)
            if parent and parent.get("type") == "overgoal":
                return "area" if words & durable_words else "project"
            parent_node = nodes.get(parent_id, {})
            parent_role = str(parent_node.get("semantic_role") or "")
            return "project" if parent_role == "area" else "stage"

        changed_ids = {item["goal_id"] for item in changes}
        for goal_id, state in final.items():
            if state.get("type") != "subgoal":
                continue
            role = branch_role(goal_id)
            node = nodes[goal_id]
            if node.get("semantic_role_source") != "derived" and node.get("semantic_role") == role:
                continue
            entry = next((item for item in changes if item["goal_id"] == goal_id), None)
            if entry:
                entry["semantic_role"] = role
            else:
                changes.append({"goal_id": goal_id, "new_type": "subgoal",
                                "parent_id": int(state["parent_id"]),
                                "semantic_role": role,
                                "reason": f"Show this Branch as a {role.title()} so its scope is clear."})
                changed_ids.add(goal_id)
        return {
            "action": "restructure" if changes else "keep",
            "confidence": .86 if changes else .72,
            "rationale": ("The path can be made clearer by separating durable domains from "
                          "projects and stages." if changes else
                          "The current path already communicates distinct scopes."),
            "question": "", "nodes": changes[:16], "warnings": [],
        }

    def classify_intake(self, text: str, selected: dict, candidates: list[dict],
                        soul: dict) -> dict:
        from .inference import concept_similarity
        cleaned = " ".join(str(text or "").split())
        words = set(re.findall(r"[\w]+", cleaned.casefold()))
        ranked_existing = []
        for candidate in candidates:
            if candidate.get("node_type") == "umbrella":
                continue
            score = concept_similarity(
                cleaned, f"{candidate.get('title', '')} {candidate.get('description', '')}")
            ranked_existing.append((score, candidate))
        ranked_existing.sort(key=lambda item: item[0], reverse=True)
        if ranked_existing and ranked_existing[0][0] >= .78:
            match = ranked_existing[0][1]
            return {"action": "existing", "existing_goal_id": int(match["id"]),
                    "parent_id": None, "new_type": None, "semantic_role": None,
                    "title": match["title"], "description": "", "confidence": .88,
                    "rationale": f"This appears to describe the existing {match['path']}.",
                    "question": ""}
        if re.search(r"\b(new root|new life domain|ongoing life area)\b", cleaned, re.I):
            return {"action": "propose", "parent_id": int(soul["id"]),
                    "new_type": "overgoal", "semantic_role": None,
                    "existing_goal_id": None, "title": cleaned[:120],
                    "description": cleaned[:1000], "confidence": .82,
                    "rationale": "You described this explicitly as an enduring life domain.",
                    "question": ""}
        project_words = {"project", "build", "create", "launch", "experiment", "test",
                         "prototype", "migration", "redesign", "app", "프로젝트", "만들기",
                         "출시", "실험"}
        stage_words = {"stage", "phase", "brainstorm", "evaluate", "prepare", "review",
                       "research", "plan", "단계", "평가", "준비", "검토", "조사", "계획"}
        area_words = {"practice", "learning", "language", "career", "health", "relationships",
                      "finance", "gaming", "hobby", "연습", "배움", "언어", "커리어",
                      "건강", "관계", "재정", "게임", "취미"}
        destination = selected
        ranked_parents = []
        for candidate in candidates:
            if candidate.get("node_type") == "umbrella" or not candidate.get("can_parent"):
                continue
            score = concept_similarity(
                cleaned, f"{candidate.get('path', '')} {candidate.get('description', '')}")
            ranked_parents.append((score, candidate))
        ranked_parents.sort(key=lambda item: item[0], reverse=True)
        if ranked_parents and ranked_parents[0][0] >= .42:
            destination = ranked_parents[0][1]
        if words & stage_words:
            node_type, role = "subgoal", "stage"
        elif words & project_words:
            node_type, role = "subgoal", "project"
        elif words & area_words and destination.get("node_type") == "overgoal":
            node_type, role = "subgoal", "area"
        else:
            imperative = bool(re.match(
                r"^(write|send|call|buy|schedule|post|finish|complete|practice|open|make|"
                r"작성|보내|전화|구매|예약|게시|완료|연습|열기)", cleaned, re.I))
            node_type, role = (
                ("task", None) if imperative or len(words) <= 8 else ("subgoal", "project"))
        return {"action": "propose", "parent_id": int(destination["id"]),
                "new_type": node_type, "semantic_role": role, "existing_goal_id": None,
                "title": cleaned[:120], "description": cleaned[:1000], "confidence": .76,
                "rationale": (f"This fits beneath {destination.get('path') or destination.get('title')} "
                              f"as a {role.title() if role else 'Leaf'}."),
                "question": ""}

    def first(self, suggestion: str, target: dict,
              placement: dict | None = None) -> tuple[str, dict]:
        message = (f'I can turn “{suggestion}” into a plan. My starting approach is to make '
                   "the smallest useful outcome explicit first. What would success look like?")
        return message, {"rationale": suggestion, "nodes": []}

    def reply(self, session: dict, answer: str, target: dict) -> tuple[str, dict]:
        draft = dict(session.get("draft") or {})
        draft["success"] = answer.strip()
        user_turns = sum(message.get("role") == "user"
                         for message in session.get("messages", [])) + 1
        if user_turns >= 2:
            return ("The plan is ready—press Summarize & review below to review the "
                    "structure before anything is created.", draft)
        return ("That gives the plan a finish line. What is the first concrete action "
                "you want to take?", draft)

    def summarize(self, session: dict, target: dict) -> tuple[str, dict]:
        suggestion = session.get("draft", {}).get("rationale") or "Implement the idea"
        success = session.get("draft", {}).get("success") or "Define a useful outcome"
        placement = session.get("draft", {}).get("_placement") or {}
        project = {"type": "subgoal", "semantic_role": "project",
                   "title": suggestion[:80], "description": success,
                   "priority": "normal", "due_date": None, "children": [{
                       "type": "task", "semantic_role": None,
                       "title": "Take the first concrete step",
                       "description": "", "priority": "normal", "due_date": None,
                       "children": [],
                   }]}
        if target["type"] == "umbrella":
            nodes = [{"type": "overgoal", "semantic_role": None,
                      "title": placement.get("root_title", "New life domain"),
                      "description": placement.get("root_description", ""), "priority": "normal",
                      "due_date": None, "children": [project]}]
        else:
            nodes = [project]
        draft = {"rationale": suggestion, "nodes": nodes, "_placement": placement}
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

    def _call(self, system: str, prompt: str, *, allow_prose: bool = False) -> dict:
        log_diag("prompt", f"surface=goal-planner model={self.model} input_chars={len(prompt)}")
        msg = self.client.messages.create(
            model=self.model, max_tokens=8000, system=system,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        truncated = getattr(msg, "stop_reason", None) == "max_tokens"
        if truncated:
            log_diag("prompt", f"surface=goal-planner model={self.model} "
                     "reply truncated at max_tokens=8000")
        data = _json_object(text)
        if data:
            return data
        # For the chat-style planner turns, a prose or truncated reply must not
        # throw away the user's message — salvage a conversational reply so the
        # session keeps its draft and the user can simply keep going.
        if allow_prose:
            salvaged = _salvage_planner_message(text)
            if salvaged:
                return {"message": salvaged}
        raise ValueError("the planner reply could not be read — nothing was "
                         "saved, please try again")

    @staticmethod
    def _today() -> str:
        return datetime.now().astimezone().date().isoformat()

    def place(self, suggestion: str, candidates: list[dict], soul: dict) -> dict:
        prompt = (f"SOUL: {json.dumps(soul, ensure_ascii=False)}\n"
                  f"PROPOSAL: {suggestion}\n"
                  f"POSSIBLE EXISTING PARENTS: {json.dumps(candidates, ensure_ascii=False)}")
        return self._call(PLACEMENT_SYSTEM, prompt)

    def recommend_restructure(self, node: dict, candidates: list[dict], soul: dict) -> dict:
        prompt = (f"SOUL: {json.dumps(soul, ensure_ascii=False)}\n"
                  f"NODE TO REVIEW: {json.dumps(node, ensure_ascii=False)}\n"
                  f"VALID POSSIBLE PARENTS: {json.dumps(candidates, ensure_ascii=False)}")
        return self._call(RESTRUCTURE_SYSTEM, prompt)

    def recommend_tree_restructure(self, review: dict, candidates: list[dict],
                                   soul: dict) -> dict:
        prompt = (f"SOUL: {json.dumps(soul, ensure_ascii=False)}\n"
                  f"SELECTED PATH AND SUBTREE: {json.dumps(review, ensure_ascii=False)}\n"
                  f"VALID DESTINATIONS: {json.dumps(candidates, ensure_ascii=False)}")
        return self._call(TREE_RESTRUCTURE_SYSTEM, prompt)

    def classify_intake(self, text: str, selected: dict, candidates: list[dict],
                        soul: dict) -> dict:
        prompt = (f"SOUL: {json.dumps(soul, ensure_ascii=False)}\n"
                  f"CURRENTLY SELECTED SCOPE: {json.dumps(selected, ensure_ascii=False)}\n"
                  f"VALID PARENTS AND EXISTING NODES: {json.dumps(candidates, ensure_ascii=False)}\n"
                  f"USER ADDITION: {str(text)[:3000]}")
        return self._call(INTAKE_SYSTEM, prompt)

    def first(self, suggestion: str, target: dict,
              placement: dict | None = None) -> tuple[str, dict]:
        data = self._call(PLANNER_SYSTEM,
                          f"TODAY: {self._today()}\nTARGET TYPE: {target['type']}\n"
                          f"APPROVED PLACEMENT: {json.dumps(placement or {}, ensure_ascii=False)}\n"
                          f"SUGGESTION: {suggestion}", allow_prose=True)
        return str(data.get("message") or "What outcome do you want?"), data.get("draft") or {}

    def reply(self, session: dict, answer: str, target: dict) -> tuple[str, dict]:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"])
        data = self._call(PLANNER_SYSTEM,
                          f"TODAY: {self._today()}\nTARGET TYPE: {target['type']}\n"
                          f"DRAFT: {json.dumps(session['draft'])}"
                          f"\nDIALOGUE:\n{transcript}\nuser: {answer}", allow_prose=True)
        return str(data.get("message") or "What should we decide next?"), data.get("draft") or session["draft"]

    def summarize(self, session: dict, target: dict) -> tuple[str, dict]:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"])
        data = self._call(SUMMARY_SYSTEM,
                          f"TODAY: {self._today()}\nTARGET TYPE: {target['type']}\n"
                          f"DIALOGUE:\n{transcript}\n"
                          f"CURRENT DRAFT: {json.dumps(session['draft'])}")
        return str(data.get("summary") or "Review this plan."), data.get("draft") or session["draft"]


def get_goal_planner(config):
    backend = (getattr(config, "curiosity_backend", "") or
               getattr(config, "inference_backend", "claude")).lower()
    return StubGoalPlanner() if backend == "stub" else ClaudeGoalPlanner(config)


def recommend_suggestion_placement(config, planner, source_item_id: int,
                                     *, max_candidates: int = 80,
                                     planning_context: str = "") -> dict:
    """Recommend a semantic owner before a suggestion can become a plan.

    This deliberately remains separate from overlap detection: overlap asks
    whether work already exists, while placement asks which enduring area owns
    genuinely new work.
    """
    store = GoalStore(config.memory_db_path)
    try:
        row = store.conn.execute(
            "SELECT kind,status,text FROM curiosity_item WHERE id=?",
            (int(source_item_id),)).fetchone()
        if not row or row["kind"] != "suggestion" or row["status"] != "open":
            raise ValueError("only an open suggestion can be placed")
        suggestion = crypto.dec(row["text"]) or ""
        soul_node = store.get(store.root_id)
        soul = {"id": store.root_id, "title": soul_node["title"],
                "description": soul_node.get("description", "")}
        candidates: list[dict] = []
        for entry in store.catalog(max_candidates + 1):
            if entry["type"] not in {"Root", "Branch"} or entry["status"] != "active":
                continue
            node = store.get(entry["id"])
            semantic_role = (store.resolved_semantic_role(int(entry["id"]))
                             if node["type"] == "subgoal" else None)
            candidates.append({
                "id": int(entry["id"]), "node_type": node["type"],
                "type_label": semantic_role.title() if semantic_role else entry["type"],
                "semantic_role": semantic_role, "title": entry["title"],
                "path": entry["path"],
                "description": str(node.get("description") or "")[:700],
            })
            if len(candidates) >= max_candidates:
                break
        placement_text = suggestion
        if str(planning_context or "").strip():
            placement_text += "\n\nREFINED PLANNING CONTEXT:\n" + str(planning_context)[:6000]
        raw = planner.place(placement_text, candidates, soul) or {}
        candidate_by_id = {item["id"]: item for item in candidates}
        try:
            parent_id = int(raw.get("parent_id")) if raw.get("parent_id") is not None else None
        except (TypeError, ValueError):
            parent_id = None
        if parent_id not in candidate_by_id:
            parent_id = None
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        root_raw = raw.get("new_root") if isinstance(raw.get("new_root"), dict) else {}
        root_title = " ".join(str(root_raw.get("title") or "").split())[:100]
        root_description = str(root_raw.get("description") or "").strip()[:700]
        root_eligible = bool(root_raw.get("eligible") and root_title and root_description)
        plan_title = " ".join(str(raw.get("plan_title") or suggestion).split())[:90]
        if not plan_title:
            plan_title = "New plan"
        recommended = candidate_by_id.get(parent_id)
        if recommended:
            proposed_path = f"{recommended['path']} › {plan_title}"
        elif root_eligible:
            proposed_path = f"{soul['title']} › {root_title} › {plan_title}"
        else:
            proposed_path = ""
        # Low confidence is intentionally surfaced instead of silently falling
        # back to the Soul, even if the model supplied a candidate id.
        needs_choice = confidence < .72 or (recommended is None and not root_eligible)
        ordered = sorted(candidates, key=lambda item: (
            0 if item["id"] == parent_id else 1,
            0 if item["type_label"] == "Branch" else 1,
            item["path"].casefold()))
        return {
            "item_id": int(source_item_id), "suggestion": suggestion,
            "plan_title": plan_title, "confidence": round(confidence, 3),
            "rationale": str(raw.get("rationale") or "").strip(),
            "question": str(raw.get("question") or "").strip()[:300],
            "needs_choice": needs_choice, "recommended_parent_id": parent_id,
            "recommended": recommended, "proposed_path": proposed_path,
            "soul": soul,
            "candidates": ordered,
            "new_root": {
                "eligible": root_eligible, "title": root_title,
                "description": root_description,
                "rationale": str(root_raw.get("rationale") or "").strip()[:500],
                "path": (f"{soul['title']} › {root_title} › {plan_title}"
                         if root_eligible else ""),
            },
        }
    finally:
        store.close()


def recommend_goal_restructure(config, planner, goal_id: int,
                               *, max_candidates: int = 80) -> dict:
    store = GoalStore(config.memory_db_path)
    try:
        node = store.get(int(goal_id))
        if not node or node["type"] == "umbrella" or node["status"] == "archived":
            raise ValueError("an active Root, Branch, or Leaf is required")
        subtree_ids = set(store._subtree_ids(int(goal_id)))
        soul_node = store.get(store.root_id)
        soul = {"id": store.root_id, "title": soul_node["title"],
                "description": soul_node.get("description", "")}
        candidates: list[dict] = [{
            "id": store.root_id, "node_type": "umbrella", "type_label": "Soul",
            "title": soul["title"], "path": soul["title"],
            "description": soul["description"],
        }]
        for entry in store.catalog(max_candidates + len(subtree_ids) + 1):
            if (entry["id"] in subtree_ids or entry["type"] not in {"Root", "Branch"}
                    or entry["status"] != "active"):
                continue
            candidate = store.get(entry["id"])
            candidates.append({
                "id": int(entry["id"]), "node_type": candidate["type"],
                "type_label": entry["type"], "title": entry["title"],
                "path": entry["path"],
                "description": str(candidate.get("description") or "")[:700],
            })
            if len(candidates) >= max_candidates:
                break
        children = [store.get(child_id) for child_id in [int(row["id"]) for row in store.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND status!='archived' ORDER BY position,id",
            (int(goal_id),)).fetchall()]]
        context = {
            "id": int(goal_id), "type": node["type"], "title": node["title"],
            "description": str(node.get("description") or "")[:1000],
            "path": " › ".join(store._goal_path_titles(int(goal_id))),
            "parent_id": node["parent_id"], "child_count": len(children),
            "children": [{"type": child["type"], "title": child["title"],
                          "description": str(child.get("description") or "")[:250]}
                         for child in children[:12] if child],
        }
        raw = planner.recommend_restructure(context, candidates, soul) or {}
        action = str(raw.get("action") or "uncertain").strip().lower()
        if action not in {"keep", "restructure", "uncertain"}:
            action = "uncertain"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        recommendation = None
        if action == "restructure":
            try:
                new_type = str(raw.get("new_type") or "")
                parent_id = int(raw.get("parent_id"))
                preview = store.restructure_preview(int(goal_id), new_type, parent_id)
                recommendation = {
                    "new_type": new_type, "parent_id": parent_id,
                    "preview": preview,
                }
            except (TypeError, ValueError):
                action = "uncertain"
                recommendation = None
        if action == "restructure" and confidence < .60:
            action = "uncertain"
        return {
            "goal_id": int(goal_id), "action": action,
            "confidence": round(confidence, 3),
            "rationale": str(raw.get("rationale") or "").strip()[:700],
            "question": str(raw.get("question") or "").strip()[:300],
            "current": {"type": node["type"], "path": context["path"]},
            "recommendation": recommendation,
        }
    finally:
        store.close()


def recommend_goal_tree_restructure(config, planner, goal_id: int,
                                    *, max_nodes: int = 80) -> dict:
    """Review the selected ancestor path and subtree as one bounded structure."""
    store = GoalStore(config.memory_db_path)
    try:
        selected = store.get(int(goal_id))
        if not selected or selected["type"] == "umbrella" or selected["status"] == "archived":
            raise ValueError("an active Root, Branch, or Leaf is required")
        soul_node = store.get(store.root_id)
        soul = {"id": store.root_id, "title": soul_node["title"],
                "description": soul_node.get("description", "")}
        ancestor_ids: list[int] = []
        current = selected
        while current and current["type"] != "umbrella":
            ancestor_ids.append(int(current["id"]))
            current = store.get(int(current["parent_id"])) if current.get("parent_id") else None
        ancestor_ids.reverse()
        scope_root_id = next((node_id for node_id in ancestor_ids
                              if store.get(node_id)["type"] == "overgoal"), ancestor_ids[0])
        subtree_ids = store._subtree_ids(int(goal_id))
        review_ids = list(dict.fromkeys([*ancestor_ids, *subtree_ids]))[:max_nodes]

        tree = store.tree()
        rendered: dict[int, dict] = {}
        pending = [tree]
        while pending:
            node = pending.pop()
            rendered[int(node["id"])] = node
            pending.extend(reversed(node.get("children", [])))
        review_nodes = []
        for node_id in review_ids:
            node = rendered.get(node_id) or store.get(node_id)
            if not node:
                continue
            review_nodes.append({
                "id": node_id, "type": node["type"], "parent_id": node["parent_id"],
                "title": node["title"],
                "description": str(node.get("description") or "")[:700],
                "path": " › ".join(store._goal_path_titles(node_id)),
                "semantic_role": node.get("semantic_role"),
                "semantic_role_source": node.get("semantic_role_source", ""),
                "child_ids": [int(child["id"]) for child in node.get("children", [])
                              if child.get("status") != "archived"][:20],
            })
        candidates = [{"id": store.root_id, "node_type": "umbrella", "type_label": "Soul",
                       "title": soul["title"], "path": soul["title"]}]
        for entry in store.catalog(max_nodes + 20):
            if entry["status"] != "active" or entry["type"] not in {"Root", "Branch"}:
                continue
            candidate = store.get(entry["id"])
            candidates.append({"id": int(entry["id"]), "node_type": candidate["type"],
                               "type_label": entry["type"], "title": entry["title"],
                               "path": entry["path"],
                               "description": str(candidate.get("description") or "")[:500]})
        review = {"selected_id": int(goal_id), "scope_root_id": int(scope_root_id),
                  "selected_path_ids": ancestor_ids, "nodes": review_nodes}
        raw = planner.recommend_tree_restructure(review, candidates, soul) or {}
        action = str(raw.get("action") or "uncertain").strip().lower()
        if action not in {"keep", "restructure", "uncertain"}:
            action = "uncertain"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        structural, roles = [], []
        allowed_goal_ids = set(review_ids)
        allowed_parent_ids = {int(item["id"]) for item in candidates} | allowed_goal_ids
        by_id = {int(item["id"]): item for item in review_nodes}
        if action == "restructure":
            for raw_node in list(raw.get("nodes") or [])[:16]:
                try:
                    node_id = int(raw_node.get("goal_id"))
                    parent_id = int(raw_node.get("parent_id"))
                except (TypeError, ValueError):
                    continue
                if node_id not in allowed_goal_ids or parent_id not in allowed_parent_ids:
                    continue
                current_node = by_id.get(node_id)
                if not current_node:
                    continue
                new_type = str(raw_node.get("new_type") or current_node["type"]).strip().lower()
                reason = str(raw_node.get("reason") or "")[:700]
                if (new_type != current_node["type"] or
                        parent_id != int(current_node.get("parent_id") or 0)):
                    structural.append({"goal_id": node_id, "new_type": new_type,
                                       "parent_id": parent_id, "reason": reason})
                role = str(raw_node.get("semantic_role") or "").strip().lower()
                locked_sources = {"user", "chat", "manual", "approved_restructure"}
                if (role and role != str(current_node.get("semantic_role") or "") and
                        str(current_node.get("semantic_role_source") or "") in locked_sources):
                    raw.setdefault("warnings", []).append(
                        f"Kept approved role for {current_node['title']}; change it manually if intent changed.")
                    role = str(current_node.get("semantic_role") or "")
                if new_type == "subgoal" and role in {"area", "project", "stage"}:
                    roles.append({
                        "goal_id": node_id, "role": role, "reason": reason,
                        "nested_stage_justification": str(
                            raw_node.get("nested_stage_justification") or "")[:700],
                    })
            try:
                preview = store.restructure_batch_preview(structural, roles)
            except ValueError as error:
                action, preview = "uncertain", None
                raw["question"] = str(error)
            if action == "restructure" and confidence < .60:
                action = "uncertain"
        else:
            preview = None
        return {
            "goal_id": int(goal_id), "scope_id": int(scope_root_id), "action": action,
            "confidence": round(confidence, 3),
            "rationale": str(raw.get("rationale") or "")[:900],
            "question": str(raw.get("question") or "")[:300],
            "warnings": [str(value)[:300] for value in list(raw.get("warnings") or [])[:6]],
            "recommendation": (None if not preview else {
                "changes": structural, "role_updates": roles, "preview": preview}),
        }
    finally:
        store.close()


def recommend_goal_intake(config, planner, selected_id: int, text: str,
                          *, max_candidates: int = 80) -> dict:
    """Classify a plain-language addition without exposing node types to the user."""
    store = GoalStore(config.memory_db_path)
    try:
        selected = store.get(int(selected_id))
        text = " ".join(str(text or "").split())
        if (not selected or selected["status"] == "archived" or
                selected["type"] not in {"overgoal", "subgoal"}):
            raise ValueError("select an active Root, Area, Project, or Stage")
        if not text:
            raise ValueError("describe what you want to add")
        subtree_ids = set(store._subtree_ids(int(selected_id)))
        tree = store.tree()
        rendered: dict[int, dict] = {}
        pending = [tree]
        while pending:
            node = pending.pop()
            rendered[int(node["id"])] = node
            pending.extend(node.get("children", []))
        selected_rendered = rendered.get(int(selected_id)) or selected
        soul_node = rendered[store.root_id]
        soul = {"id": store.root_id, "title": soul_node["title"],
                "description": soul_node.get("description", "")}
        candidates = [{
            "id": store.root_id, "node_type": "umbrella", "semantic_role": None,
            "title": soul["title"], "description": soul["description"],
            "path": soul["title"], "can_parent": False,
        }]
        for entry in store.catalog(max_candidates):
            if entry["status"] != "active" or entry["id"] == store.root_id:
                continue
            node = rendered.get(int(entry["id"])) or store.get(int(entry["id"]))
            candidates.append({
                "id": int(entry["id"]), "node_type": node["type"],
                "semantic_role": node.get("semantic_role"),
                "title": entry["title"], "path": entry["path"],
                "description": (str(node.get("description") or "")[:500]
                                if int(entry["id"]) in subtree_ids else ""),
                "can_parent": (int(entry["id"]) in subtree_ids and
                               node["type"] in {"overgoal", "subgoal"}),
            })
        if not any(item["id"] == int(selected_id) for item in candidates):
            candidates.append({
                "id": int(selected_id), "node_type": selected_rendered["type"],
                "semantic_role": selected_rendered.get("semantic_role"),
                "title": selected["title"],
                "path": " › ".join(store._goal_path_titles(int(selected_id))),
                "description": str(selected_rendered.get("description") or "")[:500],
                "can_parent": True,
            })
        selected_context = next(item for item in candidates if item["id"] == int(selected_id))
        raw = planner.classify_intake(text, selected_context, candidates, soul) or {}
        action = str(raw.get("action") or "uncertain").strip().lower()
        if action not in {"propose", "existing", "uncertain"}:
            action = "uncertain"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        if action == "existing":
            try:
                existing_id = int(raw.get("existing_goal_id"))
            except (TypeError, ValueError):
                existing_id = 0
            existing = rendered.get(existing_id)
            if not existing or existing.get("status") == "archived":
                action = "uncertain"
            else:
                return {
                    "action": "existing", "confidence": round(confidence, 3),
                    "rationale": str(raw.get("rationale") or "")[:700],
                    "question": "", "existing_goal_id": existing_id,
                    "existing_title": existing["title"],
                    "existing_path": " › ".join(store._goal_path_titles(existing_id)),
                    "recommendation": None,
                }
        recommendation = None
        if action == "propose":
            try:
                parent_id = int(raw.get("parent_id"))
                new_type = str(raw.get("new_type") or "").strip().lower()
            except (TypeError, ValueError):
                parent_id, new_type = 0, ""
            valid_parent_ids = {
                item["id"] for item in candidates if item.get("can_parent")}
            if new_type == "overgoal":
                valid_parent_ids.add(store.root_id)
            role = str(raw.get("semantic_role") or "").strip().lower() or None
            nested_stage_justification = str(
                raw.get("nested_stage_justification") or "")[:700]
            if (parent_id not in valid_parent_ids or
                    new_type not in {"overgoal", "subgoal", "task"}):
                action = "uncertain"
            else:
                try:
                    store._validate_parent(new_type, parent_id)
                except ValueError:
                    action = "uncertain"
                if new_type == "subgoal":
                    parent = rendered.get(parent_id) or store.get(parent_id)
                    if role not in {"area", "project", "stage"}:
                        role = _derived_branch_role(
                            str(raw.get("title") or text), str(raw.get("description") or text),
                            parent_type=parent["type"],
                            parent_role=str(parent.get("semantic_role") or ""))
                else:
                    role = None
                try:
                    store._validate_semantic_placement(
                        new_type, role, parent_id,
                        nested_stage_justification=nested_stage_justification)
                except ValueError:
                    action = "uncertain"
                if action == "propose":
                    title = str(raw.get("title") or text).strip()[:160]
                    description = str(raw.get("description") or text).strip()[:1200]
                    if not title:
                        action = "uncertain"
                    else:
                        parent_path = store._goal_path_titles(parent_id)
                        recommendation = {
                            "selected_id": int(selected_id), "parent_id": parent_id,
                            "new_type": new_type, "semantic_role": role,
                            "nested_stage_justification": nested_stage_justification,
                            "title": title, "description": description,
                            "proposed_path": " › ".join([*parent_path, title]),
                        }
        if action == "propose" and confidence < .58:
            action, recommendation = "uncertain", None
        return {
            "action": action, "confidence": round(confidence, 3),
            "rationale": str(raw.get("rationale") or "")[:700],
            "question": str(raw.get("question") or "")[:300],
            "existing_goal_id": None, "recommendation": recommendation,
        }
    finally:
        store.close()


def propose_goal_intake(config, recommendation: dict, rationale: str = "") -> dict:
    """Turn one validated intake classification into an approval-only proposal."""
    from .goal_ai import AgentProposal, GoalAgentStore
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        parent_id = int(recommendation.get("parent_id"))
        new_type = str(recommendation.get("new_type") or "").strip().lower()
        title = str(recommendation.get("title") or "").strip()[:160]
        description = str(recommendation.get("description") or "").strip()[:1200]
        role = str(recommendation.get("semantic_role") or "").strip().lower() or None
        nested_stage_justification = str(
            recommendation.get("nested_stage_justification") or "")[:700]
        parent = goals.get(parent_id)
        if not parent or parent["status"] == "archived" or not title:
            raise ValueError("classified addition has an invalid destination")
        goals._validate_parent(new_type, parent_id)
        if new_type == "subgoal" and role not in {"area", "project", "stage"}:
            raise ValueError("classified Branch needs an Area, Project, or Stage role")
        if new_type != "subgoal":
            role = None
        goals._validate_semantic_placement(
            new_type, role, parent_id,
            nested_stage_justification=nested_stage_justification)
        if new_type == "task":
            goals.validate_leaf_candidate(
                parent_id, title, description=description,
                reservations=agents.leaf_reservations(parent_id),
                horizon=min(2, max(1, int(getattr(
                    config, "goal_ai_leaf_horizon", 2)))))
        payload = {
            "type": new_type, "title": title, "description": description,
            "priority": "normal", "semantic_role": role,
            "nested_stage_justification": nested_stage_justification,
            "source": "plain_language_intake",
        }
        reason = str(rationale or "").strip() or (
            f"Create this beneath {parent['title']} after reviewing Faerie's classification.")
        proposal_id = agents.add_proposal(
            parent_id, AgentProposal("create_child", parent_id, payload, reason))
        if proposal_id is None:
            raise ValueError("the same addition is already proposed or was dismissed")
        return {"proposal_id": proposal_id, "parent_id": parent_id,
                "recommendation": {**payload, "proposed_path":
                                   " › ".join([*goals._goal_path_titles(parent_id), title])}}
    finally:
        agents.close(); goals.close()


def start_planning(store: GoalStore, planner, source_item_id: int,
                   target_parent_id: int | None = None,
                   placement: dict | None = None) -> dict:
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
        if not linked:
            raise ValueError("placement review is required before planning this suggestion")
        target_parent_id = int(linked["id"])
    target = store.get(target_parent_id or store.root_id)
    if not target or target["type"] not in {"umbrella", "overgoal", "subgoal"}:
        raise ValueError("planning target must be an active Soul, Root, or Branch")
    placement = dict(placement or {})
    if target["type"] == "umbrella":
        pending = placement.get("mode") == "pending" and not placement.get("user_confirmed")
        approved_root = (placement.get("mode") == "new_root" and
                         placement.get("root_eligible") and
                         str(placement.get("root_title") or "").strip() and
                         str(placement.get("root_description") or "").strip())
        if not pending and not approved_root:
            raise ValueError("a new Root requires an approved durable life-domain placement")
    else:
        placement.update({
            "mode": "existing", "parent_id": int(target["id"]),
            "parent_path": " › ".join(store._goal_path_titles(int(target["id"]))),
        })
    message, draft = planner.first(suggestion, target, placement)
    draft = dict(draft or {})
    draft["_placement"] = placement
    return store.start_plan(int(source_item_id), target["id"], message, draft)


def suggestion_leaf_overlaps(config, source_item_id: int, *, limit: int = 5) -> dict:
    """Find existing goal nodes that may already serve an Investigation suggestion.

    The public name is retained for bridge compatibility. Provenance belongs only
    to the node that was actually implemented from a suggestion; it must not be
    inherited by every descendant, which would make unrelated Leaves tie.
    """
    from .inference import concept_similarity
    store = GoalStore(config.memory_db_path)
    try:
        row = store.conn.execute(
            "SELECT kind,status,text,curiosity_id FROM curiosity_item WHERE id=?",
            (int(source_item_id),)).fetchone()
        if not row or row["kind"] != "suggestion" or row["status"] != "open":
            raise ValueError("only an open suggestion can be reviewed")
        suggestion = crypto.dec(row["text"]) or ""
        tables = {r["name"] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        def count_rows(table: str, column: str, node_ids: list[int]) -> int:
            if table not in tables:
                return 0
            placeholders = ",".join("?" for _ in node_ids)
            return int(store.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})",
                node_ids).fetchone()[0])

        matches = []
        for entry in store.catalog(300):
            if entry["type"] == "Soul" or entry["status"] in {"archived", "completed"}:
                continue
            node = store.get(entry["id"])
            comparison = "\n".join(filter(None, [
                node.get("title", ""), node.get("description", ""), node.get("notes", "")]))
            leaf_score = concept_similarity(suggestion, comparison)

            # A concise renamed node can look lexically different from the
            # suggestion that created it, so include direct provenance. Do not
            # borrow an ancestor's provenance: that was the source of every
            # child in one plan appearing as the same 48% match.
            origin_ids = {
                int(r["id"]) for r in store.conn.execute(
                    "SELECT id FROM curiosity_item WHERE implementation_goal_id=?",
                    (int(node["id"]),))
            }
            for evidence in store.conn.execute(
                    "SELECT source_id FROM goal_evidence_link WHERE goal_id=? "
                    "AND source_kind='curiosity_suggestion'", (int(node["id"]),)):
                try:
                    origin_ids.add(int(evidence["source_id"]))
                except (TypeError, ValueError):
                    continue
            origin_score = 0.0
            origin_text = ""
            if origin_ids:
                origin_placeholders = ",".join("?" for _ in origin_ids)
                for origin in store.conn.execute(
                        f"SELECT id,text FROM curiosity_item WHERE id IN ({origin_placeholders})",
                        sorted(origin_ids)):
                    if int(origin["id"]) == int(source_item_id):
                        continue
                    text = crypto.dec(origin["text"]) or ""
                    candidate_score = concept_similarity(suggestion, text)
                    if candidate_score > origin_score:
                        origin_score, origin_text = candidate_score, text
            score = max(leaf_score, origin_score)
            if score < .28:
                continue
            matched_via = "originating_suggestion" if origin_score > leaf_score else "leaf"
            scope_ids, pending_ids = [int(node["id"])], [int(node["id"])]
            while pending_ids:
                placeholders = ",".join("?" for _ in pending_ids)
                children = [int(r["id"]) for r in store.conn.execute(
                    f"SELECT id FROM goal_node WHERE parent_id IN ({placeholders}) "
                    "AND status!='archived'", pending_ids)]
                scope_ids.extend(children)
                pending_ids = children
            history_counts = {
                "coach_messages": count_rows("goal_step_coach_message", "node_id", scope_ids),
                "coach_steps": count_rows("goal_step_coach_state", "node_id", scope_ids),
                "outcomes": count_rows("experiment_outcome", "goal_id", scope_ids),
                "evidence": count_rows("goal_evidence_link", "goal_id", scope_ids),
                "children": len(scope_ids) - 1,
            }
            matches.append({
                "goal_id": int(node["id"]), "title": node["title"],
                "node_type": node["type"], "type_label": entry["type"],
                "path": entry["path"], "description": node.get("description", ""),
                "notes": node.get("notes", ""), "similarity": round(score, 3),
                "leaf_similarity": round(leaf_score, 3),
                "origin_similarity": round(origin_score, 3),
                "matched_via": matched_via,
                "origin_suggestion": origin_text if matched_via == "originating_suggestion" else "",
                "history_counts": history_counts,
                "overlap": "strong" if score >= .65 else ("possible" if score >= .42 else "light"),
            })
        matches.sort(key=lambda item: item["similarity"], reverse=True)
        return {"item_id": int(source_item_id), "suggestion": suggestion,
                "matches": matches[:max(1, min(8, int(limit)))]}
    finally:
        store.close()


def propose_goal_restructure(config, goal_id: int, new_type: str, parent_id: int,
                             position: int | None = None, rationale: str = "",
                             semantic_role: str | None = None) -> dict:
    """Stage an identity-preserving structural migration for explicit approval."""
    from .goal_ai import AgentProposal, GoalAgentStore
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        preview = goals.restructure_preview(
            int(goal_id), str(new_type), int(parent_id), position, semantic_role,
            nested_stage_justification=str(rationale or ""))
        payload = {
            "new_type": preview["proposed"]["type"],
            "semantic_role": preview["proposed"].get("semantic_role"),
            "parent_id": preview["proposed"]["parent_id"],
            "position": preview["proposed"]["position"],
            "current": preview["current"], "proposed": preview["proposed"],
            "retained_counts": preview["retained_counts"],
            "node_id_preserved": True,
        }
        reason = str(rationale or "").strip() or (
            f"Reclassify this {preview['current']['type_label']} as a "
            f"{preview['proposed']['type_label']} and move it in place without losing history.")
        proposal_id = agents.add_proposal(
            int(goal_id), AgentProposal(
                "restructure_node", int(goal_id), payload, reason))
        if proposal_id is None:
            raise ValueError("the same restructure proposal is already open or was dismissed")
        return {"proposal_id": proposal_id, "preview": preview}
    finally:
        agents.close(); goals.close()


def propose_goal_tree_restructure(config, scope_id: int, changes: list[dict],
                                  role_updates: list[dict] | None = None,
                                  rationale: str = "") -> dict:
    """Stage one atomic, identity-preserving path normalization for approval."""
    from .goal_ai import AgentProposal, GoalAgentStore
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        scope = goals.get(int(scope_id))
        if not scope or scope["type"] not in {"overgoal", "subgoal", "task"}:
            raise ValueError("active review scope not found")
        preview = goals.restructure_batch_preview(list(changes or []), list(role_updates or []))
        watched_ids = set(preview["affected_node_ids"])
        for item in preview["structural_changes"]:
            watched_ids.add(int(item["current"]["parent_id"]))
            watched_ids.add(int(item["proposed"]["parent_id"]))
        versions = {}
        for node_id in sorted(value for value in watched_ids if value):
            node = goals.get(node_id)
            if node:
                versions[str(node_id)] = node["updated_at"]
        payload = {
            "changes": [{"goal_id": item["goal_id"], "new_type": item["new_type"],
                         "parent_id": item["parent_id"], "position": item["position"],
                         "reason": item.get("reason", "")}
                        for item in preview["structural_changes"]],
            "role_updates": [{"goal_id": item["goal_id"],
                              "role": item["proposed_role"],
                              "reason": item.get("reason", "")}
                             for item in preview["role_changes"]],
            "preview": preview, "expected_versions": versions,
            "node_ids_preserved": True,
        }
        reason = str(rationale or "").strip() or (
            "Normalize this Growth path while preserving every node and its attached history.")
        proposal_id = agents.add_proposal(
            int(scope_id), AgentProposal("restructure_tree", int(scope_id), payload, reason))
        if proposal_id is None:
            raise ValueError("the same whole-tree restructure is already open or was dismissed")
        return {"proposal_id": proposal_id, "preview": preview, "scope_id": int(scope_id)}
    finally:
        agents.close(); goals.close()


def propose_suggestion_leaf_update(config, source_item_id: int, goal_id: int,
                                   title: str, description: str) -> dict:
    """Create a pending GoalAI update; never mutate the matched Leaf directly."""
    from .goal_ai import AgentProposal, GoalAgentStore
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        row = goals.conn.execute(
            "SELECT kind,status,text FROM curiosity_item WHERE id=?",
            (int(source_item_id),)).fetchone()
        node = goals.get(int(goal_id))
        if not row or row["kind"] != "suggestion" or row["status"] != "open":
            raise ValueError("only an open suggestion can update a Leaf")
        if not node or node["type"] == "umbrella" or node["status"] == "archived":
            raise ValueError("active Root, Branch, or Leaf not found")
        title = " ".join(str(title or "").split())
        description = str(description or "").strip()
        if not title or not description:
            raise ValueError("title and adapted direction are required")
        suggestion = crypto.dec(row["text"]) or ""
        # Re-clicking Adapt after editing must replace the prior draft for the
        # same suggestion+node, not stack multiple open proposals.
        replaced = 0
        for pending in agents.conn.execute(
                "SELECT id,payload_json FROM goal_agent_proposal WHERE target_node_id=? "
                "AND proposal_type='update_fields' AND status='open'", (int(goal_id),)):
            try:
                pending_payload = json.loads(crypto.dec(pending["payload_json"]) or "{}")
            except (TypeError, json.JSONDecodeError):
                pending_payload = {}
            if str(pending_payload.get("source_curiosity_item_id")) == str(source_item_id):
                agents.conn.execute(
                    "UPDATE goal_agent_proposal SET status='stale',resolved_at=? WHERE id=?",
                    (_now(), int(pending["id"])))
                replaced += 1
        if replaced:
            agents.conn.commit()
        from .inference import concept_similarity
        similarity = concept_similarity(
            suggestion, "\n".join([node["title"], node.get("description", ""), node.get("notes", "")]))
        proposal_id = agents.add_proposal(
            int(goal_id), AgentProposal(
                "update_fields", int(goal_id),
                {"title": title, "description": description,
                 "source_curiosity_item_id": int(source_item_id)},
                f"Adapt this existing goal node in place from an Investigation suggestion "
                f"instead of creating overlapping work ({similarity:.0%} overlap)."))
        if proposal_id is None:
            raise ValueError("the same Leaf update is already pending or was dismissed")
        return {"proposal_id": proposal_id, "goal_id": int(goal_id),
                "similarity": round(similarity, 3), "replaced_open_proposals": replaced}
    finally:
        agents.close(); goals.close()


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
    # Planning chat / investigation steps award XP
    try:
        from .curiosity_metrics import MetricStore
        ms = MetricStore(store.db_path)
        try:
            cnt = store.conn.execute(
                "SELECT COUNT(*) c FROM goal_plan_message WHERE session_id=?",
                (int(session_id),)
            ).fetchone()["c"]
            ms.award_xp(0, "chat_turn", f"goal-plan:{session_id}:{int(cnt)}", xp=None, confidence=0.65)
        finally:
            ms.close()
    except Exception:
        pass
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
