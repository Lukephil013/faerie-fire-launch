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


def _derived_branch_role(title: str, description: str, *, parent_type: str,
                         parent_role: str = "", has_branch_children: bool = False) -> str:
    """Choose a presentation role for legacy Branches without persisting an inference."""
    text = f"{title} {description}".casefold()
    words = set(re.findall(r"[\w]+", text))
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
        if words & project_words:
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
                           source: str = "ai", commit: bool = True) -> None:
        node = self.get(int(goal_id))
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

    def _validate_semantic_placement(self, node_type: str, semantic_role: str | None,
                                     parent_id: int, *,
                                     nested_stage_justification: str = "") -> None:
        """Prevent accidental Stage → Stage chains while retaining explicit substages."""
        role = str(semantic_role or "").strip().lower() or None
        if str(node_type) != "subgoal" or role != "stage":
            return
        if self.resolved_semantic_role(int(parent_id)) != "stage":
            return
        explanation = " ".join(str(nested_stage_justification or "").split())
        if len(explanation) < 20:
            raise ValueError(
                "placing a Stage beneath another Stage requires an explicit "
                "macro-stage/substage justification")

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
            group["recent_done"].sort(key=lambda l: l.get("completed_at") or "",
                                      reverse=True)
            projects.append({
                "project_id": parent_id,
                "project_title": parent["title"],
                "path": " › ".join(self._goal_path_titles(parent_id)),
                "project_status": parent.get("status"),
                "open": group["open"][:max_leaves],
                "recent_done": group["recent_done"][:max_leaves],
            })
        # Projects with in-flight work first, then those needing a next step.
        projects.sort(key=lambda p: (not p["open"], not p["recent_done"],
                                     p["project_id"]))
        return projects[:max_projects]

    def open_leaf_count(self, parent_id: int) -> int:
        """Open (active or paused) Leaves directly under one node."""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM goal_node WHERE parent_id=? "
            "AND node_type='task' AND status IN ('active','paused')",
            (int(parent_id),)).fetchone()[0])

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
        role_labels = {"area": "Area", "project": "Project", "stage": "Stage"}
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
        for goal_id, state in final.items():
            if state["type"] != "subgoal" or final_roles.get(goal_id) != "stage":
                continue
            parent_id = int(state.get("parent_id") or 0)
            if (not parent_id or final.get(parent_id, {}).get("type") != "subgoal" or
                    final_roles.get(parent_id) != "stage"):
                continue
            original_parent = int(nodes[goal_id].get("parent_id") or 0)
            already_nested = (
                nodes[goal_id]["type"] == "subgoal" and
                display_roles.get(goal_id) == "stage" and
                original_parent == parent_id and
                display_roles.get(original_parent) == "stage")
            if already_nested:
                continue
            justification = str(
                role_inputs.get(goal_id, {}).get("nested_stage_justification") or "")
            if len(" ".join(justification.split())) < 20:
                raise ValueError(
                    "placing a Stage beneath another Stage requires an explicit "
                    "macro-stage/substage justification")
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
                    source="ai", commit=False)
            self._mark_goal_ai_dirty(*preview["affected_node_ids"], *touched_parents)
            if commit:
                self.conn.commit()
        except Exception:
            if commit and (started or self.conn.in_transaction):
                self.conn.rollback()
            raise
        return preview

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
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "DELETE FROM goal_archive_snapshot WHERE archive_root_id=?", (int(goal_id),))
            self.conn.executemany(
                "INSERT INTO goal_archive_snapshot "
                "(archive_root_id,goal_id,prior_status,archived_at) VALUES (?,?,?,?)",
                [(int(goal_id), int(row["id"]), row["status"], now) for row in rows])
            self.conn.executemany(
                "UPDATE goal_node SET status='archived',updated_at=? WHERE id=?",
                [(now, node_id) for node_id in ids])
            self._mark_goal_ai_dirty(*ids, node.get("parent_id"))
            self.conn.commit()
        except Exception:
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
            "SELECT draft_json FROM goal_plan_session WHERE id=?",
            (int(session_id),)).fetchone()
        if row:
            try:
                existing = json.loads(crypto.dec(row["draft_json"]) or "{}")
            except json.JSONDecodeError:
                existing = {}
            if existing.get("_placement"):
                draft = dict(draft or {})
                draft["_placement"] = existing["_placement"]
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
        nodes = list(session["draft"].get("nodes") or [])
        if not nodes:
            raise ValueError("draft has no goals to create")
        target = self.get(session["target_parent_id"])
        placement = session["draft"].get("_placement") or {}
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
            new_id = self.create(
                node_type, str(raw.get("title") or "").strip(), parent_id=parent_id,
                description=str(raw.get("description") or ""),
                notes=str(raw.get("notes") or ""),
                priority=priority,
                due_date=raw.get("due_date"), _commit=False)
            for child in children:
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
                next_proposal_id = agents.add_proposal(
                    int(node["parent_id"]), AgentProposal(
                        "create_child", int(node["parent_id"]),
                        {"type": "task", "title": title,
                         "description": (f"Proposed after outcome #{outcome['id']}: "
                                         f"{observation}")[:1000],
                         "outcome_id": outcome["id"]},
                        f"The prior experiment suggested this adjustment: {title}"))
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
Ask exactly one decision-bearing question at a time. Briefly recommend a current
approach, then ask the question. Never activate goals yourself. Return strict JSON:
{"message": str, "draft": {"rationale": str, "nodes": [goal nodes]}}.
Goal nodes use type overgoal|subgoal|task, title, description, priority
low|normal|high, due_date YYYY-MM-DD or null, and children. Tasks have no children.
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
Normally normalize Stage → Stage to Project → Stage or Stage → Leaf. Keep nested Stages only when
the child entry explicitly explains the macro-stage/substage distinction.
"""

SUMMARY_SYSTEM = """Turn this planning dialogue into one concise editable goal tree.
Use the user's decisions as authoritative. Return strict JSON:
{"summary": str, "draft": {"rationale": str, "nodes": [goal nodes]}}.
The first node must fit below the supplied target: overgoal below umbrella,
otherwise subgoal below an overgoal/subgoal. Never use type "umbrella".
Include concrete tasks when known; do not invent dates, and derive any dates
from TODAY in the prompt. Nodes use type, title, description, priority,
due_date, children. Keep descriptions short so the JSON reply never exceeds
~1500 tokens.
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
        return ("That gives the plan a finish line. I would start with one small experiment "
                "and one review task. What is the first concrete action you want to take?", draft)

    def summarize(self, session: dict, target: dict) -> tuple[str, dict]:
        suggestion = session.get("draft", {}).get("rationale") or "Implement the idea"
        success = session.get("draft", {}).get("success") or "Define a useful outcome"
        placement = session.get("draft", {}).get("_placement") or {}
        project = {"type": "subgoal", "title": suggestion[:80], "description": success,
                   "priority": "normal", "due_date": None, "children": [{
                       "type": "task", "title": "Take the first concrete step",
                       "description": "", "priority": "normal", "due_date": None,
                       "children": [],
                   }]}
        if target["type"] == "umbrella":
            nodes = [{"type": "overgoal", "title": placement.get("root_title", "New life domain"),
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

    def _call(self, system: str, prompt: str) -> dict:
        log_diag("prompt", f"surface=goal-planner model={self.model} input_chars={len(prompt)}")
        msg = self.client.messages.create(
            model=self.model, max_tokens=4000, system=system,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if getattr(msg, "stop_reason", None) == "max_tokens":
            log_diag("prompt", f"surface=goal-planner model={self.model} "
                     "reply truncated at max_tokens=4000")
        data = _json_object(text)
        if not data:
            raise ValueError("the planner reply could not be read — nothing was "
                             "saved, please try again")
        return data

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
                          f"SUGGESTION: {suggestion}")
        return str(data.get("message") or "What outcome do you want?"), data.get("draft") or {}

    def reply(self, session: dict, answer: str, target: dict) -> tuple[str, dict]:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session["messages"])
        data = self._call(PLANNER_SYSTEM,
                          f"TODAY: {self._today()}\nTARGET TYPE: {target['type']}\n"
                          f"DRAFT: {json.dumps(session['draft'])}"
                          f"\nDIALOGUE:\n{transcript}\nuser: {answer}")
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
                                     *, max_candidates: int = 80) -> dict:
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
            candidates.append({
                "id": int(entry["id"]), "node_type": node["type"],
                "type_label": entry["type"], "title": entry["title"],
                "path": entry["path"],
                "description": str(node.get("description") or "")[:700],
            })
            if len(candidates) >= max_candidates:
                break
        raw = planner.place(suggestion, candidates, soul) or {}
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
        if (placement.get("mode") != "new_root" or
                not placement.get("root_eligible") or
                not str(placement.get("root_title") or "").strip() or
                not str(placement.get("root_description") or "").strip()):
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
