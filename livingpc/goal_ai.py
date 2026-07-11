"""Hierarchical GoalAI agents with bounded context and proposal-only authority."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import crypto
from .db import connect as db_connect
from .diagnostics import log_diag
from .goals import GoalStore


HEALTH_STATES = {"unknown", "on-track", "needs-attention", "blocked"}
PROMOTION_CONFIDENCE_GATE = 0.8
PROPOSAL_TYPES = {
    "create_child", "update_fields", "pause", "archive",
    "request_evidence", "start_curiosity", "promote_insight",
}
ACTIVE_AGENT_STATUSES = {"active"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize_priority(value) -> str:
    raw = str(value or "normal").strip().lower()
    return {"medium": "normal", "default": "normal", "urgent": "high"}.get(
        raw, raw if raw in {"low", "normal", "high"} else "normal")


def _normalize_node_type(value, parent_type: str | None = None) -> str:
    raw = str(value or "").strip().lower()
    mapped = {"root": "overgoal", "branch": "subgoal", "leaf": "task"}.get(raw, raw)
    if mapped in {"overgoal", "subgoal", "task"}:
        return mapped
    return "overgoal" if parent_type == "umbrella" else "task"


def _fallback_bullets(text: str, limit: int = 6) -> list[str]:
    pieces = re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", " ".join(str(text or "").split()))
    bullets = []
    for piece in pieces:
        cleaned = piece.strip(" -•\t")
        if len(cleaned) < 8:
            continue
        bullets.append(cleaned[:240].rstrip() + ("…" if len(cleaned) > 240 else ""))
        if len(bullets) >= limit:
            break
    return bullets or ["The user supplied detailed context; consult the exact encrypted answer."]


SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_agent_state (
    node_id INTEGER PRIMARY KEY,
    health TEXT NOT NULL DEFAULT 'unknown',
    confidence REAL NOT NULL DEFAULT 0,
    brief TEXT,
    evidence_summary TEXT,
    blockers TEXT,
    next_focus TEXT,
    dirty INTEGER NOT NULL DEFAULT 1,
    dirty_reason TEXT,
    deferred INTEGER NOT NULL DEFAULT 0,
    due_state TEXT NOT NULL DEFAULT 'none',
    last_run_at TEXT,
    last_context_hash TEXT,
    last_error_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (health IN ('unknown','on-track','needs-attention','blocked')),
    FOREIGN KEY (node_id) REFERENCES goal_node(id)
);

CREATE TABLE IF NOT EXISTS goal_agent_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    health TEXT NOT NULL,
    confidence REAL NOT NULL,
    report_json TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES goal_node(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_agent_assessment_node
ON goal_agent_assessment(node_id, id DESC);

CREATE TABLE IF NOT EXISTS goal_agent_question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    assessment_id INTEGER,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (status IN ('open','answered','dismissed')),
    FOREIGN KEY (node_id) REFERENCES goal_node(id),
    FOREIGN KEY (assessment_id) REFERENCES goal_agent_assessment(id)
);

CREATE TABLE IF NOT EXISTS goal_agent_proposal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_node_id INTEGER NOT NULL,
    target_node_id INTEGER NOT NULL,
    assessment_id INTEGER,
    proposal_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    rationale TEXT,
    fingerprint TEXT NOT NULL,
    target_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (status IN ('open','approved','dismissed','refined','stale')),
    FOREIGN KEY (agent_node_id) REFERENCES goal_node(id),
    FOREIGN KEY (target_node_id) REFERENCES goal_node(id),
    FOREIGN KEY (assessment_id) REFERENCES goal_agent_assessment(id)
);
CREATE INDEX IF NOT EXISTS idx_goal_agent_proposal_node
ON goal_agent_proposal(agent_node_id, status, id DESC);

CREATE TABLE IF NOT EXISTS goal_agent_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (role IN ('user','assistant')),
    FOREIGN KEY (node_id) REFERENCES goal_node(id)
);

CREATE TABLE IF NOT EXISTS goal_agent_memory_candidate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    message_id INTEGER,
    category TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    source_text TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    memory_id INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (status IN ('open','saved','dismissed')),
    FOREIGN KEY (node_id) REFERENCES goal_node(id),
    FOREIGN KEY (message_id) REFERENCES goal_agent_message(id)
);

CREATE TABLE IF NOT EXISTS goal_harvest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    draft_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    CHECK (status IN ('draft','committed','abandoned')),
    FOREIGN KEY (source_node_id) REFERENCES goal_node(id)
);
CREATE TABLE IF NOT EXISTS goal_harvest_route (
    harvest_id INTEGER NOT NULL,
    target_node_id INTEGER NOT NULL,
    insight_indexes TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (harvest_id,target_node_id),
    FOREIGN KEY (harvest_id) REFERENCES goal_harvest(id),
    FOREIGN KEY (target_node_id) REFERENCES goal_node(id)
);
"""


@dataclass
class AgentProposal:
    proposal_type: str
    target_node_id: int
    payload: dict = field(default_factory=dict)
    rationale: str = ""


@dataclass
class AgentReport:
    brief: str
    health: str = "unknown"
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_focus: str = ""
    questions: list[str] = field(default_factory=list)
    proposals: list[AgentProposal] = field(default_factory=list)


@dataclass
class ChatResult:
    reply: str
    proposals: list[AgentProposal] = field(default_factory=list)
    memory_candidate: dict | None = None


@dataclass
class HarvestDraft:
    summary: str
    insights: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)


class GoalAgentStore:
    def __init__(self, db_path: str, *, ensure: bool = True):
        self.db_path = db_path
        self.auto_ensure = bool(ensure)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        route_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(goal_harvest_route)").fetchall()}
        if "insight_indexes" not in route_cols:
            self.conn.execute("ALTER TABLE goal_harvest_route ADD COLUMN insight_indexes TEXT")
        state_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(goal_agent_state)").fetchall()}
        if "dirty_reason" not in state_cols:
            self.conn.execute("ALTER TABLE goal_agent_state ADD COLUMN dirty_reason TEXT")
        if "deferred" not in state_cols:
            self.conn.execute(
                "ALTER TABLE goal_agent_state ADD COLUMN deferred INTEGER NOT NULL DEFAULT 0")
        if "due_state" not in state_cols:
            self.conn.execute(
                "ALTER TABLE goal_agent_state ADD COLUMN due_state TEXT NOT NULL DEFAULT 'none'")
        if self.auto_ensure:
            self.ensure_agents()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def ensure_agents(self) -> None:
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO goal_agent_state (node_id,updated_at) "
            "SELECT id,? FROM goal_node", (now,))
        self.conn.commit()

    def _dec_json(self, value, fallback):
        try:
            return json.loads(crypto.dec(value) or "")
        except (TypeError, json.JSONDecodeError):
            return fallback

    def _missing_state(self, node_id: int) -> dict:
        exists = self.conn.execute(
            "SELECT 1 FROM goal_node WHERE id=?", (int(node_id),)).fetchone()
        if not exists:
            raise ValueError("goal agent not found")
        return {
            "node_id": int(node_id), "health": "unknown", "confidence": 0.0,
            "brief": "", "evidence": [], "blockers": [], "next_focus": "",
            "dirty": True, "dirty_reason": "new or changed", "deferred": False,
            "due_state": "none", "last_run_at": None, "last_error_at": None,
            "updated_at": None,
        }

    def state(self, node_id: int, *, ensure: bool | None = None) -> dict:
        should_ensure = self.auto_ensure if ensure is None else bool(ensure)
        if should_ensure:
            self.ensure_agents()
        row = self.conn.execute(
            "SELECT * FROM goal_agent_state WHERE node_id=?", (int(node_id),)).fetchone()
        if not row:
            return self._missing_state(int(node_id))
        return {
            "node_id": row["node_id"], "health": row["health"],
            "confidence": row["confidence"], "brief": crypto.dec(row["brief"]) or "",
            "evidence": self._dec_json(row["evidence_summary"], []),
            "blockers": self._dec_json(row["blockers"], []),
            "next_focus": crypto.dec(row["next_focus"]) or "", "dirty": bool(row["dirty"]),
            "dirty_reason": row["dirty_reason"] or ("new or changed" if row["dirty"] else ""),
            "deferred": bool(row["deferred"]), "due_state": row["due_state"] or "none",
            "last_run_at": row["last_run_at"], "last_error_at": row["last_error_at"],
            "updated_at": row["updated_at"],
        }

    def all_states(self) -> list[dict]:
        if self.auto_ensure:
            self.ensure_agents()
        return [self.state(row["id"], ensure=False) for row in self.conn.execute(
            "SELECT id FROM goal_node ORDER BY id")]

    def mark_dirty(self, node_id: int, *, ancestors: bool = True,
                   reason: str = "meaningful change") -> None:
        self.ensure_agents()
        current = int(node_id)
        while current:
            self.conn.execute(
                "UPDATE goal_agent_state SET dirty=1,dirty_reason=?,deferred=0,updated_at=? "
                "WHERE node_id=?", (str(reason)[:80], _now(), current))
            if not ancestors:
                break
            row = self.conn.execute(
                "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
            current = int(row["parent_id"]) if row and row["parent_id"] else 0
        self.conn.commit()

    def record_error(self, node_id: int) -> None:
        self.conn.execute(
            "UPDATE goal_agent_state SET last_error_at=?,dirty=1,updated_at=? WHERE node_id=?",
            (_now(), _now(), int(node_id)))
        self.conn.commit()

    def mark_due_date_boundaries(self, now: datetime | None = None) -> int:
        """Dirty a path once when an active node becomes due-soon or overdue."""
        local_day = (now or datetime.now().astimezone()).date()
        changed = 0
        rows = self.conn.execute(
            "SELECT g.id,g.due_date,s.due_state FROM goal_node g "
            "JOIN goal_agent_state s ON s.node_id=g.id WHERE g.status='active'"
        ).fetchall()
        for row in rows:
            raw = row["due_date"]
            state = "none"
            if raw:
                try:
                    days = (datetime.fromisoformat(raw).date() - local_day).days
                    state = "overdue" if days < 0 else ("due_soon" if days <= 3 else "future")
                except (TypeError, ValueError):
                    state = "none"
            previous = row["due_state"] or "none"
            self.conn.execute("UPDATE goal_agent_state SET due_state=? WHERE node_id=?",
                              (state, int(row["id"])))
            if state in {"due_soon", "overdue"} and state != previous:
                self.mark_dirty(int(row["id"]), reason=f"date became {state.replace('_', ' ')}")
                changed += 1
        self.conn.commit()
        return changed

    def save_report(self, node_id: int, report: AgentReport, context_hash: str,
                    model: str, *, proposal_cap: int = 3) -> dict:
        if report.health not in HEALTH_STATES:
            raise ValueError("invalid GoalAI health state")
        now = _now()
        payload = {
            "brief": report.brief, "health": report.health,
            "confidence": report.confidence, "evidence": report.evidence,
            "blockers": report.blockers, "next_focus": report.next_focus,
            "questions": report.questions,
            "proposals": [{"type": p.proposal_type, "target_node_id": p.target_node_id,
                           "payload": p.payload, "rationale": p.rationale}
                          for p in report.proposals],
        }
        previous = self.state(node_id)
        cur = self.conn.execute(
            "INSERT INTO goal_agent_assessment "
            "(node_id,health,confidence,report_json,context_hash,model,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(node_id), report.health, report.confidence,
             crypto.enc(_json(payload)), context_hash, model, now))
        assessment_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE goal_agent_state SET health=?,confidence=?,brief=?,evidence_summary=?,"
            "blockers=?,next_focus=?,dirty=0,dirty_reason=NULL,deferred=0,last_run_at=?,last_context_hash=?,"
            "last_error_at=NULL,updated_at=? WHERE node_id=?",
            (report.health, report.confidence, crypto.enc(report.brief),
             crypto.enc(_json(report.evidence)), crypto.enc(_json(report.blockers)),
             crypto.enc(report.next_focus), now, context_hash, now, int(node_id)))
        for question in report.questions:
            text = str(question).strip()
            if text and not self._question_exists(node_id, text):
                self.conn.execute(
                    "INSERT INTO goal_agent_question "
                    "(node_id,assessment_id,text,status,created_at) VALUES (?,?,?,'open',?)",
                    (int(node_id), assessment_id, crypto.enc(text), now))
        created = 0
        open_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM goal_agent_proposal WHERE agent_node_id=? AND status='open'",
            (int(node_id),)).fetchone()[0])
        for proposal in report.proposals:
            if open_count >= proposal_cap:
                break
            if self.add_proposal(node_id, proposal, assessment_id=assessment_id,
                                 commit=False):
                created += 1
                open_count += 1
        self.conn.commit()
        return {"assessment_id": assessment_id, "proposals_created": created,
                "became_blocked": previous["health"] != "blocked" and report.health == "blocked"}

    def _question_exists(self, node_id: int, text: str) -> bool:
        normalized = " ".join(text.lower().split())
        rows = self.conn.execute(
            "SELECT text FROM goal_agent_question WHERE node_id=? AND status IN ('open','answered')",
            (int(node_id),)).fetchall()
        return any(" ".join((crypto.dec(row["text"]) or "").lower().split()) == normalized
                   for row in rows)

    def add_proposal(self, agent_node_id: int, proposal: AgentProposal, *,
                     assessment_id: int | None = None, commit: bool = True) -> int | None:
        if proposal.proposal_type not in PROPOSAL_TYPES:
            return None
        if proposal.proposal_type == "promote_insight":
            if not self._within_promotion_jurisdiction(agent_node_id, proposal.target_node_id):
                return None
        elif not self._within_jurisdiction(agent_node_id, proposal.target_node_id):
            return None
        target = self.conn.execute(
            "SELECT updated_at FROM goal_node WHERE id=?", (int(proposal.target_node_id),)).fetchone()
        if not target:
            return None
        canonical = _json({"type": proposal.proposal_type,
                           "target": int(proposal.target_node_id), "payload": proposal.payload})
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        if self.conn.execute(
            "SELECT 1 FROM goal_agent_proposal WHERE agent_node_id=? AND fingerprint=? "
            "AND status IN ('open','dismissed')",
            (int(agent_node_id), fingerprint)).fetchone():
            return None
        cur = self.conn.execute(
            "INSERT INTO goal_agent_proposal "
            "(agent_node_id,target_node_id,assessment_id,proposal_type,payload_json,rationale,"
            "fingerprint,target_version,status,created_at) VALUES (?,?,?,?,?,?,?,?, 'open',?)",
            (int(agent_node_id), int(proposal.target_node_id), assessment_id,
             proposal.proposal_type, crypto.enc(_json(proposal.payload)),
             crypto.enc(proposal.rationale), fingerprint, target["updated_at"], _now()))
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def _within_jurisdiction(self, agent_node_id: int, target_node_id: int) -> bool:
        current = int(target_node_id)
        while current:
            if current == int(agent_node_id):
                return True
            row = self.conn.execute(
                "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
            current = int(row["parent_id"]) if row and row["parent_id"] else 0
        return False

    def _within_promotion_jurisdiction(self, agent_node_id: int, target_node_id: int) -> bool:
        """Promotion may only move context to this node or one of its ancestors."""
        current = int(agent_node_id)
        while current:
            if current == int(target_node_id):
                return True
            row = self.conn.execute(
                "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
            current = int(row["parent_id"]) if row and row["parent_id"] else 0
        return False

    def questions(self, node_id: int, *, include_resolved: bool = False) -> list[dict]:
        sql = "SELECT * FROM goal_agent_question WHERE node_id=?"
        if not include_resolved:
            sql += " AND status='open'"
        sql += " ORDER BY id DESC"
        rows = self.conn.execute(sql, (int(node_id),)).fetchall()
        return [{"id": r["id"], "node_id": r["node_id"], "status": r["status"],
                 "text": crypto.dec(r["text"]), "answer": crypto.dec(r["answer"]),
                 "created_at": r["created_at"], "resolved_at": r["resolved_at"]}
                for r in rows]

    def answer_question(self, question_id: int, answer: str,
                        evidence_summary: str | None = None) -> int:
        answer = (answer or "").strip()
        if not answer:
            raise ValueError("answer is required")
        row = self.conn.execute(
            "SELECT * FROM goal_agent_question WHERE id=?", (int(question_id),)).fetchone()
        if not row or row["status"] != "open":
            raise ValueError("open GoalAI question not found")
        self.conn.execute(
            "UPDATE goal_agent_question SET status='answered',answer=?,resolved_at=? WHERE id=?",
            (crypto.enc(answer), _now(), int(question_id)))
        self.conn.execute(
            "INSERT OR IGNORE INTO goal_evidence_link "
            "(goal_id,source_kind,source_id,label,created_at) VALUES (?,?,?,?,?)",
            (row["node_id"], "goal_agent_answer", str(question_id),
             crypto.enc(evidence_summary or answer), _now()))
        self.conn.commit()
        self.mark_dirty(row["node_id"])
        return int(row["node_id"])

    def dismiss_question(self, question_id: int) -> int:
        row = self.conn.execute(
            "SELECT node_id,status FROM goal_agent_question WHERE id=?",
            (int(question_id),)).fetchone()
        if not row or row["status"] != "open":
            raise ValueError("open GoalAI question not found")
        self.conn.execute(
            "UPDATE goal_agent_question SET status='dismissed',resolved_at=? WHERE id=?",
            (_now(), int(question_id)))
        self.conn.commit()
        return int(row["node_id"])

    def reopen_question(self, question_id: int) -> int:
        row = self.conn.execute(
            "SELECT node_id,status FROM goal_agent_question WHERE id=?",
            (int(question_id),)).fetchone()
        if not row or row["status"] != "dismissed":
            raise ValueError("dismissed GoalAI question not found")
        self.conn.execute(
            "UPDATE goal_agent_question SET status='open',resolved_at=NULL WHERE id=?",
            (int(question_id),))
        self.conn.commit()
        return int(row["node_id"])

    def proposals(self, node_id: int | None = None, *, status: str | None = "open") -> list[dict]:
        where, args = [], []
        if node_id is not None:
            where.append("agent_node_id=?"); args.append(int(node_id))
        if status is not None:
            where.append("status=?"); args.append(status)
        sql = "SELECT * FROM goal_agent_proposal" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY id DESC"
        return [self._proposal(r) for r in self.conn.execute(sql, args).fetchall()]

    def _proposal(self, row) -> dict:
        return {"id": row["id"], "agent_node_id": row["agent_node_id"],
                "target_node_id": row["target_node_id"], "type": row["proposal_type"],
                "payload": self._dec_json(row["payload_json"], {}),
                "rationale": crypto.dec(row["rationale"]) or "", "status": row["status"],
                "target_version": row["target_version"], "created_at": row["created_at"]}

    def get_proposal(self, proposal_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM goal_agent_proposal WHERE id=?", (int(proposal_id),)).fetchone()
        if not row:
            raise ValueError("GoalAI proposal not found")
        return self._proposal(row)

    def resolve_proposal(self, proposal_id: int, status: str) -> None:
        if status not in {"approved", "dismissed", "stale"}:
            raise ValueError("invalid proposal resolution")
        self.conn.execute(
            "UPDATE goal_agent_proposal SET status=?,resolved_at=? WHERE id=? AND status='open'",
            (status, _now(), int(proposal_id)))
        self.conn.commit()

    def reopen_proposal(self, proposal_id: int) -> int:
        row = self.conn.execute(
            "SELECT agent_node_id,status FROM goal_agent_proposal WHERE id=?",
            (int(proposal_id),)).fetchone()
        if not row or row["status"] != "dismissed":
            raise ValueError("dismissed GoalAI proposal not found")
        self.conn.execute(
            "UPDATE goal_agent_proposal SET status='open',resolved_at=NULL WHERE id=?",
            (int(proposal_id),))
        self.conn.commit()
        return int(row["agent_node_id"])

    def refine_proposal(self, proposal_id: int, payload: dict, rationale: str = "") -> dict:
        proposal = self.get_proposal(proposal_id)
        if proposal["status"] != "open":
            raise ValueError("only open proposals can be refined")
        target = self.conn.execute(
            "SELECT updated_at FROM goal_node WHERE id=?", (proposal["target_node_id"],)).fetchone()
        canonical = _json({"type": proposal["type"], "target": proposal["target_node_id"],
                           "payload": payload})
        self.conn.execute(
            "UPDATE goal_agent_proposal SET payload_json=?,rationale=?,fingerprint=?,"
            "target_version=? WHERE id=?",
            (crypto.enc(_json(payload)), crypto.enc(rationale or proposal["rationale"]),
             hashlib.sha256(canonical.encode()).hexdigest(), target["updated_at"], int(proposal_id)))
        self.conn.commit()
        return self.get_proposal(proposal_id)

    def assessments(self, node_id: int, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM goal_agent_assessment WHERE node_id=? ORDER BY id DESC LIMIT ?",
            (int(node_id), int(limit))).fetchall()
        return [{"id": r["id"], "health": r["health"], "confidence": r["confidence"],
                 "report": self._dec_json(r["report_json"], {}), "model": r["model"],
                 "created_at": r["created_at"]} for r in rows]

    def messages(self, node_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM goal_agent_message WHERE node_id=? ORDER BY id DESC LIMIT ?",
            (int(node_id), int(limit))).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": crypto.dec(r["content"]),
                 "created_at": r["created_at"]} for r in reversed(rows)]

    def add_message(self, node_id: int, role: str, content: str) -> int:
        if role not in {"user", "assistant"} or not str(content).strip():
            raise ValueError("valid role and content required")
        cur = self.conn.execute(
            "INSERT INTO goal_agent_message (node_id,role,content,created_at) VALUES (?,?,?,?)",
            (int(node_id), role, crypto.enc(str(content).strip()), _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def add_memory_candidate(self, node_id: int, candidate: dict,
                             message_id: int | None = None) -> int:
        category = str(candidate.get("category") or "goals").strip()
        attribute = str(candidate.get("attribute") or "accomplishment").strip()
        value = str(candidate.get("value") or "").strip()
        if not value:
            raise ValueError("memory candidate value is required")
        cur = self.conn.execute(
            "INSERT INTO goal_agent_memory_candidate "
            "(node_id,message_id,category,attribute,value,source_text,status,created_at) "
            "VALUES (?,?,?,?,?,?,'open',?)",
            (int(node_id), message_id, crypto.enc(category), crypto.enc(attribute),
             crypto.enc(value), crypto.enc(str(candidate.get("source_text") or value)), _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def memory_candidates(self, node_id: int, status: str = "open") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM goal_agent_memory_candidate WHERE node_id=? AND status=? ORDER BY id DESC",
            (int(node_id), status)).fetchall()
        return [{"id": r["id"], "node_id": r["node_id"],
                 "category": crypto.dec(r["category"]), "attribute": crypto.dec(r["attribute"]),
                 "value": crypto.dec(r["value"]), "source_text": crypto.dec(r["source_text"]),
                 "status": r["status"], "memory_id": r["memory_id"]}
                for r in rows]

    def resolve_memory_candidate(self, candidate_id: int, status: str,
                                 memory_id: int | None = None) -> None:
        if status not in {"saved", "dismissed"}:
            raise ValueError("invalid memory candidate resolution")
        self.conn.execute(
            "UPDATE goal_agent_memory_candidate SET status=?,memory_id=?,resolved_at=? "
            "WHERE id=? AND status='open'", (status, memory_id, _now(), int(candidate_id)))
        self.conn.commit()

    def reopen_memory_candidate(self, candidate_id: int) -> int:
        row = self.conn.execute(
            "SELECT node_id,status FROM goal_agent_memory_candidate WHERE id=?",
            (int(candidate_id),)).fetchone()
        if not row or row["status"] != "dismissed":
            raise ValueError("dismissed memory candidate not found")
        self.conn.execute(
            "UPDATE goal_agent_memory_candidate "
            "SET status='open',memory_id=NULL,resolved_at=NULL WHERE id=?",
            (int(candidate_id),))
        self.conn.commit()
        return int(row["node_id"])

    def create_harvest(self, source_node_id: int, draft: dict) -> dict:
        if not self.conn.execute("SELECT 1 FROM goal_node WHERE id=?",
                                 (int(source_node_id),)).fetchone():
            raise ValueError("harvest source not found")
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO goal_harvest (source_node_id,status,draft_json,created_at,updated_at) "
            "VALUES (?,'draft',?,?,?)",
            (int(source_node_id), crypto.enc(_json(draft)), now, now))
        self.conn.commit()
        return self.harvest(int(cur.lastrowid))

    def harvest(self, harvest_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM goal_harvest WHERE id=?",
                                (int(harvest_id),)).fetchone()
        if not row:
            raise ValueError("harvest not found")
        return {"id": row["id"], "source_node_id": row["source_node_id"],
                "status": row["status"], "draft": self._dec_json(row["draft_json"], {}),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "committed_at": row["committed_at"]}

    def update_harvest(self, harvest_id: int, draft: dict) -> dict:
        current = self.harvest(harvest_id)
        if current["status"] != "draft":
            raise ValueError("only a draft harvest can be revised")
        self.conn.execute("UPDATE goal_harvest SET draft_json=?,updated_at=? WHERE id=?",
                          (crypto.enc(_json(draft)), _now(), int(harvest_id)))
        self.conn.commit()
        return self.harvest(harvest_id)

    def commit_harvest(self, harvest_id: int, draft: dict | None = None) -> dict:
        if draft is not None:
            self.update_harvest(harvest_id, draft)
        harvest = self.harvest(harvest_id)
        if harvest["status"] == "committed":
            return harvest
        source = self.conn.execute("SELECT node_type FROM goal_node WHERE id=?",
                                   (harvest["source_node_id"],)).fetchone()
        routes = harvest["draft"].get("routes") or []
        # Cross-branch routing is Soul authority. Lower agents publish upward;
        # the Soul may later harvest and route the reusable result downward.
        if source and source["node_type"] == "umbrella":
            for route in routes:
                try:
                    target = int(route.get("target_node_id"))
                except (TypeError, ValueError):
                    continue
                if self.conn.execute("SELECT 1 FROM goal_node WHERE id=?", (target,)).fetchone():
                    self.conn.execute(
                        "INSERT OR REPLACE INTO goal_harvest_route "
                        "(harvest_id,target_node_id,insight_indexes,reason,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (int(harvest_id), target,
                         json.dumps([int(i) for i in (route.get("insight_indexes") or [])
                                     if str(i).lstrip("-").isdigit()]),
                         crypto.enc(str(route.get("reason") or "")), _now()))
                    self.mark_dirty(target)
        self.conn.execute(
            "UPDATE goal_harvest SET status='committed',committed_at=?,updated_at=? WHERE id=?",
            (_now(), _now(), int(harvest_id)))
        self.conn.commit()
        self.mark_dirty(harvest["source_node_id"])
        return self.harvest(harvest_id)

    def harvest_context(self, node_id: int, limit: int = 20) -> list[dict]:
        """Upward descendant harvests plus Soul-approved routes inherited downward."""
        upward = self.conn.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT ? UNION ALL "
            "SELECT g.id FROM goal_node g JOIN descendants d ON g.parent_id=d.id) "
            "SELECT h.* FROM goal_harvest h WHERE h.status='committed' "
            "AND h.source_node_id IN (SELECT id FROM descendants) "
            "ORDER BY h.id DESC LIMIT ?", (int(node_id), int(limit))).fetchall()
        routed = self.conn.execute(
            "WITH RECURSIVE ancestors(id) AS (SELECT ? UNION ALL "
            "SELECT g.parent_id FROM goal_node g JOIN ancestors a ON g.id=a.id "
            "WHERE g.parent_id IS NOT NULL) "
            "SELECT h.*,r.insight_indexes,r.reason route_reason FROM goal_harvest h "
            "JOIN goal_harvest_route r ON r.harvest_id=h.id "
            "WHERE h.status='committed' AND r.target_node_id IN (SELECT id FROM ancestors) "
            "ORDER BY h.id DESC LIMIT ?", (int(node_id), int(limit))).fetchall()
        out, seen = [], set()
        for row in upward:
            draft = self._dec_json(row["draft_json"], {})
            out.append({"id": row["id"], "source_node_id": row["source_node_id"],
                        "flow": "upward", **draft})
            seen.add(int(row["id"]))
        for row in routed:
            if int(row["id"]) in seen:
                continue
            draft = self._dec_json(row["draft_json"], {})
            try:
                indexes = [int(i) for i in json.loads(row["insight_indexes"] or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                indexes = []
            insights = draft.get("insights") or []
            selected = [insights[i] for i in indexes if 0 <= i < len(insights)]
            out.append({"id": row["id"], "source_node_id": row["source_node_id"],
                        "flow": "routed", "summary": draft.get("summary", ""),
                        "insights": selected,
                        "route_reason": crypto.dec(row["route_reason"]) or ""})
            seen.add(int(row["id"]))
        return out[:limit]

    def node_view(self, node_id: int) -> dict:
        return {"state": self.state(node_id), "questions": self.questions(node_id),
                "proposals": self.proposals(node_id), "assessments": self.assessments(node_id, 6),
                "messages": self.messages(node_id),
                "memory_candidates": self.memory_candidates(node_id),
                "harvests": self.harvest_context(node_id)}

    def overview(self, stale_minutes: float = 240.0) -> dict:
        states = self.all_states()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=float(stale_minutes))
        active = {int(r["id"]) for r in self.conn.execute(
            "SELECT id FROM goal_node WHERE status='active'").fetchall()}
        blocked = [{"node_id": s["node_id"]} for s in states
                   if s["node_id"] in active and s["health"] == "blocked"]
        attention = [{"node_id": s["node_id"]} for s in states
                     if s["node_id"] in active and s["health"] == "needs-attention"]
        stale = [{"node_id": s["node_id"]} for s in states
                 if s["node_id"] in active and (
                     not s["last_run_at"] or datetime.fromisoformat(s["last_run_at"]) <= cutoff)]
        questions = [{"id": int(r["id"]), "node_id": int(r["node_id"])}
                     for r in self.conn.execute(
                         "SELECT id,node_id FROM goal_agent_question WHERE status='open' "
                         "ORDER BY id").fetchall()]
        proposals = [{"id": int(r["id"]), "node_id": int(r["agent_node_id"])}
                     for r in self.conn.execute(
                         "SELECT id,agent_node_id FROM goal_agent_proposal WHERE status='open' "
                         "ORDER BY id").fetchall()]
        deferred = [{"node_id": s["node_id"]} for s in states
                    if s["node_id"] in active and s.get("deferred")]
        dirty = [{"node_id": s["node_id"], "reason": s.get("dirty_reason", "")}
                 for s in states if s["node_id"] in active and s["dirty"]]
        return {"blocked": len(blocked),
                "needs_attention": len(attention),
                "dirty": len(dirty), "deferred": len(deferred),
                "stale": len(stale), "open_questions": len(questions),
                "open_proposals": len(proposals),
                "queues": {"blocked": blocked, "needs_attention": attention,
                           "stale": stale, "questions": questions,
                           "proposals": proposals, "dirty": dirty,
                           "deferred": deferred}}


def _find(node: dict, node_id: int) -> dict | None:
    if not node:
        return None
    if int(node["id"]) == int(node_id):
        return node
    for child in node.get("children", []):
        found = _find(child, node_id)
        if found:
            return found
    return None


def _ancestors(goals: GoalStore, node: dict) -> list[dict]:
    chain = []
    current = node
    while current and current.get("parent_id"):
        current = goals.get(current["parent_id"])
        if current:
            chain.append(current)
    return list(reversed(chain))


def _agent_summary(store: GoalAgentStore, node_id: int) -> dict:
    try:
        state = store.state(node_id)
        return {k: state[k] for k in ("health", "confidence", "brief", "blockers", "next_focus")}
    except ValueError:
        return {"health": "unknown", "confidence": 0, "brief": "", "blockers": [],
                "next_focus": ""}


def build_agent_context(goals: GoalStore, agents: GoalAgentStore, node_id: int,
                        *, max_chars: int = 14000) -> dict:
    """Build a bounded hierarchy context plus always-on Core Profile facts.

    General global memory and passive capture remain excluded; Core Profile is
    explicitly user-curated hard context.
    """
    tree = goals.tree()
    node = _find(tree, node_id)
    if not node:
        raise ValueError("goal not found")

    def clipped(value, limit=800):
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"
        if isinstance(value, list):
            return [clipped(item, limit) for item in value]
        if isinstance(value, dict):
            return {key: clipped(item, limit) for key, item in value.items()}
        return value

    def intent(item):
        return {"id": item["id"], "type": item["type"], "title": item["title"],
                "description": item.get("description", "")}

    parent_state = agents.state(node_id)
    parent_last = parent_state.get("last_run_at")

    def descendant_is_fresh(item):
        state = agents.state(item["id"])
        if state["dirty"] or not parent_last:
            return True
        return bool(state.get("last_run_at") and state["last_run_at"] > parent_last)

    def subtree(item, depth=0):
        if depth and not descendant_is_fresh(item):
            # Cached reports keep the complete strategic view without repeatedly
            # shipping unchanged descendant descriptions, notes, and evidence.
            return {"id": item["id"], "type": item["type"], "title": item["title"],
                    "status": item["status"], "completion": item.get("completion"),
                    "mastery": item.get("mastery"),
                    "agent_report": _agent_summary(agents, item["id"]),
                    "cached_unchanged": True, "children": []}
        compact = {"id": item["id"], "type": item["type"], "title": item["title"],
                   "description": item.get("description", ""), "status": item["status"],
                   "priority": item["priority"], "due_date": item.get("due_date"),
                   "completion": item.get("completion"), "mastery": item.get("mastery"),
                   "origin": item.get("origin"),
                   "agent_report": _agent_summary(agents, item["id"]),
                   "children": []}
        for child in item.get("children", []):
            compact["children"].append(subtree(child, depth + 1))
        return compact

    curiosity_details = []
    if node.get("curiosities"):
        from .curiosity import CuriosityStore
        curiosities = CuriosityStore(agents.db_path)
        try:
            for linked in node["curiosities"]:
                cur = curiosities.get_curiosity(linked["id"])
                if cur:
                    items = curiosities.items_for_curiosity(linked["id"])[-12:]
                    curiosity_details.append({
                        "id": cur["id"], "label": cur["label"], "directive": cur["directive"],
                        "status": cur["status"],
                        "items": [{"kind": i["kind"], "text": i["text"],
                                   "status": i["status"], "answer": i.get("answer")}
                                  for i in items],
                    })
        finally:
            curiosities.close()
    try:
        from .memory import MemoryStore
        mem = MemoryStore(agents.db_path)
        try:
            core_profile = mem.core_profile_facts(limit=50)
        finally:
            mem.close()
    except Exception:
        core_profile = []
    context = {
        "jurisdiction": {"node_id": node_id, "node_type": node["type"]},
        "core_profile": core_profile,
        "ancestor_intent": [intent(a) for a in _ancestors(goals, node)],
        "node": {k: node.get(k) for k in (
            "id", "parent_id", "type", "title", "description", "notes", "status",
            "priority", "due_date", "completion", "mastery", "evidence", "origin")},
        "subtree": subtree(node),
        "attached_curiosities": curiosity_details,
        "agent_state": agents.state(node_id),
        "prior_assessments": agents.assessments(node_id, 5),
        "open_proposals": agents.proposals(node_id),
        "resolved_proposals": agents.proposals(node_id, status="dismissed")[:8],
        "answered_questions": [q for q in agents.questions(node_id, include_resolved=True)
                               if q["status"] == "answered"][-8:],
        "recent_chat": agents.messages(node_id, 12),
        "committed_harvests": agents.harvest_context(node_id),
    }
    encoded = _json(context)
    if len(encoded) > max_chars:
        context["prior_assessments"] = context["prior_assessments"][:2]
        context["resolved_proposals"] = context["resolved_proposals"][:3]
        context["recent_chat"] = context["recent_chat"][-6:]
        context["attached_curiosities"] = [
            {**c, "items": c["items"][-4:]} for c in context["attached_curiosities"]]
        encoded = _json(context)
    if len(encoded) > max_chars:
        context["subtree"] = {
            **context["subtree"],
            "children": [{k: child.get(k) for k in
                          ("id", "type", "title", "status", "completion", "agent_report")}
                         for child in context["subtree"].get("children", [])],
        }
        context["answered_questions"] = clipped(context["answered_questions"][-4:], 700)
        context["recent_chat"] = clipped(context["recent_chat"][-4:], 700)
        context["attached_curiosities"] = clipped(context["attached_curiosities"][:4], 700)
        context["committed_harvests"] = clipped(context["committed_harvests"][:4], 700)
        context["core_profile"] = clipped(context["core_profile"][:30], 1200)
        context["node"] = clipped(context["node"], 1200)
        context["ancestor_intent"] = clipped(context["ancestor_intent"], 700)
        encoded = _json(context)
    if len(encoded) > max_chars:
        # Final bounded form. It preserves jurisdiction and actionable state,
        # but omits verbose history rather than silently exceeding the budget.
        context = {
            "jurisdiction": context["jurisdiction"],
            "core_profile": clipped(context.get("core_profile", [])[:20], 700),
            "ancestor_intent": clipped(context["ancestor_intent"], 350),
            "node": clipped(context["node"], 650),
            "subtree": clipped(context["subtree"], 350),
            "attached_curiosities": clipped(context["attached_curiosities"][:2], 350),
            "agent_state": clipped(context["agent_state"], 500),
            "open_proposals": clipped(context["open_proposals"][:3], 350),
            "answered_questions": clipped(context["answered_questions"][-2:], 350),
            "committed_harvests": clipped(context["committed_harvests"][:2], 350),
            "prompt_budget_truncated": True,
        }
        encoded = _json(context)
    if len(encoded) > max_chars:
        # Extremely small custom budgets still fail closed to a minimal valid
        # context instead of sending an oversized prompt.
        context = {
            "jurisdiction": context["jurisdiction"],
            "core_profile": clipped(context.get("core_profile", [])[:10], 300),
            "node": clipped({k: context["node"].get(k) for k in
                             ("id", "parent_id", "type", "title", "status",
                              "priority", "completion")}, 200),
            "prompt_budget_truncated": True,
        }
    return context


def context_hash(context: dict) -> str:
    return hashlib.sha256(_json(context).encode()).hexdigest()


ROLE_GUIDANCE = {
    "task": "You are a Leaf agent. Assess execution, blockers, evidence needs, and the immediate next action.",
    "subgoal": "You are a Branch agent. Coordinate Leaves and nested Branches without leaving this branch.",
    "overgoal": "You are a Root agent. Assess strategy, sequencing, tradeoffs, and domain progress.",
    "umbrella": "You are the Soul agent. Integrate the full tree against the user's Actualized Self intent.",
}

REPORT_SYSTEM = """You are one bounded agent in a personal goal hierarchy.
You may update only your own analytical report. You must never claim to have
changed, completed, paused, archived, or mastered a goal. Structural ideas are
proposals for user approval. Use only the supplied hierarchy context: no assumed
memory, screen activity, or facts. Be concise, specific, and non-clinical.

Return strict JSON:
{"brief":str,"health":"unknown"|"on-track"|"needs-attention"|"blocked",
"confidence":0-1,"evidence":[str],"blockers":[str],"next_focus":str,
"questions":[str],"proposals":[{"type":str,"target_node_id":int,
"payload":object,"rationale":str}]}
Allowed proposal types: create_child, update_fields, pause, archive,
request_evidence, start_curiosity, promote_insight. Never propose automatic completion or mastery.
For create_child, type must be overgoal/subgoal/task (Root/Branch/Leaf) and
priority must be low/normal/high. Use "normal", never "medium". update_fields
may contain description, notes, priority, or due_date.
Use promote_insight only when confidence is at least 0.8 that a lesson,
preference, constraint, blocker, method, or decision matters beyond the current
node. Its target_node_id must be the current node or an ancestor where the
insight should become visible. Payload must be {"summary":str,"title":str,
"detail":str,"kind":"preference"|"constraint"|"method"|"lesson"|"decision",
"confidence":0.8-1}. The user must approve before it flows upward.
"""

CHAT_SYSTEM = """You are the persistent bounded agent for one goal-tree node.
Answer using only its supplied hierarchy context and conversation. You may offer
structured proposals, but you cannot mutate goals. When the user explicitly asks
to save an accomplishment to memory, return an exact memory_candidate for review;
never save it yourself.
For proposal payloads, priority must be low/normal/high (use normal rather than
medium), and child type must be overgoal/subgoal/task. You may use
promote_insight when confidence is at least 0.8 that something discussed should
move upward to this node or an ancestor; include summary, title, detail, kind,
and confidence in the payload.
Return strict JSON: {"reply":str,"proposals":[{"type":str,
"target_node_id":int,"payload":object,"rationale":str}],
"memory_candidate":null|{"category":str,"attribute":str,"value":str,
"source_text":str}}.
"""

ANSWER_SUMMARY_SYSTEM = """Summarize the user's exact answer into 3-7 concise,
faithful bullet points for a compact evidence display. Preserve decisions,
constraints, preferences, dates, and important examples. Do not infer beyond
what they wrote. Return strict JSON only: {"bullets":[str]}.
"""

DESCRIPTION_SYSTEM = """Draft a concise description for this goal-tree node.
Explain what success means and why the node exists in 1-3 plain sentences.
Use only the supplied bounded hierarchy context. Do not invent facts, dates, or
commitments. Return strict JSON only: {"description":str}.
"""

HARVEST_SYSTEM = """You distill reusable learning from one bounded goal-tree
scope. Produce compact insights that would prevent the user from having to
explain the same constraint, preference, method, blocker, or lesson again.
Do not copy the full branch and do not invent facts. Insights from a Root,
Branch, or Leaf flow upward to the Soul; only a Soul harvest may suggest
cross-branch routes. Route only when another node would materially benefit.
Return strict JSON: {"summary":str,"insights":[{"title":str,"detail":str,
"kind":"preference|constraint|method|lesson|decision"}],"routes":[{
"target_node_id":int,"insight_indexes":[int],"reason":str}]}.
"""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", (text or "").strip(), re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _parse_proposals(raw, default_target: int) -> list[AgentProposal]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()
        if kind not in PROPOSAL_TYPES:
            continue
        try:
            target = int(item.get("target_node_id") or default_target)
        except (TypeError, ValueError):
            target = default_target
        payload = dict(item.get("payload") or {})
        if "priority" in payload:
            payload["priority"] = _normalize_priority(payload["priority"])
        if kind == "create_child" and "type" in payload:
            payload["type"] = _normalize_node_type(payload["type"])
        if kind == "promote_insight":
            try:
                confidence = max(0.0, min(1.0, float(payload.get("confidence", 0))))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < PROMOTION_CONFIDENCE_GATE:
                continue
            payload["confidence"] = confidence
            if not str(payload.get("detail") or payload.get("summary") or "").strip():
                continue
        out.append(AgentProposal(kind, target, payload,
                                 str(item.get("rationale") or "").strip()))
    return out


def parse_report(text: str, node_id: int) -> AgentReport | None:
    data = _extract_json(text)
    brief = str(data.get("brief") or "").strip()
    health = str(data.get("health") or "unknown").strip().lower()
    if not brief or health not in HEALTH_STATES:
        return None
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    strings = lambda key: [str(x).strip() for x in data.get(key, [])
                           if str(x).strip()][:8]
    return AgentReport(brief, health, confidence, strings("evidence"),
                       strings("blockers"), str(data.get("next_focus") or "").strip(),
                       strings("questions")[:3], _parse_proposals(data.get("proposals"), node_id))


def parse_chat(text: str, node_id: int) -> ChatResult | None:
    data = _extract_json(text)
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    candidate = data.get("memory_candidate")
    if not isinstance(candidate, dict) or not str(candidate.get("value") or "").strip():
        candidate = None
    return ChatResult(reply, _parse_proposals(data.get("proposals"), node_id), candidate)


def parse_harvest(text: str, *, allow_routes: bool) -> HarvestDraft | None:
    data = _extract_json(text)
    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    insights = []
    for item in data.get("insights") or []:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "").strip()
        if detail:
            insights.append({"title": str(item.get("title") or "Insight").strip(),
                             "detail": detail,
                             "kind": str(item.get("kind") or "lesson").strip()})
    routes = [dict(r) for r in (data.get("routes") or []) if isinstance(r, dict)] if allow_routes else []
    return HarvestDraft(summary, insights[:12], routes[:12])


class StubGoalAgentModel:
    model_name = "stub-goal-agent"

    def assess(self, context: dict, role: str) -> AgentReport:
        node = context["node"]
        completion = node.get("completion") or {}
        if node["type"] == "task":
            health = "unknown"
            brief = f"There is not enough explicit evidence yet to assess {node['title']}."
        elif completion.get("percent") == 100:
            health = "on-track"; brief = f"All active tasks under {node['title']} are complete."
        elif context["subtree"].get("children"):
            health = "needs-attention"; brief = f"{node['title']} has active work to coordinate."
        else:
            health = "unknown"; brief = f"{node['title']} needs a concrete next step."
        return AgentReport(brief, health, .65, [], [],
                           "Review the next concrete action.",
                           ["What would meaningful progress look like next?"] if health == "unknown" else [])

    def chat(self, context: dict, messages: list[dict]) -> ChatResult:
        last = messages[-1]["content"] if messages else ""
        candidate = None
        if "memory" in last.lower() or "accomplish" in last.lower():
            candidate = {"category": context["node"]["title"],
                         "attribute": "accomplishment", "value": last,
                         "source_text": last}
        return ChatResult("I’m keeping this scoped to the selected goal. "
                          "I can assess its evidence or help shape a proposal.",
                          memory_candidate=candidate)

    def harvest(self, context: dict, instruction: str = "") -> HarvestDraft:
        node = context["node"]
        brief = context.get("agent_state", {}).get("brief") or node.get("description") or node["title"]
        return HarvestDraft(
            f"Reusable learning harvested from {node['title']}.",
            [{"title": "Current lesson", "detail": brief, "kind": "lesson"}], [])

    def summarize_answer(self, text: str) -> list[str]:
        return _fallback_bullets(text)

    def describe(self, context: dict) -> str:
        node = context["node"]
        label = {"umbrella": "Soul", "overgoal": "Root",
                 "subgoal": "Branch", "task": "Leaf"}.get(node["type"], "node")
        return f"This {label} defines what meaningful progress toward {node['title']} looks like."


class ClaudeGoalAgentModel:
    def __init__(self, model: str, config, *, usage_category: str = "goal_ai"):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from anthropic import Anthropic
        self.model_name = model
        self.usage_category = usage_category
        self.client = Anthropic(api_key=key,
                                timeout=getattr(config, "llm_timeout_seconds", 60.0),
                                max_retries=getattr(config, "llm_max_retries", 0))

    def _call(self, system: str, prompt: str) -> str:
        log_diag("prompt", f"surface=goal-ai model={self.model_name} input_chars={len(prompt)}")
        started = time.monotonic()
        msg = self.client.messages.create(
            model=self.model_name, max_tokens=1300, system=system,
            messages=[{"role": "user", "content": prompt}])
        from .llm_usage import record_response
        record_response(self.usage_category, self.model_name, msg, time.monotonic() - started)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def assess(self, context: dict, role: str) -> AgentReport:
        result = parse_report(self._call(
            REPORT_SYSTEM, f"ROLE: {ROLE_GUIDANCE[role]}\nCONTEXT:\n{_json(context)}"),
            context["node"]["id"])
        if not result:
            raise ValueError("GoalAI returned an invalid report")
        return result

    def chat(self, context: dict, messages: list[dict]) -> ChatResult:
        result = parse_chat(self._call(
            CHAT_SYSTEM, f"ROLE: {ROLE_GUIDANCE[context['node']['type']]}\n"
            f"CONTEXT:\n{_json(context)}\nCONVERSATION:\n{_json(messages[-12:])}"),
            context["node"]["id"])
        if not result:
            raise ValueError("GoalAI returned an invalid chat response")
        return result

    def harvest(self, context: dict, instruction: str = "") -> HarvestDraft:
        allow_routes = context["node"]["type"] == "umbrella"
        result = parse_harvest(self._call(
            HARVEST_SYSTEM,
            f"SCOPE:\n{_json(context)}\nUSER REVISION REQUEST:\n{instruction or '(initial harvest)'}\n"
            f"CROSS-BRANCH ROUTES ALLOWED: {str(allow_routes).lower()}"),
            allow_routes=allow_routes)
        if not result:
            raise ValueError("GoalAI returned an invalid harvest")
        return result

    def summarize_answer(self, text: str) -> list[str]:
        data = _extract_json(self._call(ANSWER_SUMMARY_SYSTEM, str(text or "")))
        bullets = [str(x).strip() for x in (data.get("bullets") or []) if str(x).strip()]
        return bullets[:7] or _fallback_bullets(text)

    def describe(self, context: dict) -> str:
        data = _extract_json(self._call(DESCRIPTION_SYSTEM, _json(context)))
        description = str(data.get("description") or "").strip()
        if not description:
            raise ValueError("GoalAI returned no description")
        return description[:1200]


def get_goal_agent_model(config, node_type: str, *, manual: bool = False):
    backend = (getattr(config, "goal_ai_backend", "") or
               getattr(config, "inference_backend", "claude")).lower()
    if backend == "stub":
        return StubGoalAgentModel()
    parent = node_type in {"overgoal", "umbrella"} or manual
    model = (getattr(config, "goal_ai_parent_model", "claude-sonnet-4-6") if parent else
             getattr(config, "goal_ai_leaf_model", "claude-haiku-4-5"))
    return ClaudeGoalAgentModel(
        model, config, usage_category="manual" if manual else "goal_ai")


def summarize_goal_answer(config, node_id: int, text: str, *, model=None) -> str:
    """Compact long UI evidence while the exact encrypted answer remains stored."""
    text = str(text or "").strip()
    if len(text) <= 500:
        return text
    goals = GoalStore(config.memory_db_path)
    try:
        node = goals.get(node_id)
        if not node:
            raise ValueError("goal not found")
        # Summarization is a narrow compression task; use the configured leaf
        # model even when the answer belongs to a Root or Soul.
        active = model or get_goal_agent_model(config, "task", manual=False)
        goals.conn.commit()
        bullets = active.summarize_answer(text)
        return "\n".join(f"• {bullet}" for bullet in bullets)
    finally:
        goals.close()


def generate_goal_description(config, node_id: int, *, model=None) -> str:
    """Return an unsaved GoalAI description draft for explicit user review."""
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        node = goals.get(node_id)
        if not node:
            raise ValueError("goal not found")
        context = build_agent_context(
            goals, agents, node_id,
            max_chars=int(getattr(config, "goal_ai_context_max_chars", 14000)))
        active = model or get_goal_agent_model(config, node["type"], manual=True)
        goals.conn.commit()
        agents.conn.commit()
        return active.describe(context)
    finally:
        agents.close(); goals.close()


def run_goal_agent(config, node_id: int, *, model=None, manual: bool = False) -> dict:
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        node = goals.get(node_id)
        if not node:
            raise ValueError("goal not found")
        context = build_agent_context(
            goals, agents, node_id,
            max_chars=int(getattr(config, "goal_ai_context_max_chars", 14000)))
        digest = context_hash(context)
        active_model = model or get_goal_agent_model(config, node["type"], manual=manual)
        # Never hold a write transaction across a model call: commit anything
        # context-building may have started before the network round-trip.
        goals.conn.commit()
        agents.conn.commit()
        try:
            report = active_model.assess(context, node["type"])
            saved = agents.save_report(
                node_id, report, digest, active_model.model_name,
                proposal_cap=int(getattr(config, "goal_ai_max_open_proposals", 3)))
            parent_id = node.get("parent_id")
            if parent_id:
                agents.mark_dirty(int(parent_id))
            return {"ok": True, "node_id": node_id, "health": report.health, **saved}
        except Exception:
            agents.record_error(node_id)
            raise
    finally:
        agents.close(); goals.close()


def _depths(tree: dict) -> dict[int, int]:
    out = {}
    def walk(node, depth):
        out[int(node["id"])] = depth
        for child in node.get("children", []):
            walk(child, depth + 1)
    if tree:
        walk(tree, 0)
    return out


def run_goal_subtree(config, node_id: int, *, models: dict | None = None) -> dict:
    goals = GoalStore(config.memory_db_path)
    try:
        root = _find(goals.tree(), node_id)
        if not root:
            raise ValueError("goal not found")
        nodes = []
        def collect(node):
            for child in node.get("children", []):
                if child["status"] == "active":
                    collect(child)
            if node["id"] == node_id or node["status"] == "active":
                nodes.append(node)
        collect(root)
    finally:
        goals.close()
    results = []
    for node in nodes:
        chosen = (models or {}).get(node["type"])
        results.append(run_goal_agent(config, node["id"], model=chosen, manual=True))
    return {"ok": True, "reviewed": len(results), "results": results}


def due_goal_nodes(config, now: datetime | None = None) -> list[int]:
    now = now or datetime.now(timezone.utc)
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        tree = goals.tree()
        depths = _depths(tree)
        agents.mark_due_date_boundaries(now.astimezone())
        rows = goals.conn.execute(
            "SELECT g.id,g.status,s.dirty,s.health,s.last_run_at,s.updated_at "
            "FROM goal_node g "
            "JOIN goal_agent_state s ON s.node_id=g.id WHERE g.status='active'"
        ).fetchall()
        # Time passing alone is deliberately not eligibility. New records begin
        # dirty, and all meaningful mutations persistently dirty their path.
        due = [row for row in rows if row["dirty"] or row["last_run_at"] is None]
        due.sort(key=lambda r: (
            -depths.get(int(r["id"]), 0),
            0 if r["health"] == "blocked" else 1,
            r["updated_at"] or "",
            int(r["id"]),
        ))
        limit = max(1, int(getattr(config, "goal_ai_batch_size", 12)))
        chosen = [int(row["id"]) for row in due[:limit]]
        deferred = [int(row["id"]) for row in due[limit:]]
        agents.conn.execute("UPDATE goal_agent_state SET deferred=0")
        if deferred:
            marks = ",".join("?" for _ in deferred)
            agents.conn.execute(
                f"UPDATE goal_agent_state SET deferred=1 WHERE node_id IN ({marks})",
                deferred)
        agents.conn.commit()
        return chosen
    finally:
        agents.close(); goals.close()


def run_goal_sweep(config, *, now: datetime | None = None,
                   model_factory=None) -> dict:
    node_ids = due_goal_nodes(config, now=now)
    results, failures = [], 0
    for node_id in node_ids:
        try:
            model = model_factory(node_id) if model_factory else None
            results.append(run_goal_agent(config, node_id, model=model))
        except Exception as error:
            failures += 1
            log_diag("goal-ai", f"scheduled node failed node_id={node_id} error={type(error).__name__}")
    return {"reviewed": len(results), "failures": failures,
            "proposals_created": sum(r.get("proposals_created", 0) for r in results),
            "became_blocked": sum(bool(r.get("became_blocked")) for r in results),
            "results": results}


def chat_with_goal_agent(config, node_id: int, text: str, *, model=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("message is required")
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        node = goals.get(node_id)
        if not node:
            raise ValueError("goal not found")
        agents.add_message(node_id, "user", text)
        context = build_agent_context(goals, agents, node_id,
                                      max_chars=int(getattr(config, "goal_ai_context_max_chars", 14000)))
        messages = agents.messages(node_id, 12)
        active_model = model or get_goal_agent_model(config, node["type"], manual=True)
        # Never hold a write transaction across a model call (see run_goal_agent).
        goals.conn.commit()
        agents.conn.commit()
        result = active_model.chat(context, messages)
        message_id = agents.add_message(node_id, "assistant", result.reply)
        created = 0
        open_count = len(agents.proposals(node_id))
        cap = int(getattr(config, "goal_ai_max_open_proposals", 3))
        for proposal in result.proposals:
            if open_count >= cap:
                break
            if agents.add_proposal(node_id, proposal):
                created += 1; open_count += 1
        candidate_id = (agents.add_memory_candidate(node_id, result.memory_candidate, message_id)
                        if result.memory_candidate else None)
        agents.mark_dirty(node_id)
        return {"reply": result.reply, "proposals_created": created,
                "memory_candidate_id": candidate_id, "view": agents.node_view(node_id)}
    finally:
        agents.close(); goals.close()


def start_goal_harvest(config, node_id: int, *, model=None) -> dict:
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        node = goals.get(node_id)
        if not node:
            raise ValueError("goal not found")
        context = build_agent_context(
            goals, agents, node_id,
            max_chars=int(getattr(config, "goal_ai_context_max_chars", 14000)))
        active = model or get_goal_agent_model(config, node["type"], manual=True)
        goals.conn.commit()
        agents.conn.commit()
        draft = active.harvest(context)
        return agents.create_harvest(node_id, {
            "summary": draft.summary, "insights": draft.insights, "routes": draft.routes})
    finally:
        agents.close(); goals.close()


def revise_goal_harvest(config, harvest_id: int, instruction: str, *, model=None) -> dict:
    goals = GoalStore(config.memory_db_path)
    agents = GoalAgentStore(config.memory_db_path)
    try:
        harvest = agents.harvest(harvest_id)
        node = goals.get(harvest["source_node_id"])
        if not node or harvest["status"] != "draft":
            raise ValueError("draft harvest not found")
        context = build_agent_context(
            goals, agents, node["id"],
            max_chars=int(getattr(config, "goal_ai_context_max_chars", 14000)))
        context["current_harvest_draft"] = harvest["draft"]
        active = model or get_goal_agent_model(config, node["type"], manual=True)
        goals.conn.commit()
        agents.conn.commit()
        draft = active.harvest(context, str(instruction or "").strip())
        return agents.update_harvest(harvest_id, {
            "summary": draft.summary, "insights": draft.insights, "routes": draft.routes})
    finally:
        agents.close(); goals.close()


def decide_proposal(config, proposal_id: int, action: str,
                    *, payload: dict | None = None, rationale: str = "") -> dict:
    agents = GoalAgentStore(config.memory_db_path)
    goals = GoalStore(config.memory_db_path)
    try:
        proposal = agents.get_proposal(proposal_id)
        if action == "reopen":
            node_id = agents.reopen_proposal(proposal_id)
            return {"ok": True, "status": "open", "agent": agents.node_view(node_id)}
        if proposal["status"] != "open":
            raise ValueError("proposal is no longer open")
        if action == "dismiss":
            agents.resolve_proposal(proposal_id, "dismissed")
            return {"ok": True, "status": "dismissed"}
        if action == "refine":
            return {"ok": True, "status": "open",
                    "proposal": agents.refine_proposal(proposal_id, dict(payload or {}), rationale)}
        if action != "approve":
            raise ValueError("unknown proposal action")
        target = goals.get(proposal["target_node_id"])
        if not target or target["updated_at"] != proposal["target_version"]:
            agents.resolve_proposal(proposal_id, "stale")
            raise ValueError("goal changed since this proposal was created; review it again")
        kind, data = proposal["type"], proposal["payload"]
        if kind == "create_child":
            child_type = _normalize_node_type(
                data.get("type"), parent_type=target["type"])
            goals.create(child_type, str(data.get("title") or "").strip(),
                         parent_id=target["id"], description=str(data.get("description") or ""),
                         priority=_normalize_priority(data.get("priority")),
                         due_date=data.get("due_date"))
        elif kind == "update_fields":
            changes = {k: v for k, v in data.items()
                       if k in {"description", "notes", "priority", "due_date"}}
            if "priority" in changes:
                changes["priority"] = _normalize_priority(changes["priority"])
            if not changes:
                raise ValueError("proposal has no supported fields")
            goals.update(target["id"], **changes)
        elif kind == "pause":
            goals.update(target["id"], status="paused")
        elif kind == "archive":
            goals.update(target["id"], status="archived")
        elif kind == "request_evidence":
            question = str(data.get("question") or proposal["rationale"]).strip()
            if not question:
                raise ValueError("evidence request has no question")
            agents.conn.execute(
                "INSERT INTO goal_agent_question (node_id,text,status,created_at) "
                "VALUES (?,?,'open',?)", (target["id"], crypto.enc(question), _now()))
            agents.conn.commit()
        elif kind == "start_curiosity":
            from .curiosity import CuriosityStore
            curiosities = CuriosityStore(config.memory_db_path)
            try:
                directive = str(data.get("directive") or proposal["rationale"]).strip()
                label = str(data.get("label") or target["title"]).strip()
                if not directive:
                    raise ValueError("curiosity proposal has no directive")
                curiosity_id = curiosities.add_curiosity(directive, label)
                goals.link_curiosity(target["id"], curiosity_id)
            finally:
                curiosities.close()
        elif kind == "promote_insight":
            try:
                confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < PROMOTION_CONFIDENCE_GATE:
                raise ValueError("promotion confidence is below the gate")
            detail = str(data.get("detail") or data.get("summary") or "").strip()
            if not detail:
                raise ValueError("promotion has no insight detail")
            draft = {
                "summary": str(data.get("summary") or proposal["rationale"] or detail).strip(),
                "insights": [{
                    "title": str(data.get("title") or "Promoted insight").strip(),
                    "detail": detail,
                    "kind": str(data.get("kind") or "lesson").strip(),
                    "confidence": confidence,
                    "recommended_scope_node_id": target["id"],
                }],
                "routes": [],
                "promotion": {
                    "recommended_scope_node_id": target["id"],
                    "confidence": confidence,
                    "rationale": proposal["rationale"],
                },
            }
            harvest = agents.create_harvest(proposal["agent_node_id"], draft)
            agents.commit_harvest(harvest["id"])
            agents.mark_dirty(target["id"])
        agents.resolve_proposal(proposal_id, "approved")
        agents.mark_dirty(target["id"])
        response = {"ok": True, "status": "approved", "tree": goals.tree()}
        if kind == "promote_insight":
            response["harvest_id"] = harvest["id"]
        return response
    finally:
        goals.close(); agents.close()


def promote_memory_candidate(config, candidate_id: int, action: str) -> dict:
    agents = GoalAgentStore(config.memory_db_path)
    try:
        if action == "reopen":
            node_id = agents.reopen_memory_candidate(candidate_id)
            return {"ok": True, "status": "open", "agent": agents.node_view(node_id)}
        row = agents.conn.execute(
            "SELECT * FROM goal_agent_memory_candidate WHERE id=? AND status='open'",
            (int(candidate_id),)).fetchone()
        if not row:
            raise ValueError("open memory candidate not found")
        if action == "dismiss":
            agents.resolve_memory_candidate(candidate_id, "dismissed")
            return {"ok": True, "status": "dismissed"}
        if action != "save":
            raise ValueError("unknown memory candidate action")
        from .memory import MemoryStore
        mem = MemoryStore(config.memory_db_path)
        try:
            memory_id = mem.add(
                crypto.dec(row["category"]), crypto.dec(row["attribute"]),
                crypto.dec(row["value"]), raw_source=crypto.dec(row["source_text"]),
                source_refs=[{"kind": "goal-agent-accomplishment", "goal_id": row["node_id"],
                              "candidate_id": int(candidate_id)}])
        finally:
            mem.close()
        agents.resolve_memory_candidate(candidate_id, "saved", memory_id)
        return {"ok": True, "status": "saved", "memory_id": memory_id}
    finally:
        agents.close()
