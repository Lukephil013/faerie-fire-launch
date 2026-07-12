"""The inference store — Faerie Fire's self-model of *hypotheses* about you.

Distinct from `MemoryStore` facts. Inferences are bold, psychological guesses the
system forms from your behavior. The current review lifecycle is:

    candidate -> persistent Address conversation -> accepted/tentative/rejected
    directed question -> persistent investigation -> same explicit outcomes

Legacy yes/no/kind-of/refine methods remain for older bridges and migrations,
but the UI no longer treats a model confidence percentage as user approval.

Stored in the same `memory.db` as facts/associations but in its own `inference`
table, so it coexists with `MemoryStore` without touching fact rows.

Pure sqlite/json/datetime — no external dependencies, fully testable offline.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from . import crypto
from .db import connect as db_connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    return db_connect(db_path)


# --- tuning knobs -----------------------------------------------------------
CORE_BELIEF_CONFIRMATIONS = 3     # legacy pre-Address rows only
THEME_REJECTION_CAP = 4           # rejections in a theme -> park it (stop nagging)
CONFIRM_BUMP = 0.4                # fraction of remaining distance to 1.0 per yes
PARTIAL_CONFIDENCE = 0.5          # "kind of" resting confidence
REFINE_CONFIDENCE = 0.95         # your own wording is near-truth
SURFACE_CONFIDENCE = 0.80        # a claim must reach this to be shown for yes/no
                                  # (below it, a theme only shows as "forming")

_STATUSES = {"candidate", "confirmed", "partial", "rejected", "retired"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS inference (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme           TEXT NOT NULL,
    statement       TEXT NOT NULL,
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'candidate',
    evidence        TEXT,            -- JSON: behaviour that triggered it
    refines_id      INTEGER,         -- the inference this one replaces
    source_refs     TEXT,            -- JSON list of memory ids / event refs
    times_confirmed INTEGER DEFAULT 0,
    times_skipped   INTEGER DEFAULT 0,
    created_at      TEXT,
    validated_at    TEXT,
    evidence_cutoff_id INTEGER,
    last_shown_at   TEXT,
    CHECK (status IN ('candidate','confirmed','partial','rejected','retired')),
    FOREIGN KEY (refines_id) REFERENCES inference(id)
);
CREATE INDEX IF NOT EXISTS idx_inf_status ON inference(status);
CREATE INDEX IF NOT EXISTS idx_inf_theme ON inference(theme);

-- Raw behavioural evidence, tagged by theme. This is what the engine "sits on":
-- it accumulates silently and is NEVER shown as a yes/no question. Synthesis
-- turns enough of it into a single claim in the `inference` table.
CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    theme        TEXT NOT NULL,
    observation  TEXT NOT NULL,
    weight       REAL DEFAULT 1.0,
    source_refs  TEXT,
    run_id       TEXT,
    item_index   INTEGER,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_theme ON evidence(theme);

-- User feedback on No / Kind-of answers: the model asks follow-up questions,
-- the user explains (free text, links welcome), and the distilled LESSON is
-- stored here. Lessons are authoritative user corrections fed back into every
-- future synthesis for that theme.
CREATE TABLE IF NOT EXISTS feedback_note (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inference_id INTEGER,
    theme        TEXT NOT NULL,
    action       TEXT NOT NULL,      -- 'no' | 'kind_of'
    questions    TEXT,               -- JSON list the model asked
    user_text    TEXT,               -- what the user wrote back
    lesson       TEXT NOT NULL,      -- distilled correction for future runs
    refs         TEXT,               -- JSON list of links the user provided
    created_at   TEXT,
    FOREIGN KEY (inference_id) REFERENCES inference(id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_theme ON feedback_note(theme);

-- Persistent, user-directed investigations. Text is encrypted at rest. These
-- are deliberately separate from companion chat: each session resolves one
-- proposed inference or one question the user explicitly asked Faerie to study.
CREATE TABLE IF NOT EXISTS inference_inquiry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    inference_id   INTEGER,
    prompt         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    draft_claim    TEXT,
    model_confidence REAL,
    outcome        TEXT,
    canonical_id   INTEGER,
    created_at     TEXT,
    updated_at     TEXT,
    resolved_at    TEXT,
    CHECK (kind IN ('address','directed')),
    CHECK (status IN ('open','accepted','tentative','rejected','awaiting_evidence')),
    FOREIGN KEY (inference_id) REFERENCES inference(id),
    FOREIGN KEY (canonical_id) REFERENCES inference(id)
);
CREATE INDEX IF NOT EXISTS idx_inquiry_status ON inference_inquiry(status);
CREATE TABLE IF NOT EXISTS inference_inquiry_message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id  INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT,
    CHECK (role IN ('user','assistant')),
    FOREIGN KEY (inquiry_id) REFERENCES inference_inquiry(id)
);
CREATE INDEX IF NOT EXISTS idx_inquiry_message ON inference_inquiry_message(inquiry_id,id);

-- Investigation-derived changes remain inert proposals until separately
-- approved. The inference table remains the sole person-model truth store.
CREATE TABLE IF NOT EXISTS person_model_proposal (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    curiosity_id        INTEGER NOT NULL,
    synthesis_id        INTEGER NOT NULL,
    operation           TEXT NOT NULL,
    target_inference_id INTEGER,
    payload_json        TEXT NOT NULL,
    fingerprint         TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decision_note       TEXT,
    applied_inference_id INTEGER,
    CHECK (operation IN ('new','support','contradict','narrow','retire',
                         'situational','change_over_time')),
    CHECK (status IN ('open','approved','rejected')),
    FOREIGN KEY (target_inference_id) REFERENCES inference(id),
    FOREIGN KEY (applied_inference_id) REFERENCES inference(id)
);
CREATE INDEX IF NOT EXISTS idx_person_model_proposal_curiosity
ON person_model_proposal(curiosity_id,status,id DESC);
CREATE TABLE IF NOT EXISTS person_model_reconciliation_run (
    synthesis_id INTEGER PRIMARY KEY,
    curiosity_id INTEGER NOT NULL,
    result_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL
);
"""


_CONCEPT_WORD = re.compile(r"[a-z0-9]+")
_CONCEPT_STOP = {
    "about", "after", "again", "also", "and", "are", "because", "been",
    "being", "but", "can", "could", "does", "for", "from", "have", "how",
    "into", "just", "like", "more", "not", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "through", "was", "were",
    "what", "when", "where", "which", "while", "who", "why", "with",
    "would", "you", "your",
}


def _concept_tokens(text: str) -> set[str]:
    tokens = set()
    for raw in _CONCEPT_WORD.findall((text or "").lower()):
        if raw in _CONCEPT_STOP or len(raw) < 3:
            continue
        # A tiny deterministic stemmer catches common rephrasings without a
        # dependency or sending private text to another service.
        token = raw
        for suffix in ("ingly", "edly", "ation", "ments", "ment", "ness", "ing", "ed", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                token = token[:-len(suffix)]
                break
        tokens.add(token)
    return tokens


def concept_similarity(left: str, right: str) -> float:
    """Conservative lexical concept similarity used as a final dedupe guard."""
    a, b = _concept_tokens(left), _concept_tokens(right)
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    return max(overlap / len(a | b), overlap / min(len(a), len(b)) * 0.85)


def _bump(conf) -> float:
    base = 0.6 if conf is None else float(conf)
    return round(min(1.0, base + (1.0 - base) * CONFIRM_BUMP), 4)


class InferenceStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = _connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created by earlier versions."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(inference)")}
        if "reflected_at" not in cols:
            # when the companion last volunteered this belief back to you
            self.conn.execute("ALTER TABLE inference ADD COLUMN reflected_at TEXT")
        if "resolution_status" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN resolution_status TEXT")
        if "absorbed_by_id" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN absorbed_by_id INTEGER")
        if "evidence_cutoff_id" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN evidence_cutoff_id INTEGER")
        if "scope" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN scope TEXT DEFAULT 'general'")
        if "sensitivity" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN sensitivity TEXT DEFAULT 'normal'")
        if "counterevidence" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN counterevidence TEXT DEFAULT '[]'")
        if "source_kind" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN source_kind TEXT")
        if "source_id" not in cols:
            self.conn.execute("ALTER TABLE inference ADD COLUMN source_id INTEGER")
        evidence_cols = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(evidence)")
        }
        if "run_id" not in evidence_cols:
            self.conn.execute("ALTER TABLE evidence ADD COLUMN run_id TEXT")
        if "item_index" not in evidence_cols:
            self.conn.execute("ALTER TABLE evidence ADD COLUMN item_index INTEGER")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_run_item "
            "ON evidence(run_id, item_index) WHERE run_id IS NOT NULL"
        )

    # --- writes -----------------------------------------------------------
    def add_candidate(self, theme: str, statement: str, *, confidence=None,
                      evidence=None, source_refs=None, refines_id=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO inference (theme, statement, confidence, status, evidence, "
            "refines_id, source_refs, created_at) VALUES (?,?,?,'candidate',?,?,?,?)",
            (theme, crypto.enc(statement), confidence, json.dumps(evidence or {}),
             refines_id, json.dumps(source_refs or []), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _evidence_cutoff_id(self, theme: str) -> int:
        return int(self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM evidence WHERE theme=?",
            (theme,),
        ).fetchone()[0] or 0)

    def confirm(self, inference_id: int) -> None:
        row = self.get(inference_id)
        if row is None:
            return
        self.conn.execute(
            "UPDATE inference SET status='confirmed', confidence=?, "
            "times_confirmed=times_confirmed+1, validated_at=?, "
            "evidence_cutoff_id=? WHERE id=?",
            (_bump(row["confidence"]), _now(),
             self._evidence_cutoff_id(row["theme"]), inference_id),
        )
        self.conn.commit()

    def reject(self, inference_id: int) -> None:
        self.conn.execute(
            "UPDATE inference SET status='rejected', validated_at=? WHERE id=?",
            (_now(), inference_id),
        )
        self.conn.commit()

    def kind_of(self, inference_id: int) -> None:
        """Partial truth: keep it as a soft belief and flag it for sharpening."""
        self.conn.execute(
            "UPDATE inference SET status='partial', confidence=?, validated_at=? WHERE id=?",
            (PARTIAL_CONFIDENCE, _now(), inference_id),
        )
        self.conn.commit()

    def skip(self, inference_id: int) -> None:
        self.conn.execute(
            "UPDATE inference SET times_skipped=times_skipped+1, last_shown_at=? WHERE id=?",
            (_now(), inference_id),
        )
        self.conn.commit()

    def refine(self, inference_id: int, new_statement: str) -> int:
        """You rewrote it: retire the guess, store your wording as confirmed truth."""
        row = self.get(inference_id)
        theme = row["theme"] if row else "general"
        self.conn.execute(
            "UPDATE inference SET status='retired', validated_at=? WHERE id=?",
            (_now(), inference_id),
        )
        cur = self.conn.execute(
            "INSERT INTO inference (theme, statement, confidence, status, evidence, "
            "refines_id, source_refs, times_confirmed, created_at, validated_at, "
            "evidence_cutoff_id) VALUES (?,?,?,'confirmed',?,?,?,1,?,?,?)",
            (theme, crypto.enc(new_statement), REFINE_CONFIDENCE,
             json.dumps({"method": "user-refined"}), inference_id,
             row["source_refs"] if row else "[]", _now(), _now(),
             self._evidence_cutoff_id(theme)),
        )
        self.conn.execute(
            "UPDATE inference SET resolution_status='accepted' WHERE id=?",
            (int(cur.lastrowid),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def retire(self, inference_id: int) -> None:
        self.conn.execute(
            "UPDATE inference SET status='retired', validated_at=? WHERE id=?",
            (_now(), inference_id),
        )
        self.conn.commit()

    def retire_theme(self, theme: str) -> int:
        cur = self.conn.execute(
            "UPDATE inference SET status='retired', validated_at=? "
            "WHERE theme=? AND status IN ('candidate','partial','confirmed')",
            (_now(), theme),
        )
        self.conn.commit()
        return cur.rowcount

    # --- Investigation -> person-model proposals -----------------------
    @staticmethod
    def _normalize_person_payload(payload: dict | None) -> dict:
        raw = payload if isinstance(payload, dict) else {}

        def text(name: str, limit: int = 2000) -> str:
            return str(raw.get(name) or "").strip()[:limit]

        def strings(name: str, limit: int = 10) -> list[str]:
            values = raw.get(name) if isinstance(raw.get(name), list) else []
            return [str(value).strip()[:500] for value in values
                    if str(value).strip()][:limit]

        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        scope = text("scope", 40).lower() or "situational"
        if scope not in {"situational", "domain", "identity"}:
            scope = "situational"
        sensitivity = text("sensitivity", 40).lower() or "normal"
        if sensitivity not in {"normal", "sensitive"}:
            sensitivity = "normal"
        return {
            "theme": text("theme", 160) or "investigation",
            "statement": text("statement"), "scope": scope,
            "sensitivity": sensitivity, "confidence": confidence,
            "rationale": text("rationale"),
            "evidence": strings("evidence"),
            "counterevidence": strings("counterevidence"),
            "change_over_time": text("change_over_time", 800),
        }

    def _person_proposal_dict(self, row) -> dict | None:
        if row is None:
            return None
        try:
            payload = json.loads(crypto.dec(row["payload_json"]) or "{}")
        except (TypeError, ValueError):
            payload = {}
        target = self.get(row["target_inference_id"]) if row["target_inference_id"] else None
        return {
            "id": int(row["id"]), "curiosity_id": int(row["curiosity_id"]),
            "synthesis_id": int(row["synthesis_id"]), "operation": row["operation"],
            "target_inference_id": row["target_inference_id"],
            "target": self._dict(target) if target is not None else None,
            "payload": self._normalize_person_payload(payload), "status": row["status"],
            "created_at": row["created_at"], "decided_at": row["decided_at"],
            "decision_note": crypto.dec(row["decision_note"]) or "",
            "applied_inference_id": row["applied_inference_id"],
        }

    def add_person_proposal(self, curiosity_id: int, synthesis_id: int,
                            operation: str, payload: dict, *,
                            target_inference_id: int | None = None) -> dict:
        operation = str(operation or "").strip().lower()
        allowed = {"new", "support", "contradict", "narrow", "retire",
                   "situational", "change_over_time"}
        if operation not in allowed:
            raise ValueError("unknown person-model operation")
        normalized = self._normalize_person_payload(payload)
        target = self.get(int(target_inference_id)) if target_inference_id else None
        if operation != "new" and target is None:
            raise ValueError("this person-model update requires an existing belief")
        if operation not in {"support", "retire"} and not normalized["statement"]:
            raise ValueError("this person-model update requires a proposed statement")
        if normalized["scope"] == "identity" and (
                normalized["confidence"] < .9 or len(normalized["evidence"]) < 3):
            raise ValueError("identity-level updates require 90% confidence and three evidence items")
        material = json.dumps({"curiosity_id": int(curiosity_id),
                               "synthesis_id": int(synthesis_id),
                               "operation": operation,
                               "target": int(target_inference_id or 0),
                               "payload": normalized}, ensure_ascii=False, sort_keys=True)
        fingerprint = __import__("hashlib").sha256(material.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT OR IGNORE INTO person_model_proposal "
            "(curiosity_id,synthesis_id,operation,target_inference_id,payload_json,"
            "fingerprint,status,created_at) VALUES (?,?,?,?,?,?,'open',?)",
            (int(curiosity_id), int(synthesis_id), operation,
             int(target_inference_id) if target_inference_id else None,
             crypto.enc(json.dumps(normalized, ensure_ascii=False, sort_keys=True)),
             fingerprint, _now()))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM person_model_proposal WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return self._person_proposal_dict(row)

    def person_proposals(self, curiosity_id: int | None = None, *,
                         synthesis_id: int | None = None,
                         status: str | None = None) -> list[dict]:
        clauses, params = [], []
        if curiosity_id is not None:
            clauses.append("curiosity_id=?"); params.append(int(curiosity_id))
        if synthesis_id is not None:
            clauses.append("synthesis_id=?"); params.append(int(synthesis_id))
        if status is not None:
            clauses.append("status=?"); params.append(str(status))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            "SELECT * FROM person_model_proposal" + where + " ORDER BY id DESC",
            tuple(params)).fetchall()
        return [self._person_proposal_dict(row) for row in rows]

    def person_reconciliation_run(self, synthesis_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM person_model_reconciliation_run WHERE synthesis_id=?",
            (int(synthesis_id),)).fetchone()
        return (dict(row) if row is not None else None)

    def mark_person_reconciled(self, synthesis_id: int, curiosity_id: int,
                               result_count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO person_model_reconciliation_run "
            "(synthesis_id,curiosity_id,result_count,completed_at) VALUES (?,?,?,?)",
            (int(synthesis_id), int(curiosity_id), int(result_count), _now()))
        self.conn.commit()

    def decide_person_proposal(self, proposal_id: int, action: str, *,
                               payload: dict | None = None,
                               note: str = "") -> dict:
        row = self.conn.execute(
            "SELECT * FROM person_model_proposal WHERE id=?", (int(proposal_id),)
        ).fetchone()
        proposal = self._person_proposal_dict(row)
        if not proposal or proposal["status"] != "open":
            raise ValueError("person-model proposal is no longer open")
        if action not in {"approve", "reject"}:
            raise ValueError("unknown person-model decision")
        normalized = self._normalize_person_payload(
            payload if payload is not None else proposal["payload"])
        if proposal["operation"] not in {"support", "retire"} and not normalized["statement"]:
            raise ValueError("this person-model update requires a proposed statement")
        if normalized["scope"] == "identity" and (
                normalized["confidence"] < .9 or len(normalized["evidence"]) < 3):
            raise ValueError("identity-level updates require 90% confidence and three evidence items")
        applied_id = None
        now = _now()
        if action == "approve":
            target_id = proposal["target_inference_id"]
            operation = proposal["operation"]
            if operation == "support":
                target = self.get(target_id)
                confidence = _bump(target["confidence"] if target else normalized["confidence"])
                self.conn.execute(
                    "UPDATE inference SET confidence=?,resolution_status='accepted',"
                    "validated_at=? WHERE id=?", (confidence, now, target_id))
                applied_id = target_id
            elif operation == "retire":
                self.conn.execute(
                    "UPDATE inference SET status='retired',validated_at=? WHERE id=?",
                    (now, target_id))
            else:
                if target_id:
                    self.conn.execute(
                        "UPDATE inference SET status='retired',validated_at=? WHERE id=?",
                        (now, target_id))
                cur = self.conn.execute(
                    "INSERT INTO inference (theme,statement,confidence,status,evidence,"
                    "refines_id,source_refs,times_confirmed,created_at,validated_at,"
                    "resolution_status,evidence_cutoff_id,scope,sensitivity,counterevidence,"
                    "source_kind,source_id) VALUES (?,?,?,'confirmed',?,?,?,1,?,?,"
                    "'accepted',?,?,?,?,?,?)",
                    (normalized["theme"], crypto.enc(normalized["statement"]),
                     normalized["confidence"],
                     json.dumps({"method": "investigation-reconciliation",
                                 "synthesis_id": proposal["synthesis_id"],
                                 "evidence_count": len(normalized["evidence"])}),
                     target_id, json.dumps([proposal["synthesis_id"]]), now, now,
                     self._evidence_cutoff_id(normalized["theme"]), normalized["scope"],
                     normalized["sensitivity"],
                     crypto.enc(json.dumps(
                         normalized["counterevidence"], ensure_ascii=False)),
                     "curiosity_synthesis", proposal["synthesis_id"]))
                applied_id = int(cur.lastrowid)
        status = "approved" if action == "approve" else "rejected"
        self.conn.execute(
            "UPDATE person_model_proposal SET status=?,payload_json=?,decided_at=?,"
            "decision_note=?,applied_inference_id=? WHERE id=?",
            (status, crypto.enc(json.dumps(normalized, ensure_ascii=False, sort_keys=True)),
             now, crypto.enc(str(note or "")), applied_id, int(proposal_id)))
        self.conn.commit()
        updated = self.conn.execute(
            "SELECT * FROM person_model_proposal WHERE id=?", (int(proposal_id),)
        ).fetchone()
        return self._person_proposal_dict(updated)

    # --- reads ------------------------------------------------------------
    def get(self, inference_id: int):
        return self.conn.execute(
            "SELECT * FROM inference WHERE id=?", (inference_id,)).fetchone()

    def _dict(self, r) -> dict:
        return {
            "id": r["id"], "theme": r["theme"], "statement": crypto.dec(r["statement"]),
            "confidence": r["confidence"], "status": r["status"],
            "evidence": json.loads(r["evidence"] or "{}"),
            "refines_id": r["refines_id"],
            "source_refs": json.loads(r["source_refs"] or "[]"),
            "times_confirmed": r["times_confirmed"],
            "times_skipped": r["times_skipped"],
            "resolution_status": r["resolution_status"],
            "absorbed_by_id": r["absorbed_by_id"],
            "scope": r["scope"] or "general",
            "sensitivity": r["sensitivity"] or "normal",
            "counterevidence": self._decoded_json_list(r["counterevidence"]),
            "source_kind": r["source_kind"], "source_id": r["source_id"],
            "is_core_belief": (r["status"] == "confirmed" and
                (r["scope"] or "general") in {"general", "identity"} and (
                r["resolution_status"] == "accepted"
                or r["times_confirmed"] >= CORE_BELIEF_CONFIRMATIONS)),
        }

    @staticmethod
    def _decoded_json_list(value) -> list:
        try:
            decoded = json.loads(crypto.dec(value) or "[]")
            return decoded if isinstance(decoded, list) else []
        except (TypeError, ValueError):
            return []

    def to_review(self, limit: int | None = None, *,
                  min_confidence: float = SURFACE_CONFIDENCE) -> list[dict]:
        """The yes/no stack: ONLY claims that have reached the confidence gate.
        Sub-gate claims are still 'forming' and never appear here. Least-skipped
        and most-confident first; unlimited by default."""
        cutoff = datetime.now(timezone.utc).replace(microsecond=0)
        cutoff = cutoff.timestamp() - 24 * 60 * 60
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM inference WHERE status='candidate' "
            "AND confidence IS NOT NULL AND confidence >= ? "
            "AND (last_shown_at IS NULL OR last_shown_at < ?) "
            "ORDER BY times_skipped ASC, confidence DESC, id ASC",
            (min_confidence, cutoff_iso),
        ).fetchall()
        out = [self._dict(r) for r in rows]
        return out[:limit] if limit else out

    def forming(self, *, max_confidence: float = SURFACE_CONFIDENCE) -> list[dict]:
        """Themes still building toward the gate — shown as passive progress only
        (theme + confidence + how much evidence backs it), never as questions."""
        rows = self.conn.execute(
            "SELECT * FROM inference WHERE status='candidate' "
            "AND (confidence IS NULL OR confidence < ?) "
            "ORDER BY confidence DESC, id DESC",
            (max_confidence,),
        ).fetchall()
        counts = self.evidence_count_by_theme()
        out = []
        for r in rows:
            out.append({"id": r["id"], "theme": r["theme"],
                        "confidence": r["confidence"] or 0.0,
                        "evidence_count": counts.get(r["theme"], 0)})
        return out

    def confirmed(self, *, core_only: bool = False) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM inference WHERE status='confirmed' "
            "ORDER BY times_confirmed DESC, confidence DESC, id DESC"
        ).fetchall()
        items = [self._dict(r) for r in rows]
        return [i for i in items if i["is_core_belief"]] if core_only else items

    def partials(self) -> list[dict]:
        """'Kind of' inferences awaiting a sharper re-hypothesis next loop."""
        rows = self.conn.execute(
            "SELECT * FROM inference WHERE status='partial' ORDER BY id"
        ).fetchall()
        return [self._dict(r) for r in rows]

    def rejected_for_theme(self, theme: str, limit: int = 10) -> list[str]:
        """Negative constraints for the loop: what you already said no to."""
        rows = self.conn.execute(
            "SELECT statement FROM inference WHERE theme=? AND status='rejected' "
            "ORDER BY id DESC LIMIT ?", (theme, limit),
        ).fetchall()
        return [crypto.dec(r["statement"]) for r in rows]

    def theme_rejection_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT theme, COUNT(*) c FROM inference WHERE status='rejected' "
            "GROUP BY theme"
        ).fetchall()
        return {r["theme"]: r["c"] for r in rows}

    def parked_themes(self, cap: int = THEME_REJECTION_CAP) -> list[str]:
        """Themes rejected so many times we should stop guessing about them."""
        return [t for t, c in self.theme_rejection_counts().items() if c >= cap]

    def last_confirmed_at(self, theme: str) -> str | None:
        """When this theme was last confirmed (a "Yes"), if ever. A theme can
        have been confirmed more than once over time (each re-confirmation
        updates this), so this is the MAX across all confirmed rows for it."""
        row = self.conn.execute(
            "SELECT MAX(validated_at) v FROM inference WHERE theme=? AND status='confirmed'",
            (theme,),
        ).fetchone()
        return row["v"] if row and row["v"] else None

    def evidence_count_since(self, theme: str, since: str) -> int:
        """How much NEW evidence a theme has accumulated after confirmation.

        New rows are counted by evidence id when available, not timestamp. On
        Windows several rows can share the same microsecond timestamp as the
        confirmation; id watermarks avoid old evidence being treated as fresh.
        The timestamp fallback is for legacy confirmed rows created before the
        cutoff column existed.
        """
        row = self.conn.execute(
            "SELECT evidence_cutoff_id FROM inference "
            "WHERE theme=? AND status='confirmed' AND validated_at=? "
            "ORDER BY id DESC LIMIT 1",
            (theme, since),
        ).fetchone()
        if row and row["evidence_cutoff_id"] is not None:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE theme=? AND id > ?",
                (theme, int(row["evidence_cutoff_id"])),
            ).fetchone()[0])
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE theme=? AND created_at > ?",
            (theme, since),
        ).fetchone()[0])

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM inference GROUP BY status").fetchall()
        by = {r["status"]: r["c"] for r in rows}
        by["core_beliefs"] = len(self.confirmed(core_only=True))
        by["evidence"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM evidence").fetchone()[0])
        return by

    # --- evidence (the hidden accumulation layer) -------------------------
    def add_evidence(self, theme: str, observation: str, *, weight: float = 1.0,
                     source_refs=None, run_id: str | None = None,
                     item_index: int | None = None) -> int | None:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO evidence "
            "(theme, observation, weight, source_refs, run_id, item_index, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (theme, crypto.enc(observation), weight, json.dumps(source_refs or []),
             run_id, item_index, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid) if cur.rowcount else None

    def evidence_episode_count(self, theme: str) -> int:
        """Independent observation windows, not repeated rows from one run."""
        return int(self.conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(run_id, 'legacy-' || id)) "
            "FROM evidence WHERE theme=?", (theme,),
        ).fetchone()[0])

    def evidence_for_theme(self, theme: str, limit: int = 50) -> list[str]:
        rows = self.conn.execute(
            "SELECT observation FROM evidence WHERE theme=? ORDER BY id DESC LIMIT ?",
            (theme, limit),
        ).fetchall()
        return [crypto.dec(r["observation"]) for r in rows]

    def evidence_count_by_theme(self) -> dict:
        rows = self.conn.execute(
            "SELECT theme, COUNT(*) c FROM evidence GROUP BY theme").fetchall()
        return {r["theme"]: r["c"] for r in rows}

    def themes_with_evidence(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT theme, COUNT(*) c FROM evidence GROUP BY theme "
            "ORDER BY c DESC").fetchall()
        return [r["theme"] for r in rows]

    # --- feedback lessons (authoritative user corrections) -----------------
    def add_feedback_note(self, inference_id, theme: str, action: str,
                          *, questions=None, user_text: str = "",
                          lesson: str = "", refs=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO feedback_note (inference_id, theme, action, questions, "
            "user_text, lesson, refs, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (inference_id, theme, action, crypto.enc(json.dumps(questions or [])),
             crypto.enc(user_text), crypto.enc(lesson),
             crypto.enc(json.dumps(refs or [])), _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def lessons_for_theme(self, theme: str, limit: int = 5) -> list[str]:
        rows = self.conn.execute(
            "SELECT lesson FROM feedback_note WHERE theme=? AND lesson != '' "
            "ORDER BY id DESC LIMIT ?", (theme, limit)).fetchall()
        return [crypto.dec(r["lesson"]) for r in rows]

    def themes_with_lessons(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT theme FROM feedback_note WHERE lesson != ''").fetchall()
        return [r["theme"] for r in rows]

    def upsert_claim(self, theme: str, statement: str, confidence: float,
                     *, evidence=None) -> int:
        """Keep ONE evolving claim per theme. Updates the theme's current open
        (still-candidate) claim if there is one; otherwise inserts a new one.
        A confirmed/rejected/retired claim is left alone, so after a rejection a
        fresh claim is started (which synthesis will phrase differently)."""
        duplicate = self.find_canonical_match(statement, theme=theme)
        if duplicate is not None:
            return int(duplicate["id"])
        row = self.conn.execute(
            "SELECT id FROM inference WHERE theme=? AND status='candidate' "
            "ORDER BY id DESC LIMIT 1", (theme,),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE inference SET statement=?, confidence=?, evidence=? WHERE id=?",
                (crypto.enc(statement), confidence, json.dumps(evidence or {}), row["id"]),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO inference (theme, statement, confidence, status, evidence, "
            "created_at) VALUES (?,?,?,'candidate',?,?)",
            (theme, crypto.enc(statement), confidence, json.dumps(evidence or {}), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # --- addressed inferences / directed investigations -----------------
    def start_inquiry(self, kind: str, prompt: str, *, inference_id=None) -> int:
        kind = str(kind).strip()
        prompt = str(prompt or "").strip()
        if kind not in ("address", "directed"):
            raise ValueError("kind must be address or directed")
        if not prompt:
            raise ValueError("investigation prompt cannot be empty")
        if kind == "address":
            row = self.get(int(inference_id)) if inference_id is not None else None
            if row is None:
                raise ValueError("address inquiry requires an existing inference")
            existing = self.conn.execute(
                "SELECT id FROM inference_inquiry WHERE inference_id=? AND status='open' "
                "ORDER BY id DESC LIMIT 1", (int(inference_id),),
            ).fetchone()
            if existing:
                return int(existing["id"])
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO inference_inquiry "
            "(kind,inference_id,prompt,status,created_at,updated_at) "
            "VALUES (?,?,?,'open',?,?)",
            (kind, int(inference_id) if inference_id is not None else None,
             crypto.enc(prompt), now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_inquiry_message(self, inquiry_id: int, role: str, content: str) -> int:
        content = str(content or "").strip()
        if role not in ("user", "assistant") or not content:
            raise ValueError("message requires a valid role and non-empty content")
        cur = self.conn.execute(
            "INSERT INTO inference_inquiry_message (inquiry_id,role,content,created_at) "
            "VALUES (?,?,?,?)", (int(inquiry_id), role, crypto.enc(content), _now()),
        )
        self.conn.execute("UPDATE inference_inquiry SET updated_at=? WHERE id=?",
                          (_now(), int(inquiry_id)))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_inquiry_draft(self, inquiry_id: int, statement: str | None,
                             confidence: float | None) -> None:
        encoded = crypto.enc(statement.strip()) if statement and statement.strip() else None
        self.conn.execute(
            "UPDATE inference_inquiry SET draft_claim=?, model_confidence=?, updated_at=? "
            "WHERE id=? AND status='open'",
            (encoded, confidence, _now(), int(inquiry_id)),
        )
        self.conn.commit()

    def inquiry(self, inquiry_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM inference_inquiry WHERE id=?", (int(inquiry_id),)
        ).fetchone()
        if row is None:
            return None
        messages = self.conn.execute(
            "SELECT id,role,content,created_at FROM inference_inquiry_message "
            "WHERE inquiry_id=? ORDER BY id", (int(inquiry_id),)
        ).fetchall()
        return {
            "id": row["id"], "kind": row["kind"],
            "inference_id": row["inference_id"], "prompt": crypto.dec(row["prompt"]),
            "status": row["status"],
            "draft_claim": crypto.dec(row["draft_claim"]) if row["draft_claim"] else "",
            "model_confidence": row["model_confidence"], "outcome": row["outcome"],
            "canonical_id": row["canonical_id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "resolved_at": row["resolved_at"],
            "messages": [{"id": m["id"], "role": m["role"],
                          "content": crypto.dec(m["content"]),
                          "created_at": m["created_at"]} for m in messages],
        }

    def open_inquiries(self) -> list[dict]:
        ids = self.conn.execute(
            "SELECT id FROM inference_inquiry WHERE status='open' ORDER BY updated_at DESC"
        ).fetchall()
        return [self.inquiry(r["id"]) for r in ids]

    def resolve_inquiry(self, inquiry_id: int, outcome: str,
                        statement: str | None = None) -> int | None:
        inquiry = self.inquiry(inquiry_id)
        if inquiry is None or inquiry["status"] != "open":
            raise ValueError("inquiry is missing or already resolved")
        if outcome not in ("accepted", "tentative", "rejected", "awaiting_evidence"):
            raise ValueError("invalid inquiry outcome")
        source_id = inquiry["inference_id"]
        canonical_id = None
        final_statement = str(statement or inquiry["draft_claim"] or inquiry["prompt"]).strip()
        now = _now()
        if outcome in ("accepted", "tentative"):
            if not final_statement:
                raise ValueError("accepting a belief requires a statement")
            theme = "directed inquiry"
            source = self.get(source_id) if source_id is not None else None
            if source is not None:
                theme = source["theme"]
                self.conn.execute(
                    "UPDATE inference SET status='retired', validated_at=? WHERE id=?",
                    (now, source_id),
                )
            cur = self.conn.execute(
                "INSERT INTO inference (theme,statement,confidence,status,evidence,"
                "refines_id,source_refs,times_confirmed,created_at,validated_at,"
                "resolution_status,evidence_cutoff_id) "
                "VALUES (?,?,?,'confirmed',?,?,?,1,?,?,?,?)",
                (theme, crypto.enc(final_statement), inquiry["model_confidence"],
                 json.dumps({"method": "address-conversation", "inquiry_id": inquiry_id}),
                 source_id, json.dumps([]), now, now, outcome,
                 self._evidence_cutoff_id(theme)),
            )
            canonical_id = int(cur.lastrowid)
            self.absorb_similar_candidates(canonical_id, final_statement, theme=theme)
        elif source_id is not None:
            self.conn.execute(
                "UPDATE inference SET status=?, validated_at=? WHERE id=?",
                ("rejected" if outcome == "rejected" else "retired", now, source_id),
            )
        self.conn.execute(
            "UPDATE inference_inquiry SET status=?,outcome=?,canonical_id=?,resolved_at=?,"
            "updated_at=? WHERE id=?",
            (outcome, outcome, canonical_id, now, now, int(inquiry_id)),
        )
        self.conn.commit()
        return canonical_id

    def find_canonical_match(self, statement: str, *, theme: str | None = None,
                             threshold: float = 0.72):
        rows = self.conn.execute(
            "SELECT * FROM inference WHERE status='confirmed' ORDER BY id DESC"
        ).fetchall()
        best, best_score = None, 0.0
        for row in rows:
            score = concept_similarity(statement, crypto.dec(row["statement"]))
            needed = 0.58 if theme and row["theme"] == theme else threshold
            if score >= needed and score > best_score:
                best, best_score = row, score
        return best

    def absorb_similar_candidates(self, canonical_id: int, statement: str, *,
                                  theme: str | None = None) -> int:
        rows = self.conn.execute(
            "SELECT id,theme,statement FROM inference WHERE status='candidate'"
        ).fetchall()
        absorbed = 0
        for row in rows:
            score = concept_similarity(statement, crypto.dec(row["statement"]))
            needed = 0.58 if theme and row["theme"] == theme else 0.72
            if score >= needed:
                self.conn.execute(
                    "UPDATE inference SET status='retired',absorbed_by_id=?,validated_at=? "
                    "WHERE id=?", (canonical_id, _now(), row["id"]),
                )
                absorbed += 1
        return absorbed

    # --- proactive reflection --------------------------------------------
    def next_reflection(self):
        """A confirmed belief for the companion to volunteer back to you. Prefers
        core beliefs, then never-reflected, then least-recently reflected, then
        most confident. Returns a dict or None."""
        row = self.conn.execute(
            "SELECT * FROM inference WHERE status='confirmed' "
            "ORDER BY (times_confirmed >= ?) DESC, "
            "         (reflected_at IS NULL) DESC, reflected_at ASC, "
            "         confidence DESC, id DESC LIMIT 1",
            (CORE_BELIEF_CONFIRMATIONS,),
        ).fetchone()
        return self._dict(row) if row else None

    def mark_reflected(self, inference_id: int) -> None:
        self.conn.execute(
            "UPDATE inference SET reflected_at=? WHERE id=?", (_now(), inference_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
