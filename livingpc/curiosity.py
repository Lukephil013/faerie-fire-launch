"""Curiosity — a goal you set, that the engine pursues on its own.

Everything else in this codebase reacts: triage tags what comes in,
inference notices patterns in what's already there, clarify cleans up loose
ends. Curiosity is the one subsystem where the user points the engine at a
domain ("understand how to help me hit my fitness goals") and it goes looking
— generating questions to ask directly, and (once it has enough confirmed
ground truth to be confident you'd want it) suggestions to try. Answers flow
back into memory as ordinary facts, feeding the loop back into itself: better
facts -> better next-round questions -> better suggestions.

A curiosity is a standing directive, not a one-shot form. It stays active
until paused or archived, gets a fresh round of items generated periodically
(and immediately on creation, and on demand), and one curiosity at a time can
be marked "greatest" — the one that gets first call on generation.

Flow (driven by the GUI, mirrors clarify.py's shape):
    set_curiosity(mem, inf, store, directive, model)   -> creates + first round
    generate_items(mem, inf, store, curiosity_id, model) -> N items queued
    answer_item(mem, store, item_id, text, model)      -> resolves a question, writes a fact
    dismiss_item(store, item_id)                       -> closes a question, never re-asked
    respond_suggestion(store, item_id, action)         -> tried | not_helpful_light |
                                                           not_helpful_heavy | dismissed
    set_greatest(store, curiosity_id)                  -> exactly one at a time
    pause_curiosity / archive_curiosity / reactivate_curiosity
    run_all_active(mem, inf, store, model, ...)        -> the periodic/scheduler pass

Two independent confidence gates, both scored by the model per item:
  - QUESTIONS need only be non-redundant given everything already asked,
    answered, or dismissed for this curiosity (default floor 0.70).
  - SUGGESTIONS need to be grounded in what's actually confirmed about the
    user — inferences they said "yes" to — since acting on a wrong guess
    costs more than asking a question that turns out redundant (default
    floor 0.80).
A round can propose as many items as genuinely warranted; nothing in the
prompt caps the mix. The per-round *storage* budget (max_open, and how many
of a round's gated items actually get written) is a code-side circuit
breaker, not something the model is told about.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import connect as db_connect
from .diagnostics import log_diag
from .memory_context import format_memories, select_memories
from . import crypto


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- schema -------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS curiosity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    directive     TEXT NOT NULL,
    label         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    is_greatest   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    last_run_at   TEXT,
    notion_page_id TEXT,
    CHECK (status IN ('active', 'paused', 'archived'))
);

CREATE TABLE IF NOT EXISTS curiosity_item (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    curiosity_id         INTEGER NOT NULL,
    kind                 TEXT NOT NULL,
    text                 TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    answer               TEXT,
    resulting_memory_id  INTEGER,
    confidence           REAL,
    created_at           TEXT,
    resolved_at          TEXT,
    metric_event_type    TEXT,
    metric_dimension_slug TEXT,
    response_type        TEXT NOT NULL DEFAULT 'text',
    CHECK (kind IN ('question', 'suggestion')),
    CHECK (status IN ('open', 'answered', 'dismissed', 'tried',
                       'not_helpful_light', 'not_helpful_heavy')),
    FOREIGN KEY (curiosity_id) REFERENCES curiosity(id)
);
CREATE INDEX IF NOT EXISTS idx_cur_item_curiosity ON curiosity_item(curiosity_id);
CREATE INDEX IF NOT EXISTS idx_cur_item_status ON curiosity_item(status);

CREATE TABLE IF NOT EXISTS curiosity_classification_proposal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    curiosity_id  INTEGER NOT NULL,
    proposal_type TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    rationale     TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    fingerprint   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    CHECK (proposal_type IN ('attach_existing','create_branch','create_root_branch',
                             'create_leaf','keep_soul','keep_investigating')),
    CHECK (status IN ('open','approved','dismissed')),
    FOREIGN KEY (curiosity_id) REFERENCES curiosity(id)
);
CREATE INDEX IF NOT EXISTS idx_curiosity_classification
ON curiosity_classification_proposal(curiosity_id,status,id DESC);

CREATE TABLE IF NOT EXISTS curiosity_classification_context (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    curiosity_id  INTEGER NOT NULL,
    proposal_id   INTEGER,
    note          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (curiosity_id) REFERENCES curiosity(id),
    FOREIGN KEY (proposal_id) REFERENCES curiosity_classification_proposal(id)
);
CREATE INDEX IF NOT EXISTS idx_curiosity_classification_context
ON curiosity_classification_context(curiosity_id,id DESC);
"""

_SUGGESTION_ACTIONS = {"tried", "not_helpful_light", "not_helpful_heavy", "dismissed"}

_LABEL_STOPWORDS = {
    "you", "your", "want", "to", "understand", "how", "help", "the", "a", "an",
    "is", "are", "it", "for", "of", "and", "greatest", "curiosity", "right",
    "now", "me", "my", "i", "this", "that", "these", "those", "week", "weekend",
    "am", "going", "be", "being", "been", "feel", "feels", "felt", "more",
    "less", "very", "really", "currently", "current", "version", "subject",
}


class CuriosityStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created by earlier versions."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(curiosity)")}
        if "notion_page_id" not in cols:
            self.conn.execute("ALTER TABLE curiosity ADD COLUMN notion_page_id TEXT")
        item_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(curiosity_item)")}
        additions = {
            "metric_event_type": "TEXT",
            "metric_dimension_slug": "TEXT",
            "response_type": "TEXT NOT NULL DEFAULT 'text'",
            "implementation_session_id": "INTEGER",
            "implementation_goal_id": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in item_cols:
                self.conn.execute(
                    f"ALTER TABLE curiosity_item ADD COLUMN {name} {declaration}")

    def _mark_linked_goal_agents_dirty(self, curiosity_id: int) -> None:
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name IN ('goal_agent_state','goal_curiosity_link','goal_node')")}
        if len(tables) != 3:
            return
        state_columns = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(goal_agent_state)").fetchall()}
        now = _now()
        linked = self.conn.execute(
            "SELECT goal_id FROM goal_curiosity_link WHERE curiosity_id=?",
            (int(curiosity_id),)).fetchall()
        for row in linked:
            current = int(row["goal_id"])
            while current:
                if {"dirty_reason", "deferred"}.issubset(state_columns):
                    self.conn.execute(
                        "UPDATE goal_agent_state SET dirty=1,dirty_reason=?,deferred=0,updated_at=? "
                        "WHERE node_id=?",
                        ("attached curiosity changed", now, current))
                else:
                    self.conn.execute(
                        "UPDATE goal_agent_state SET dirty=1,updated_at=? WHERE node_id=?",
                        (now, current))
                parent = self.conn.execute(
                    "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
                current = int(parent["parent_id"]) if parent and parent["parent_id"] else 0

    # --- curiosities ---------------------------------------------------
    def add_curiosity(self, directive: str, label: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO curiosity (directive, label, status, is_greatest, created_at) "
            "VALUES (?, ?, 'active', 0, ?)",
            (crypto.enc(directive), crypto.enc(label), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_curiosity(self, curiosity_id: int):
        row = self.conn.execute(
            "SELECT * FROM curiosity WHERE id=?", (curiosity_id,)).fetchone()
        return self._curiosity_dict(row) if row is not None else None

    def _curiosity_dict(self, r) -> dict:
        return {
            "id": r["id"], "directive": crypto.dec(r["directive"]),
            "label": crypto.dec(r["label"]),
            "status": r["status"], "is_greatest": bool(r["is_greatest"]),
            "created_at": r["created_at"], "last_run_at": r["last_run_at"],
            "notion_page_id": r["notion_page_id"] if "notion_page_id" in r.keys() else None,
        }

    def set_notion_page_id(self, curiosity_id: int, page_id: str) -> None:
        self.conn.execute(
            "UPDATE curiosity SET notion_page_id=? WHERE id=?", (page_id, curiosity_id))
        self.conn.commit()

    def rename(self, curiosity_id: int, label: str) -> dict:
        """Rename the user-facing curiosity label without changing its directive."""
        label = (label or "").strip()
        if not label:
            raise ValueError("curiosity name is required")
        cur = self.conn.execute(
            "UPDATE curiosity SET label=? WHERE id=?",
            (crypto.enc(label), int(curiosity_id)))
        if cur.rowcount != 1:
            raise ValueError(f"curiosity {curiosity_id} not found")
        self._mark_linked_goal_agents_dirty(int(curiosity_id))
        self.conn.commit()
        return self.get_curiosity(int(curiosity_id))

    def list_curiosities(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM curiosity WHERE status=? ORDER BY is_greatest DESC, id",
                (status,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM curiosity ORDER BY is_greatest DESC, id").fetchall()
        return [self._curiosity_dict(r) for r in rows]

    def set_greatest(self, curiosity_id: int, on: bool = True) -> None:
        """Mark one curiosity as the greatest (clearing any other), or with
        on=False un-mark it — clicking the star again toggles it off."""
        self.conn.execute("UPDATE curiosity SET is_greatest=0")
        if on:
            self.conn.execute(
                "UPDATE curiosity SET is_greatest=1 WHERE id=?", (curiosity_id,))
        self.conn.commit()

    def set_status(self, curiosity_id: int, status: str) -> None:
        if status not in ("active", "paused", "archived"):
            raise ValueError(f"invalid curiosity status: {status}")
        self.conn.execute(
            "UPDATE curiosity SET status=? WHERE id=?", (status, curiosity_id))
        self._mark_linked_goal_agents_dirty(int(curiosity_id))
        self.conn.commit()

    def touch(self, curiosity_id: int) -> None:
        self.conn.execute(
            "UPDATE curiosity SET last_run_at=? WHERE id=?", (_now(), curiosity_id))
        self.conn.commit()

    # --- items -----------------------------------------------------------
    def add_item(self, curiosity_id: int, kind: str, text: str, *,
                confidence: float | None = None, metric_event_type: str | None = None,
                metric_dimension_slug: str | None = None,
                response_type: str = "text") -> int:
        if metric_event_type not in {None, "assessment", "practice", "milestone"}:
            raise ValueError("invalid metric event type")
        if response_type not in {"text", "rating", "yes_no"}:
            raise ValueError("invalid curiosity response type")
        cur = self.conn.execute(
            "INSERT INTO curiosity_item (curiosity_id, kind, text, status, "
            "confidence, created_at, metric_event_type, metric_dimension_slug, response_type) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (curiosity_id, kind, crypto.enc(text), confidence, _now(), metric_event_type,
            metric_dimension_slug, response_type),
        )
        self._mark_linked_goal_agents_dirty(int(curiosity_id))
        self.conn.commit()
        return int(cur.lastrowid)

    def get_item(self, item_id: int):
        return self.conn.execute(
            "SELECT * FROM curiosity_item WHERE id=?", (item_id,)).fetchone()

    def _item_dict(self, r) -> dict:
        implementation_goal_id = (r["implementation_goal_id"]
                                  if "implementation_goal_id" in r.keys() else None)
        return {
            "id": r["id"], "curiosity_id": r["curiosity_id"], "kind": r["kind"],
            "text": crypto.dec(r["text"]),
            "status": "implemented" if implementation_goal_id else r["status"],
            "answer": crypto.dec(r["answer"]),
            "resulting_memory_id": r["resulting_memory_id"],
            "confidence": r["confidence"], "created_at": r["created_at"],
            "resolved_at": r["resolved_at"],
            "metric_event_type": r["metric_event_type"],
            "metric_dimension_slug": r["metric_dimension_slug"],
            "response_type": r["response_type"],
            "implementation_session_id": (r["implementation_session_id"]
                                              if "implementation_session_id" in r.keys() else None),
            "implementation_goal_id": implementation_goal_id,
        }

    def items_for_curiosity(self, curiosity_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM curiosity_item WHERE curiosity_id=? ORDER BY id",
            (curiosity_id,)).fetchall()
        return [self._item_dict(r) for r in rows]

    def item_counts(self, curiosity_id: int) -> dict:
        rows = self.conn.execute(
            "SELECT status,kind,COUNT(*) AS count FROM curiosity_item "
            "WHERE curiosity_id=? GROUP BY status,kind",
            (int(curiosity_id),)).fetchall()
        counts = {
            "questions": 0,
            "suggestions": 0,
            "open_questions": 0,
            "open_suggestions": 0,
            "answered": 0,
            "dismissed": 0,
            "resolved": 0,
            "total": 0,
        }
        for row in rows:
            n = int(row["count"])
            kind = row["kind"]
            status = row["status"]
            counts["total"] += n
            if kind == "question":
                counts["questions"] += n
            elif kind == "suggestion":
                counts["suggestions"] += n
            if status == "open":
                if kind == "question":
                    counts["open_questions"] += n
                elif kind == "suggestion":
                    counts["open_suggestions"] += n
            else:
                counts["resolved"] += n
                if status == "answered":
                    counts["answered"] += n
                elif status == "dismissed":
                    counts["dismissed"] += n
        return counts

    def open_items(self, curiosity_id: int | None = None) -> list[dict]:
        if curiosity_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM curiosity_item WHERE status='open' AND "
                "curiosity_id=? ORDER BY id", (curiosity_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM curiosity_item WHERE status='open' "
                "ORDER BY id").fetchall()
        return [self._item_dict(r) for r in rows]

    def resolved(self, curiosity_id: int | None = None, limit: int = 25) -> list[dict]:
        if curiosity_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM curiosity_item WHERE status!='open' AND "
                "curiosity_id=? ORDER BY resolved_at DESC LIMIT ?",
                (curiosity_id, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM curiosity_item WHERE status!='open' "
                "ORDER BY resolved_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._item_dict(r) for r in rows]

    def mark_answered(self, item_id: int, answer: str,
                      resulting_memory_id: int | None) -> None:
        row = self.get_item(item_id)
        self.conn.execute(
            "UPDATE curiosity_item SET status='answered', answer=?, "
            "resulting_memory_id=?, resolved_at=? WHERE id=?",
            (crypto.enc(answer), resulting_memory_id, _now(), item_id))
        if row:
            self._mark_linked_goal_agents_dirty(int(row["curiosity_id"]))
        self.conn.commit()

    def mark_dismissed(self, item_id: int) -> None:
        row = self.get_item(item_id)
        self.conn.execute(
            "UPDATE curiosity_item SET status='dismissed', resolved_at=? WHERE id=?",
            (_now(), item_id))
        if row:
            self._mark_linked_goal_agents_dirty(int(row["curiosity_id"]))
        self.conn.commit()

    def mark_suggestion_resolved(self, item_id: int, status: str) -> None:
        row = self.get_item(item_id)
        self.conn.execute(
            "UPDATE curiosity_item SET status=?, resolved_at=? WHERE id=?",
            (status, _now(), item_id))
        if row:
            self._mark_linked_goal_agents_dirty(int(row["curiosity_id"]))
        self.conn.commit()

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM curiosity_item GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    # --- classification proposals ---------------------------------------
    def add_classification_proposal(self, curiosity_id: int,
                                    proposal: ClassificationProposal) -> int | None:
        if proposal.proposal_type not in CLASSIFICATION_TYPES:
            raise ValueError("invalid classification proposal type")
        payload = proposal.payload if isinstance(proposal.payload, dict) else {}
        canonical = json.dumps({
            "type": proposal.proposal_type,
            "payload": payload,
        }, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        duplicate = self.conn.execute(
            "SELECT id FROM curiosity_classification_proposal "
            "WHERE curiosity_id=? AND fingerprint=? AND status IN ('open','dismissed') "
            "ORDER BY id DESC LIMIT 1",
            (int(curiosity_id), fingerprint),
        ).fetchone()
        if duplicate:
            return None
        cur = self.conn.execute(
            "INSERT INTO curiosity_classification_proposal "
            "(curiosity_id,proposal_type,payload_json,rationale,status,fingerprint,created_at) "
            "VALUES (?,?,?,?,'open',?,?)",
            (int(curiosity_id), proposal.proposal_type, crypto.enc(canonical),
             crypto.enc(proposal.rationale), fingerprint, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def classification_proposals(self, curiosity_id: int,
                                 status: str | None = "open") -> list[dict]:
        where = ["curiosity_id=?"]
        args: list[object] = [int(curiosity_id)]
        if status is not None:
            where.append("status=?")
            args.append(status)
        rows = self.conn.execute(
            "SELECT * FROM curiosity_classification_proposal WHERE "
            + " AND ".join(where) + " ORDER BY id DESC", args).fetchall()
        return [self._classification_dict(row) for row in rows]

    def get_classification_proposal(self, proposal_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM curiosity_classification_proposal WHERE id=?",
            (int(proposal_id),)).fetchone()
        if not row:
            raise ValueError("classification proposal not found")
        return self._classification_dict(row)

    def _classification_dict(self, row) -> dict:
        data = json.loads(crypto.dec(row["payload_json"]) or "{}")
        return {
            "id": int(row["id"]),
            "curiosity_id": int(row["curiosity_id"]),
            "type": row["proposal_type"],
            "payload": data.get("payload") or {},
            "rationale": crypto.dec(row["rationale"]) or "",
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    def resolve_classification_proposal(self, proposal_id: int, status: str) -> None:
        if status not in {"approved", "dismissed"}:
            raise ValueError("invalid classification proposal resolution")
        self.conn.execute(
            "UPDATE curiosity_classification_proposal SET status=?,resolved_at=? "
            "WHERE id=? AND status='open'",
            (status, _now(), int(proposal_id)))
        self.conn.commit()

    def add_classification_context(self, curiosity_id: int, note: str,
                                   proposal_id: int | None = None) -> int:
        note = (note or "").strip()
        if not note:
            raise ValueError("context note is empty")
        cur = self.conn.execute(
            "INSERT INTO curiosity_classification_context "
            "(curiosity_id,proposal_id,note,created_at) VALUES (?,?,?,?)",
            (int(curiosity_id), None if proposal_id is None else int(proposal_id),
             crypto.enc(note), _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def classification_contexts(self, curiosity_id: int, limit: int = 12) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM curiosity_classification_context WHERE curiosity_id=? "
            "ORDER BY id DESC LIMIT ?",
            (int(curiosity_id), int(limit))).fetchall()
        return [{
            "id": int(row["id"]),
            "curiosity_id": int(row["curiosity_id"]),
            "proposal_id": row["proposal_id"],
            "note": crypto.dec(row["note"]) or "",
            "created_at": row["created_at"],
        } for row in rows]

    def close(self) -> None:
        self.conn.close()


# --- prompts ------------------------------------------------------------
CURIOSITY_SYSTEM = """\
You are the curiosity stage of a personal "second brain" memory system. The
user has set a DIRECTIVE — a domain they explicitly want you to investigate
and learn about them, in service of helping them with it. Your job is to
generate the next round of items that move that directive forward.

Two kinds of item:
- QUESTION: something to ask the user directly. Must be genuinely new —
  not a rewording of anything already pending, already answered, or already
  dismissed for this directive.
- SUGGESTION: something concrete to try, grounded in what's actually
  confirmed about the user (their confirmed beliefs, and facts already on
  record) — not a generic tip. Only propose a suggestion when you have real
  grounding for it; if you don't, ask a question instead.

Score every item's confidence 0.0-1.0:
- For a QUESTION, confidence means "this is not redundant with anything
  already asked, answered, or dismissed, and likely to reduce uncertainty" —
  low if it overlaps prior ground or asks for an essay when a smaller
  distinction would do; high if it's a clear, novel, answerable angle.
- For a SUGGESTION, confidence means "the user would actually want this,
  based on what's confirmed about them" — low if it's a generic guess, high
  if it follows directly from their confirmed beliefs and facts.

Return a batch of new items — a mix of QUESTIONs and SUGGESTIONs, as many as
genuinely warranted (usually 2-5):
- Prefer small, low-burden questions over broad essay prompts.
- If the current investigation has only the initial journal/current-framing
  answer, start with highly clarifying yes/no or "kind of" questions to get a
  quick handle on the context.
- After that, mix yes/no questions with short text questions.
- A good question should name the uncertainty it would resolve.
- Once two or more answers plus confirmed context support a concrete next step,
  include at least one grounded SUGGESTION. Do not let novel questions crowd
  every actionable next step out of the batch.

When APPROVED METRIC DIMENSIONS are present, a question may be a structured
assessment only if it directly asks the user to rate one listed dimension.
Tag that item with metric_event_type="assessment", the exact dimension slug,
and response_type="rating". Ordinary reflective questions remain untagged.
Suggestions may be tagged metric_event_type="practice" and a dimension slug
only when the action directly exercises that dimension.

Return STRICT JSON only:
{"items": [{"kind": "question"|"suggestion", "text": str, "confidence": number,
"metric_event_type": "assessment"|"practice"|null,
"metric_dimension_slug": str|null, "response_type": "text"|"rating"|"yes_no"}]}
"""

CURIOSITY_RESOLVE_SYSTEM = """\
You are the fact-extraction stage of a personal "second brain". The user just
answered a question that was asked in service of a DIRECTIVE (a goal they set
for what you should learn about them). Turn their answer into ONE clean,
confident memory fact — a short attribute label and a value stating what's
now true — using only what they actually said, nothing invented or implied
beyond it.

Return STRICT JSON only: {"attribute": str, "value": str}
"""


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.DOTALL)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


@dataclass
class GeneratedItem:
    kind: str
    text: str
    confidence: float
    metric_event_type: str | None = None
    metric_dimension_slug: str | None = None
    response_type: str = "text"


@dataclass
class ClassificationProposal:
    proposal_type: str
    payload: dict
    rationale: str


@dataclass
class CuriosityContext:
    facts_block: str
    pending_block: str
    qa_block: str
    dismissed_block: str
    suggestions_block: str
    beliefs_block: str
    metric_block: str = "  (none)"
    classification_context_block: str = "  (none)"
    core_profile_block: str = "  (none yet)"


CLASSIFICATION_TYPES = {
    "attach_existing", "create_branch", "create_root_branch", "create_leaf",
    "keep_soul", "keep_investigating",
}


def build_curiosity_prompt(directive: str, context: CuriosityContext) -> str:
    return "\n".join([
        f"DIRECTIVE: {directive}\n",
        "ALWAYS-ON CORE PROFILE (stable basics and hard constraints):\n"
        + context.core_profile_block + "\n",
        "CURRENT INVESTIGATION JOURNAL AND ANSWERS (treat this as freshest/most authoritative):\n"
        + context.qa_block + "\n",
        "USER CORRECTIONS / HARD CONSTRAINTS FOR THIS INVESTIGATION:\n"
        + context.classification_context_block + "\n",
        "WHAT YOU ALREADY KNOW (older relevant memory; use only as tentative background):\n"
        + context.facts_block + "\n",
        "STILL WAITING ON YOUR RESPONSE (don't duplicate these):\n" + context.pending_block + "\n",
        "QUESTIONS DISMISSED without an answer (don't re-ask):\n" + context.dismissed_block + "\n",
        "PRIOR SUGGESTIONS (status: tried / not_helpful_light / not_helpful_heavy / dismissed):\n"
        + context.suggestions_block + "\n",
        "CONFIRMED BELIEFS about you (ground suggestion confidence in these):\n"
        + context.beliefs_block + "\n",
        "APPROVED METRIC DIMENSIONS (use exact slugs for structured items):\n"
        + context.metric_block + "\n",
        "Ask follow-up questions that get to the crux of the user's current framing. "
        "Do not presuppose an old belief when the fresh journal/Q&A contradicts it. "
        "Return new items as STRICT JSON.",
    ])


def build_curiosity_resolve_prompt(directive: str, question: str, answer: str) -> str:
    return (f"DIRECTIVE: {directive}\n"
            f"QUESTION ASKED: {question}\n"
            f"USER'S ANSWER: {answer}")


# --- Notion summary (the "consolidated essentials" mirrored to Notion) -----
NOTION_SUMMARY_SYSTEM = """\
You are writing a living page that consolidates everything currently
understood about ONE curiosity — a goal the user asked this system to pursue.
Distill it down to the essentials someone could read in 30 seconds and know
exactly where things stand. Use short markdown: a one-line restatement of the
goal, a short bulleted list of the most important things now known (drawn
only from confirmed facts and answered questions — never invent or
speculate), and a short "Direction" section naming the most useful next step
or focus given everything so far. No filler, no repeating raw Q&A verbatim,
no hedging language. If very little is known yet, say so plainly instead of
padding with generic advice.

Return STRICT JSON only: {"markdown": str}
"""


def build_notion_summary_prompt(curiosity: dict, context: CuriosityContext) -> str:
    return "\n".join([
        f"GOAL: {curiosity['directive']}\n",
        "WHAT'S CONFIRMED (memory facts relevant to this goal):\n" + context.facts_block + "\n",
        "RESOLVED Q&A so far:\n" + context.qa_block + "\n",
        "SUGGESTIONS TRIED / RESPONSES:\n" + context.suggestions_block + "\n",
        "CONFIRMED BELIEFS ABOUT THE USER (broader context):\n" + context.beliefs_block + "\n",
        "Write the consolidated essentials as STRICT JSON.",
    ])


CLASSIFICATION_SYSTEM = """\
You classify an Investigation into the user's Soul/Root/Branch/Leaf goal tree.
The Investigation may begin unattached. Your job is to propose where it should
go only after considering the existing tree and the evidence gathered so far.

Return STRICT JSON only:
{"proposals":[{"type":str,"payload":object,"rationale":str}]}

Allowed proposal types and payloads:
- attach_existing: {"goal_id": int}
- create_branch: {"parent_id": int, "title": str, "description": str}
- create_root_branch: {"root_title": str, "root_description": str,
  "branch_title": str, "branch_description": str}
- create_leaf: {"parent_id": int, "title": str, "description": str,
  "priority": "low"|"normal"|"high"}
- keep_soul: {"note": str}
- keep_investigating: {"question": str}

Prefer the smallest fitting proposal:
1. attach_existing if a good node already exists.
2. create_branch under an existing Root/Branch if the domain exists but the
   specific mechanism does not.
3. create_root_branch only if the investigation reveals a missing life domain.
4. create_leaf only when action is already clear.
5. keep_soul for self-understanding with no current action.
6. keep_investigating when there is not enough evidence.

Never claim a proposal is applied. These are suggestions for user approval.
"""


def build_classification_prompt(curiosity: dict, context: CuriosityContext,
                                tree_summary: str, attached_summary: str) -> str:
    return "\n".join([
        f"INVESTIGATION LABEL: {curiosity['label']}",
        f"INVESTIGATION QUESTION/DIRECTIVE: {curiosity['directive']}\n",
        "CURRENT ATTACHMENTS:\n" + attached_summary + "\n",
        "EXISTING SOUL TREE:\n" + tree_summary + "\n",
        "ALWAYS-ON CORE PROFILE (stable basics and hard constraints):\n"
        + context.core_profile_block + "\n",
        "RELEVANT MEMORY FACTS / HARD CONTEXT:\n" + context.facts_block + "\n",
        "USER CORRECTIONS / HARD CONSTRAINTS FOR PROPOSALS:\n"
        + context.classification_context_block + "\n",
        "RESOLVED Q&A / EVIDENCE SO FAR:\n" + context.qa_block + "\n",
        "OPEN QUESTIONS:\n" + context.pending_block + "\n",
        "PRIOR SUGGESTIONS / EXPERIMENTS:\n" + context.suggestions_block + "\n",
        "Return classification proposals as STRICT JSON.",
    ])


def parse_items(text: str) -> list[GeneratedItem]:
    data = _extract_json(text) or {}
    out: list[GeneratedItem] = []
    for raw in data.get("items", []) or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "")).strip().lower()
        if kind not in ("question", "suggestion"):
            continue
        item_text = str(raw.get("text", "")).strip()
        if not item_text:
            continue
        try:
            conf = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        event_type = str(raw.get("metric_event_type") or "").strip().lower() or None
        if event_type not in {None, "assessment", "practice"}:
            event_type = None
        slug = re.sub(r"[^a-z0-9_]+", "_", str(
            raw.get("metric_dimension_slug") or "").strip().lower()).strip("_") or None
        response_type = str(raw.get("response_type") or "text").strip().lower()
        if response_type not in {"text", "rating", "yes_no"}:
            response_type = "text"
        if kind == "question" and event_type != "assessment":
            event_type, slug = None, None
            if response_type not in {"text", "yes_no"}:
                response_type = "text"
        if kind == "suggestion" and event_type != "practice":
            event_type, slug, response_type = None, None, "text"
        out.append(GeneratedItem(
            kind=kind, text=item_text, confidence=max(0.0, min(1.0, conf)),
            metric_event_type=event_type, metric_dimension_slug=slug,
            response_type=response_type))
    return out


def parse_classification_proposals(text: str) -> list[ClassificationProposal]:
    data = _extract_json(text) or {}
    out: list[ClassificationProposal] = []
    for raw in data.get("proposals", []) or []:
        if not isinstance(raw, dict):
            continue
        proposal_type = str(raw.get("type") or "").strip().lower()
        if proposal_type not in CLASSIFICATION_TYPES:
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        rationale = str(raw.get("rationale") or "").strip()
        out.append(ClassificationProposal(proposal_type, payload, rationale))
    return out


def _first_goal_id_named(tree_summary: str, title: str) -> int | None:
    pattern = re.compile(r"\bid=(\d+)\b[^\n]*\btitle=" + re.escape(title) + r"\b",
                         re.IGNORECASE)
    match = pattern.search(tree_summary or "")
    return int(match.group(1)) if match else None


# --- models ---------------------------------------------------------------
class StubCuriosityModel:
    """Offline/deterministic — lets the whole curiosity flow run and be
    tested without an API key. A suggestion only appears once there's been
    some real back-and-forth to ground it."""

    def generate(self, directive: str, context: CuriosityContext) -> list[GeneratedItem]:
        n_answers = context.qa_block.count("\n  A:") + context.qa_block.count("  A:")
        items = [
            GeneratedItem(
                "question",
                f'What does progress on "{directive[:50].strip()}" look like to you?',
                0.9),
            GeneratedItem(
                "question",
                "What's the biggest thing getting in the way of that right now?",
                0.88),
        ]
        if n_answers >= 2:
            items.append(GeneratedItem(
                "suggestion",
                "Based on what you've shared, pick one small, concrete target for "
                "this week and track it daily.",
                0.85))
        return items

    def resolve(self, directive: str, question: str, answer: str) -> dict:
        attribute = question.strip().rstrip("?").strip()[:60] or "note"
        return {"attribute": attribute, "value": (answer or "").strip()}

    def summarize(self, curiosity: dict, context: CuriosityContext) -> str:
        known = context.qa_block if context.qa_block != "  (none yet)" else None
        if not known:
            return (f"**Goal:** {curiosity['directive']}\n\n"
                    "Nothing confirmed yet — still gathering answers.")
        return (f"**Goal:** {curiosity['directive']}\n\n"
                f"**Known so far:**\n{known}\n\n"
                "**Direction:** keep answering open questions to sharpen this.")

    def classify(self, curiosity: dict, context: CuriosityContext,
                 tree_summary: str, attached_summary: str) -> list[ClassificationProposal]:
        directive = (curiosity.get("directive") or "").lower()
        answered = context.qa_block != "  (none yet)"
        if not answered:
            return [ClassificationProposal(
                "keep_investigating",
                {"question": "Answer a few investigation questions before classification."},
                "There is not enough answered evidence to place this confidently.")]
        if any(word in directive for word in ("social", "people", "meeting", "interaction")):
            mental_health_id = _first_goal_id_named(tree_summary, "Mental Health")
            if mental_health_id:
                return [ClassificationProposal(
                    "create_branch",
                    {"parent_id": mental_health_id,
                     "title": "Reduce social threat response",
                     "description": "Understand and reduce dread around meeting new people, social uncertainty, and perceived social obligation."},
                    "The investigation appears related to social threat response and fits under the existing Mental Health Root.")]
            return [ClassificationProposal(
                "create_root_branch",
                {"root_title": "Social Life / Connection",
                 "root_description": "A domain for building safe, energizing social connection.",
                 "branch_title": "Reduce dread around meeting new people",
                 "branch_description": "Explore and reduce dread before new or uncertain social interactions."},
                "No existing Root clearly owns this social-connection domain.")]
        return [ClassificationProposal(
            "keep_soul",
            {"note": "This investigation currently looks like general self-understanding."},
            "The answered evidence does not clearly require a goal placement yet.")]


class ClaudeCuriosityModel:
    """Anthropic-backed; one call per generation round, one per answer."""

    def __init__(self, model: str = "claude-haiku-4-5", api_key: str | None = None,
                max_tokens: int = 900, timeout_seconds: float = 60.0,
                usage_category: str = "manual"):
        self.model = model
        self.max_tokens = max_tokens
        self.usage_category = usage_category
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use curiosity_backend=stub.")
        from anthropic import Anthropic  # lazy import
        self._client = Anthropic(api_key=key, timeout=timeout_seconds)

    def _call(self, system: str, user: str) -> str:
        started = time.monotonic()
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        from .llm_usage import record_response
        record_response(self.usage_category, self.model, msg, time.monotonic() - started)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def generate(self, directive: str, context: CuriosityContext) -> list[GeneratedItem]:
        prompt = build_curiosity_prompt(directive, context)
        log_diag("prompt", f"surface=curiosity-generate model={self.model} "
                 f"input_chars={len(CURIOSITY_SYSTEM) + len(prompt)}")
        return parse_items(self._call(CURIOSITY_SYSTEM, prompt))

    def resolve(self, directive: str, question: str, answer: str) -> dict:
        prompt = build_curiosity_resolve_prompt(directive, question, answer)
        log_diag("prompt", f"surface=curiosity-resolve model={self.model}")
        data = _extract_json(self._call(CURIOSITY_RESOLVE_SYSTEM, prompt))
        attribute = str(data.get("attribute") or "").strip() or "note"
        value = str(data.get("value") or answer).strip()
        return {"attribute": attribute, "value": value}

    def summarize(self, curiosity: dict, context: CuriosityContext) -> str:
        prompt = build_notion_summary_prompt(curiosity, context)
        log_diag("prompt", f"surface=curiosity-notion-summary model={self.model}")
        data = _extract_json(self._call(NOTION_SUMMARY_SYSTEM, prompt))
        return str(data.get("markdown") or "").strip()

    def classify(self, curiosity: dict, context: CuriosityContext,
                 tree_summary: str, attached_summary: str) -> list[ClassificationProposal]:
        prompt = build_classification_prompt(curiosity, context, tree_summary,
                                             attached_summary)
        log_diag("prompt", f"surface=curiosity-classify model={self.model} "
                 f"input_chars={len(CLASSIFICATION_SYSTEM) + len(prompt)}")
        return parse_classification_proposals(self._call(CLASSIFICATION_SYSTEM, prompt))


def get_curiosity_model(config, *, usage_category: str = "manual"):
    backend = (getattr(config, "curiosity_backend", "") or
              getattr(config, "inference_backend", "claude")).lower()
    if backend == "stub":
        return StubCuriosityModel()
    return ClaudeCuriosityModel(
        model=getattr(config, "curiosity_model", "claude-haiku-4-5"),
        timeout_seconds=getattr(config, "llm_timeout_seconds", 60.0),
        usage_category=usage_category)


# --- the flow the GUI drives -------------------------------------------------
def _default_label(directive: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", directive or "")
    significant = [w for w in words if w.lower() not in _LABEL_STOPWORDS]
    pick = (significant or words)[:3]
    return " ".join(pick).lower() or "general"


def _journal_label(journal_text: str) -> str:
    """Small deterministic topic label for journal-created Investigation tabs."""
    text = " ".join((journal_text or "").lower().split())
    has = lambda *words: any(word in text for word in words)
    if has("new people", "social", "friend", "friends", "meeting people"):
        if has("dread", "anxious", "anxiety", "fear", "avoid"):
            return "social dread"
        return "social connection"
    if has("exercise", "fitness", "gym", "workout", "hiking", "climbing"):
        if has("dislike", "hate", "avoid", "dread"):
            return "exercise fit"
        return "fitness"
    if has("work", "job", "parsons", "career", "meeting", "inbox", "email"):
        if has("dread", "anxious", "anxiety", "avoid"):
            return "work dread"
        return "work"
    if has("korean", "language", "grammar", "vocab"):
        return "korean study"
    if has("sleep", "tired", "fatigue", "energy", "burnout", "burnt out"):
        return "energy"
    return _default_label(journal_text)


def _journal_directive(journal_text: str) -> str:
    first_line = next((line.strip() for line in journal_text.splitlines()
                       if line.strip()), journal_text.strip())
    question = re.split(r"(?<=[?!.])\s+", first_line, maxsplit=1)[0].strip()
    return (question or first_line).strip()[:240]


_TOPIC_STOPWORDS = _LABEL_STOPWORDS | {
    "investigation", "investigations", "curiosity", "curiosities", "journal",
    "current", "framing", "about", "around", "from", "into", "with", "that",
    "this", "does", "what", "why", "when", "where", "really", "thing",
    "things", "issue", "issues",
}


def _topic_tokens(*texts: str) -> set[str]:
    tokens = set()
    for text in texts:
        for raw in re.findall(r"[A-Za-z][A-Za-z'-]*", text or ""):
            word = raw.lower().strip("'")
            if word.endswith("'s"):
                word = word[:-2]
            if len(word) < 3 or word in _TOPIC_STOPWORDS:
                continue
            tokens.add(word)
    return tokens


def _similar_topic(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    smaller = min(len(a), len(b))
    if smaller < 2:
        return False
    overlap = len(a & b)
    return overlap == smaller and overlap / len(a | b) >= 0.5


def _find_similar_active_curiosity(store: CuriosityStore, *,
                                   label: str, directive: str) -> dict | None:
    wanted_label = _topic_tokens(label)
    wanted = _topic_tokens(label, directive)
    if not wanted:
        return None
    candidates = sorted(store.list_curiosities("active"),
                        key=lambda c: (not c.get("is_greatest"), c["id"]))
    for curiosity in candidates:
        existing_label = _topic_tokens(curiosity.get("label", ""))
        existing = _topic_tokens(curiosity.get("label", ""),
                                 curiosity.get("directive", ""))
        if (_similar_topic(wanted_label, existing_label) or
                _similar_topic(wanted_label, existing) or
                _similar_topic(wanted, existing_label) or
                _similar_topic(wanted, existing)):
            return curiosity
    return None


def _domain_memory_facts(memories: list[dict], directive: str) -> list[dict]:
    """Deterministic hard-context supplement for domains where missing one
    stable fact can make proposals incoherent. This is intentionally small and
    additive; semantic retrieval still supplies the general context."""
    text = (directive or "").lower()
    career_trigger = {
        "work", "career", "job", "parsons", "freelance", "contract",
        "income", "employed", "employment", "role", "company", "interview",
    }
    if not any(word in text for word in career_trigger):
        return []
    career_terms = career_trigger | {
        "current job", "current role", "title", "autodesk", "salesforce",
        "leave", "replacement", "resume", "application",
    }
    selected = []
    seen: set[int] = set()
    for memory in memories:
        haystack = " ".join([
            str(memory.get("category") or ""),
            str(memory.get("attribute") or ""),
            str(memory.get("value") or ""),
        ]).lower()
        if any(term in haystack for term in career_terms):
            memory_id = int(memory.get("id") or 0)
            if memory_id not in seen:
                selected.append(memory)
                seen.add(memory_id)
        if len(selected) >= 12:
            break
    return selected


def _commit_open_connections(*owners) -> None:
    """Release any accidental SQLite transaction before model/network calls.

    A connection can enter a write transaction through an ignored insert,
    migration, or dirty-propagation helper. The intended writes in these flows
    commit immediately, so this should normally be a no-op; when it is not, it
    prevents holding memory.db's single write lock across slow LLM/HTTP work.
    """
    for owner in owners:
        conn = getattr(owner, "conn", None)
        if conn is not None:
            conn.commit()


def _context_note_block(store: CuriosityStore, curiosity_id: int) -> str:
    notes = store.classification_contexts(curiosity_id, limit=8)
    return "\n".join(
        f"  - {note['note']}" for note in reversed(notes)) or "  (none)"


def _build_context(mem, inf, store: CuriosityStore, curiosity_id: int) -> CuriosityContext:
    curiosity = store.get_curiosity(curiosity_id)
    directive = curiosity["directive"]

    facts = mem.active_as_dicts()
    selection = select_memories(facts, directive, max_items=24, max_chars=2000)
    selected = list(selection.memories)
    selected_ids = {int(memory.get("id") or 0) for memory in selected}
    for memory in _domain_memory_facts(facts, directive):
        memory_id = int(memory.get("id") or 0)
        if memory_id not in selected_ids:
            selected.append(memory)
            selected_ids.add(memory_id)
    facts_block = format_memories(selected[:32]) or "(none yet)"

    items = store.items_for_curiosity(curiosity_id)
    pending_lines: list[str] = []
    qa_lines: list[str] = []
    dismissed_lines: list[str] = []
    sugg_lines: list[str] = []
    for it in items:
        if it["kind"] == "question":
            if it["status"] == "open":
                pending_lines.append(f"  - {it['text']}")
            elif it["status"] == "answered":
                qa_lines.append(f"  Q: {it['text']}\n  A: {it['answer']}")
            elif it["status"] == "dismissed":
                dismissed_lines.append(f"  - {it['text']}")
        else:  # suggestion
            if it["status"] == "open":
                pending_lines.append(f"  - {it['text']}")
            else:
                sugg_lines.append(f"  - {it['text']} [{it['status']}]")

    beliefs = inf.confirmed() if inf is not None else []
    beliefs_block = "\n".join(f"  - {b['statement']}" for b in beliefs[:12]) or "  (none yet)"

    metric_block = "  (none)"
    profile_row = store.conn.execute(
        "SELECT status,dimensions_json FROM curiosity_metric_profile WHERE curiosity_id=?",
        (curiosity_id,)).fetchone() if store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curiosity_metric_profile'"
        ).fetchone() else None
    if profile_row and profile_row["status"] == "approved":
        dimensions = json.loads(profile_row["dimensions_json"])
        metric_block = "\n".join(
            f"  - {item['slug']}: {item['label']}" for item in dimensions) or "  (none)"

    return CuriosityContext(
        facts_block=facts_block,
        pending_block="\n".join(pending_lines) or "  (none)",
        qa_block="\n".join(qa_lines) or "  (none yet)",
        dismissed_block="\n".join(dismissed_lines) or "  (none)",
        suggestions_block="\n".join(sugg_lines) or "  (none yet)",
        beliefs_block=beliefs_block,
        metric_block=metric_block,
        classification_context_block=_context_note_block(store, curiosity_id),
        core_profile_block=mem.core_profile_block(max_facts=50, max_chars=3500),
    )


def _goal_type_label(node_type: str) -> str:
    return {"umbrella": "Soul", "overgoal": "Root",
            "subgoal": "Branch", "task": "Leaf"}.get(node_type, node_type)


def _tree_summary(goals) -> str:
    tree = goals.tree()
    lines: list[str] = []

    def visit(node: dict, depth: int = 0) -> None:
        if node.get("status") == "archived":
            return
        indent = "  " * depth
        title = node.get("title") or ""
        desc = (node.get("description") or "").strip()
        desc_part = f" — {desc[:160]}" if desc else ""
        lines.append(
            f"{indent}- id={node['id']} type={_goal_type_label(node['type'])} "
            f"title={title}{desc_part}")
        for child in node.get("children") or []:
            visit(child, depth + 1)

    visit(tree)
    return "\n".join(lines) or "  (no goal tree)"


def _attached_summary(store: CuriosityStore, curiosity_id: int) -> str:
    if not store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_curiosity_link'"
    ).fetchone():
        return "  (none)"
    rows = store.conn.execute(
        "SELECT g.id,g.node_type,g.title FROM goal_curiosity_link l "
        "JOIN goal_node g ON g.id=l.goal_id WHERE l.curiosity_id=? "
        "ORDER BY g.id", (int(curiosity_id),)).fetchall()
    return "\n".join(
        f"  - id={row['id']} type={_goal_type_label(row['node_type'])} "
        f"title={crypto.dec(row['title'])}" for row in rows) or "  (none)"


def classify_curiosity(config, mem, inf, store: CuriosityStore, curiosity_id: int,
                       model=None) -> dict:
    from .goals import GoalStore
    curiosity = store.get_curiosity(int(curiosity_id))
    if not curiosity:
        raise ValueError("investigation not found")
    goals = GoalStore(config.memory_db_path)
    try:
        context = _build_context(mem, inf, store, int(curiosity_id))
        tree_summary = _tree_summary(goals)
        attached_summary = _attached_summary(store, int(curiosity_id))
    finally:
        goals.close()

    # Model calls can take many seconds. Do not keep any accidental write
    # transaction open across that boundary; on Windows this is a common source
    # of "database is locked" when another UI action writes to the same file.
    _commit_open_connections(mem, inf, store)

    active = model or get_curiosity_model(config, usage_category="manual")
    proposals = active.classify(
        curiosity, context, tree_summary, attached_summary)
    created = []
    for proposal in proposals[:4]:
        proposal_id = store.add_classification_proposal(int(curiosity_id), proposal)
        if proposal_id:
            created.append(proposal_id)
    return {"created": len(created), "proposal_ids": created,
            "proposals": store.classification_proposals(int(curiosity_id))}


def _compact_line(text: str, limit: int = 260) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _classification_origin(curiosity: dict, proposal: dict, store: CuriosityStore) -> dict:
    """Durable recap for a Goal node born from an Investigation placement."""
    curiosity_id = int(curiosity["id"])
    answered = [
        item for item in store.items_for_curiosity(curiosity_id)
        if item["kind"] == "question" and item["status"] == "answered"
    ]
    qa_summary = []
    qa_detail = []
    for item in answered:
        question_raw = str(item["text"] or "")
        answer_raw = str(item.get("answer") or "")
        question = _compact_line(question_raw, 160)
        answer = _compact_line(answer_raw, 240)
        if question or answer:
            qa_summary.append(f"Q: {question}\nA: {answer}".strip())
        if question_raw or answer_raw:
            qa_detail.append(f"Q: {question_raw}\nA: {answer_raw}".strip())
    contexts = [
        str(row["note"] or "")
        for row in store.classification_contexts(curiosity_id, limit=50)
        if row.get("note")
    ]
    placement = str(proposal.get("rationale") or "").strip()
    summary_parts = [
        f"Created from Investigation “{curiosity['label']}”.",
        f"Question/directive: {_compact_line(curiosity['directive'], 260)}",
    ]
    if placement:
        summary_parts.append(f"Placement rationale: {_compact_line(placement, 320)}")
    if qa_summary:
        summary_parts.append("Known so far: " + " | ".join(
            _compact_line(line.replace("\n", " "), 220) for line in qa_summary[:3]))
    detail_sections = [
        f"Investigation label: {curiosity['label']}",
        f"Question/directive: {curiosity['directive']}",
    ]
    if placement:
        detail_sections.append(f"Placement rationale: {placement}")
    if proposal.get("payload"):
        detail_sections.append("Approved proposal payload:\n" + json.dumps(
            proposal["payload"], ensure_ascii=False, indent=2))
    if contexts:
        detail_sections.append("User corrections / constraints:\n" + "\n".join(
            f"- {note}" for note in contexts))
    if qa_detail:
        detail_sections.append("Answered investigation evidence:\n" + "\n\n".join(qa_detail))
    return {
        "source_kind": "investigation",
        "source_id": curiosity_id,
        "source_proposal_id": int(proposal["id"]),
        "source_label": str(curiosity["label"] or ""),
        "summary": "\n".join(summary_parts),
        "detail": "\n\n".join(detail_sections),
    }


def decide_classification_proposal(config, proposal_id: int, action: str) -> dict:
    from .goals import GoalStore
    store = CuriosityStore(config.memory_db_path)
    goals = GoalStore(config.memory_db_path)
    try:
        proposal = store.get_classification_proposal(int(proposal_id))
        curiosity = store.get_curiosity(int(proposal["curiosity_id"]))
        if not curiosity:
            raise ValueError("investigation not found")
        if proposal["status"] != "open":
            raise ValueError("classification proposal is no longer open")
        if action == "dismiss":
            store.resolve_classification_proposal(int(proposal_id), "dismissed")
            return {"ok": True, "status": "dismissed"}
        if action != "approve":
            raise ValueError("unknown classification action")
        payload = proposal["payload"]
        curiosity_id = proposal["curiosity_id"]
        proposal_type = proposal["type"]
        attached_goal_id = None
        created_question_id = None
        origin = _classification_origin(curiosity, proposal, store)
        origin_goal_ids: list[int] = []
        if proposal_type == "attach_existing":
            attached_goal_id = int(payload["goal_id"])
            goals.link_curiosity(attached_goal_id, curiosity_id)
        elif proposal_type == "create_branch":
            parent_id = int(payload["parent_id"])
            parent = goals.get(parent_id)
            if not parent:
                raise ValueError("parent goal not found")
            node_type = "overgoal" if parent["type"] == "umbrella" else "subgoal"
            attached_goal_id = goals.create(
                node_type,
                str(payload.get("title") or "New Branch"),
                parent_id=parent_id,
                description=str(payload.get("description") or ""))
            origin_goal_ids.append(attached_goal_id)
            goals.link_curiosity(attached_goal_id, curiosity_id)
        elif proposal_type == "create_root_branch":
            root_id = goals.create(
                "overgoal", str(payload.get("root_title") or "New Root"),
                parent_id=goals.root_id,
                description=str(payload.get("root_description") or ""))
            origin_goal_ids.append(root_id)
            branch_title = str(payload.get("branch_title") or "").strip()
            if branch_title:
                attached_goal_id = goals.create(
                    "subgoal", branch_title, parent_id=root_id,
                    description=str(payload.get("branch_description") or ""))
                origin_goal_ids.append(attached_goal_id)
            else:
                attached_goal_id = root_id
            goals.link_curiosity(attached_goal_id, curiosity_id)
        elif proposal_type == "create_leaf":
            parent_id = int(payload["parent_id"])
            priority = str(payload.get("priority") or "normal")
            if priority not in {"low", "normal", "high"}:
                priority = "normal"
            attached_goal_id = goals.create(
                "task", str(payload.get("title") or "New Leaf"),
                parent_id=parent_id,
                description=str(payload.get("description") or ""),
                priority=priority)
            origin_goal_ids.append(attached_goal_id)
            goals.link_curiosity(parent_id, curiosity_id)
        elif proposal_type == "keep_soul":
            attached_goal_id = None
        elif proposal_type == "keep_investigating":
            question = str(payload.get("question") or "").strip()
            if question:
                created_question_id = store.add_item(
                    int(curiosity_id), "question", question,
                    confidence=0.86, response_type="text")
            attached_goal_id = None
        else:
            raise ValueError("unsupported classification proposal")
        for goal_id in dict.fromkeys(origin_goal_ids):
            goals.set_origin(goal_id, **origin)
        store.resolve_classification_proposal(int(proposal_id), "approved")
        return {"ok": True, "status": "approved",
                "attached_goal_id": attached_goal_id,
                "created_question_id": created_question_id,
                "tree": goals.tree()}
    finally:
        goals.close()
        store.close()


def refine_classification_proposal(config, proposal_id: int, note: str,
                                   model=None) -> dict:
    from .inference import InferenceStore
    from .memory import MemoryStore
    store = CuriosityStore(config.memory_db_path)
    mem = MemoryStore(config.memory_db_path)
    inf = InferenceStore(config.memory_db_path)
    try:
        proposal = store.get_classification_proposal(int(proposal_id))
        curiosity_id = int(proposal["curiosity_id"])
        context_id = store.add_classification_context(
            curiosity_id, str(note or ""), int(proposal_id))
        if proposal["status"] == "open":
            store.resolve_classification_proposal(int(proposal_id), "dismissed")
        result = classify_curiosity(
            config, mem, inf, store, curiosity_id,
            model or get_curiosity_model(config, usage_category="manual"))
        return {"ok": True, "context_id": context_id,
                "dismissed_proposal_id": int(proposal_id), **result}
    except Exception as error:
        return {"ok": False, "message": f"{type(error).__name__}: {error}"}
    finally:
        inf.close()
        mem.close()
        store.close()


def generate_items(mem, inf, store: CuriosityStore, curiosity_id: int, model, *,
                   limit: int | None = None,
                   question_min_confidence: float = 0.70,
                   suggestion_min_confidence: float = 0.80,
                   max_open: int = 6) -> int:
    """One round for one curiosity: ask the model for new items, filter by
    confidence, queue what clears the bar (highest-confidence first, up to
    `limit`). Returns how many were queued."""
    row = store.get_curiosity(curiosity_id)
    if row is None or row["status"] != "active":
        return 0
    open_items = store.open_items(curiosity_id)
    if len(open_items) >= max_open:
        return 0  # queue's backed up — wait for the user to catch up

    context = _build_context(mem, inf, store, curiosity_id)
    _commit_open_connections(mem, inf, store)
    raw_items = model.generate(row["directive"], context)

    existing = store.items_for_curiosity(curiosity_id)
    existing_text = {" ".join(it["text"].lower().split()) for it in existing}
    gated = []
    for item in raw_items:
        floor = suggestion_min_confidence if item.kind == "suggestion" else question_min_confidence
        normalized = " ".join((item.text or "").lower().split())
        if item.confidence >= floor and normalized and normalized not in existing_text:
            gated.append(item)
    gated.sort(key=lambda i: i.confidence, reverse=True)

    budget = min(limit if limit is not None else len(gated),
                 max_open - len(open_items))
    selected: list[GeneratedItem] = []
    answered_count = sum(
        it["kind"] == "question" and it["status"] == "answered" for it in existing)
    has_open_suggestion = any(
        it["kind"] == "suggestion" and it["status"] == "open" for it in existing)
    if budget > 0 and answered_count >= 2 and not has_open_suggestion:
        suggestion = next((it for it in gated if it.kind == "suggestion"), None)
        if suggestion is not None:
            selected.append(suggestion)
    selected.extend(it for it in gated if it not in selected)

    created = 0
    for item in selected[:budget]:
        allowed_slugs = {line.split(":", 1)[0].strip(" -") for line in
                         context.metric_block.splitlines()} if context.metric_block != "  (none)" else set()
        slug = item.metric_dimension_slug if item.metric_dimension_slug in allowed_slugs else None
        event_type = item.metric_event_type if slug else None
        response_type = item.response_type if event_type == "assessment" else "text"
        store.add_item(
            curiosity_id, item.kind, item.text, confidence=item.confidence,
            metric_event_type=event_type, metric_dimension_slug=slug,
            response_type=response_type)
        created += 1
    store.touch(curiosity_id)
    return created


def _initial_clarifier_items(directive: str) -> list[GeneratedItem]:
    """Small fallback starter set for journal-created investigations.

    This prevents the first screen from becoming a dead "generate more" card
    when the model produces no items above the gate. The questions are generic
    enough for any user, but concrete enough to start narrowing context.
    """
    topic = (directive or "this").strip().rstrip(".?!")[:90] or "this"
    return [
        GeneratedItem(
            "question",
            f'Is "{topic}" mostly about something that happens before the event/task begins?',
            0.92,
            response_type="yes_no"),
        GeneratedItem(
            "question",
            f'Is "{topic}" mostly about what happens during the event/task itself?',
            0.9,
            response_type="yes_no"),
        GeneratedItem(
            "question",
            f'Is "{topic}" mostly about the aftermath — recovery, regret, exhaustion, or meaning-making afterward?',
            0.88,
            response_type="yes_no"),
        GeneratedItem(
            "question",
            "Which answer above feels closest, and what is the smallest concrete example?",
            0.86,
            response_type="text"),
    ]


def seed_initial_clarifiers(store: CuriosityStore, curiosity_id: int,
                            directive: str, *, limit: int | None = None) -> int:
    existing_text = {
        " ".join(it["text"].lower().split())
        for it in store.items_for_curiosity(int(curiosity_id))
    }
    budget = max(1, min(limit if limit is not None else 4, 4))
    created = 0
    for item in _initial_clarifier_items(directive)[:budget]:
        normalized = " ".join(item.text.lower().split())
        if normalized in existing_text:
            continue
        store.add_item(
            int(curiosity_id), item.kind, item.text,
            confidence=item.confidence, response_type=item.response_type)
        created += 1
    if created:
        store.touch(int(curiosity_id))
    return created


def notion_summary_markdown(mem, inf, store: CuriosityStore, curiosity_id: int, model) -> str:
    """The consolidated-essentials markdown for one curiosity — same context
    shape generation uses, handed to the model's summarize() instead."""
    curiosity = store.get_curiosity(curiosity_id)
    if curiosity is None:
        raise ValueError(f"curiosity {curiosity_id} not found")
    context = _build_context(mem, inf, store, curiosity_id)
    _commit_open_connections(mem, inf, store)
    return model.summarize(curiosity, context)


def set_curiosity(mem, inf, store: CuriosityStore, directive: str, model, *,
                  label: str | None = None, make_greatest: bool = False,
                  limit: int | None = None) -> dict:
    """Create a new curiosity from a directive and immediately generate its
    first round of items."""
    directive = (directive or "").strip()
    if not directive:
        raise ValueError("directive is empty")
    label = (label or "").strip() or _default_label(directive)
    existing = _find_similar_active_curiosity(store, label=label, directive=directive)
    if existing:
        curiosity_id = int(existing["id"])
        reused = True
    else:
        curiosity_id = store.add_curiosity(directive, label)
        reused = False
    if make_greatest:
        store.set_greatest(curiosity_id)
    created = generate_items(mem, inf, store, curiosity_id, model, limit=limit)
    return {"curiosity_id": curiosity_id, "created": created, "reused": reused}


def set_curiosity_from_journal(mem, inf, store: CuriosityStore, journal_text: str,
                               model, *, label: str | None = None,
                               make_greatest: bool = False,
                               limit: int | None = None) -> dict:
    """Create an Investigation from a freeform current-state dump.

    The dump is stored as the first answered seed item before generation, so the
    first AI questions are grounded in the user's current framing instead of old
    memory assumptions.
    """
    journal_text = (journal_text or "").strip()
    if not journal_text:
        raise ValueError("journal text is empty")
    directive = _journal_directive(journal_text)
    label = (label or "").strip() or _journal_label(journal_text)
    existing = _find_similar_active_curiosity(store, label=label, directive=directive)
    if existing:
        curiosity_id = int(existing["id"])
        reused = True
    else:
        curiosity_id = store.add_curiosity(directive, label)
        reused = False
    seed_id = store.add_item(
        curiosity_id, "question", "Initial journal dump / current framing",
        confidence=1.0)
    new_id = mem.add(
        label, "initial journal dump", journal_text, raw_source=journal_text,
        source_refs=[{
            "kind": "curiosity-journal-seed",
            "curiosity_item": seed_id,
        }],
    )
    store.mark_answered(seed_id, journal_text, new_id)
    if make_greatest:
        store.set_greatest(curiosity_id)
    created = generate_items(mem, inf, store, curiosity_id, model, limit=limit)
    if created == 0:
        created = seed_initial_clarifiers(
            store, curiosity_id, directive, limit=limit or 4)
    return {"curiosity_id": curiosity_id, "seed_item_id": seed_id,
            "resulting_memory_id": new_id, "created": created, "reused": reused}


def answer_item(mem, store: CuriosityStore, item_id: int, text: str, model, *,
                rating: float | None = None) -> dict:
    row = store.get_item(item_id)
    if row is None:
        raise ValueError(f"curiosity item {item_id} not found")
    item = store._item_dict(row)
    if item["kind"] != "question":
        raise ValueError(f"item {item_id} is a suggestion — use respond_suggestion")
    if item["status"] != "open":
        raise ValueError(f"curiosity item {item_id} is already {item['status']}")
    text = (text or "").strip()
    if not text:
        raise ValueError("answer text is empty")
    if item["response_type"] == "rating":
        if rating is None or not 0 <= float(rating) <= 10:
            raise ValueError("a 0-10 rating is required for this assessment")

    curiosity = store.get_curiosity(item["curiosity_id"])
    _commit_open_connections(mem, store)
    resolution = model.resolve(curiosity["directive"], item["text"], text)
    attribute = (resolution.get("attribute") or "note").strip()
    # The user's exact words are authoritative. The model may categorize them,
    # but it may not silently rewrite what enters long-term memory.
    new_id = mem.add(
        curiosity["label"], attribute, text, raw_source=text,
        source_refs=[{
            "kind": "curiosity-answer", "curiosity_item": item_id,
            "question": item["text"],
        }],
    )
    store.mark_answered(item_id, text, new_id)
    log_diag("curiosity", f"answered id={item_id} resulting_memory={new_id}")
    return {"resulting_memory_id": new_id, "curiosity_id": item["curiosity_id"],
            "metric_event_type": item["metric_event_type"],
            "metric_dimension_slug": item["metric_dimension_slug"],
            "rating": None if rating is None else float(rating)}


def dismiss_item(store: CuriosityStore, item_id: int) -> None:
    row = store.get_item(item_id)
    if row is None:
        raise ValueError(f"curiosity item {item_id} not found")
    if store._item_dict(row)["kind"] != "question":
        raise ValueError(f"item {item_id} is a suggestion — use respond_suggestion")
    store.mark_dismissed(item_id)


def respond_suggestion(store: CuriosityStore, item_id: int, action: str) -> None:
    row = store.get_item(item_id)
    if row is None:
        raise ValueError(f"curiosity item {item_id} not found")
    item = store._item_dict(row)
    if item["kind"] != "suggestion":
        raise ValueError(f"item {item_id} is a question — use answer_item/dismiss_item")
    if action not in _SUGGESTION_ACTIONS:
        raise ValueError(f"unknown suggestion action: {action}")
    store.mark_suggestion_resolved(item_id, action)


def set_greatest(store: CuriosityStore, curiosity_id: int, on: bool = True) -> None:
    store.set_greatest(curiosity_id, on)


def pause_curiosity(store: CuriosityStore, curiosity_id: int) -> None:
    store.set_status(curiosity_id, "paused")


def archive_curiosity(store: CuriosityStore, curiosity_id: int) -> None:
    store.set_status(curiosity_id, "archived")


def reactivate_curiosity(store: CuriosityStore, curiosity_id: int) -> None:
    store.set_status(curiosity_id, "active")


def run_all_active(mem, inf, store: CuriosityStore, model, *,
                   greatest_limit: int = 5, background_limit: int = 2,
                   question_min_confidence: float = 0.70,
                   suggestion_min_confidence: float = 0.80,
                   max_open: int = 6) -> int:
    """The periodic/scheduler pass: one round for every active curiosity,
    the greatest one getting a bigger budget than the rest."""
    total = 0
    for row in store.list_curiosities(status="active"):
        limit = greatest_limit if row["is_greatest"] else background_limit
        total += generate_items(
            mem, inf, store, row["id"], model, limit=limit,
            question_min_confidence=question_min_confidence,
            suggestion_min_confidence=suggestion_min_confidence,
            max_open=max_open)
    return total
