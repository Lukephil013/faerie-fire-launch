"""The memory graph — the temporal second brain.

Each row in `memory` is one fact with a validity window. Facts are never
overwritten; when something changes, the old fact is closed out (valid_to set,
status='superseded') and a new fact is linked via supersedes_id.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date as _date

from .db import connect as db_connect
from .storage import now_iso
from . import crypto


def today() -> str:
    return _date.today().isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS category (
    name        TEXT PRIMARY KEY,
    description TEXT,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subject       TEXT DEFAULT 'user',
    category      TEXT,
    attribute     TEXT,
    value         TEXT,
    valid_from    TEXT,
    valid_to      TEXT,
    status        TEXT DEFAULT 'active',
    supersedes_id INTEGER,
    confidence    REAL,
    source_refs   TEXT,
    source_text   TEXT,
    approved_at   TEXT,
    FOREIGN KEY (supersedes_id) REFERENCES memory(id)
);
CREATE INDEX IF NOT EXISTS idx_mem_active ON memory(status);
CREATE INDEX IF NOT EXISTS idx_mem_cat ON memory(category);

CREATE TABLE IF NOT EXISTS pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,
    payload    TEXT,
    created_at TEXT,
    for_date   TEXT
);

CREATE TABLE IF NOT EXISTS rejected (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,
    category   TEXT,
    label      TEXT,        -- encrypted: the proposal text we should not re-propose
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memory_edge (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL,
    target_id     INTEGER NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'related',
    directed      INTEGER NOT NULL DEFAULT 0,
    strength      REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'proposed',
    evidence      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    approved_at   TEXT,
    FOREIGN KEY (source_id) REFERENCES memory(id),
    FOREIGN KEY (target_id) REFERENCES memory(id),
    CHECK (strength >= 0.0 AND strength <= 1.0),
    CHECK (status IN ('proposed', 'approved', 'rejected', 'retired')),
    UNIQUE (source_id, target_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_edge_status ON memory_edge(status);
CREATE INDEX IF NOT EXISTS idx_edge_source ON memory_edge(source_id);
CREATE INDEX IF NOT EXISTS idx_edge_target ON memory_edge(target_id);

CREATE TABLE IF NOT EXISTS core_profile_fact (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    section     TEXT NOT NULL,
    attribute   TEXT NOT NULL,
    value       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    priority    INTEGER NOT NULL DEFAULT 50,
    source_kind TEXT,
    source_id   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (status IN ('active','retired'))
);
CREATE INDEX IF NOT EXISTS idx_core_profile_status
ON core_profile_fact(status, priority DESC, section, attribute);
CREATE UNIQUE INDEX IF NOT EXISTS idx_core_profile_active_key
ON core_profile_fact(section, attribute) WHERE status='active';
"""


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "with", "user",
}
_DIRECTED_RELATIONS = {"supports", "contradicts", "causes", "part_of"}


def _tokens(value: object) -> set[str]:
    return {
        token for token in _TOKEN_RE.findall(str(value or "").lower())
        if len(token) > 1 and token not in _STOP_WORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def association_evidence(left: dict, right: dict) -> dict:
    """Return explainable deterministic signals for a pair of memories.

    The result is a relevance strength, not a calibrated probability. Values are
    used only during this calculation and are never copied into edge evidence.
    """
    same_category = bool(left.get("category") and left.get("category") == right.get("category"))
    attribute_similarity = _jaccard(_tokens(left.get("attribute")), _tokens(right.get("attribute")))
    value_similarity = _jaccard(_tokens(left.get("value")), _tokens(right.get("value")))
    supersession = (
        left.get("supersedes_id") == right.get("id")
        or right.get("supersedes_id") == left.get("id")
    )
    components = {
        "same_category": 0.24 if same_category else 0.0,
        "attribute_overlap": round(0.36 * attribute_similarity, 4),
        "value_overlap": round(0.30 * value_similarity, 4),
        "supersession": 1.0 if supersession else 0.0,
    }
    strength = 1.0 if supersession else min(0.95, sum(components.values()))
    return {
        "method": "deterministic-v1",
        "components": components,
        "strength": round(strength, 4),
    }


class MemoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(memory)")}
        if "source_text" not in columns:
            self.conn.execute("ALTER TABLE memory ADD COLUMN source_text TEXT")
        self.conn.commit()

    # --- categories ---
    def ensure_category(self, name: str, description: str = "", *, commit: bool = True) -> None:
        if not name:
            return
        exists = self.conn.execute(
            "SELECT 1 FROM category WHERE name = ?", (name,)
        ).fetchone()
        if not exists:
            self.conn.execute(
                "INSERT INTO category (name, description, created_at) VALUES (?, ?, ?)",
                (name, description, now_iso()),
            )
            if commit:
                self.conn.commit()

    def categories(self):
        return self.conn.execute("SELECT * FROM category ORDER BY name").fetchall()

    # --- writes ---
    def add(self, category, attribute, value, *, valid_from=None, confidence=None,
            source_refs=None, supersedes_id=None, raw_source=None,
            commit: bool = True) -> int:
        self.ensure_category(category, commit=commit)
        cur = self.conn.execute(
            "INSERT INTO memory (category, attribute, value, valid_from, valid_to, "
            "status, supersedes_id, confidence, source_refs, source_text, approved_at) "
            "VALUES (?, ?, ?, ?, NULL, 'active', ?, ?, ?, ?, ?)",
            (category, attribute, crypto.enc(value), valid_from or today(),
             supersedes_id, confidence, json.dumps(source_refs or []),
             crypto.enc(raw_source), now_iso()),
        )
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def supersede(self, old_id, value, *, attribute=None, confidence=None,
                  source_refs=None, as_of=None, commit: bool = True) -> int:
        """Close old_id and add the replacement. `as_of` (YYYY-MM-DD) lets
        historical imports date the transition; default is today."""
        old = self.get(old_id)
        if old is None:
            raise ValueError(f"memory {old_id} not found")
        ts = as_of or today()
        self.conn.execute(
            "UPDATE memory SET valid_to = ?, status = 'superseded' WHERE id = ?",
            (ts, old_id),
        )
        if commit:
            self.conn.commit()
        return self.add(old["category"], attribute or old["attribute"], value,
                        valid_from=ts, confidence=confidence,
                        source_refs=source_refs, supersedes_id=old_id, commit=commit)

    def provenance(self, memory_id: int) -> dict:
        row = self.get(memory_id)
        if row is None:
            raise ValueError(f"memory {memory_id} not found")
        return {
            "source_refs": json.loads(row["source_refs"] or "[]"),
            "raw_source": crypto.dec(row["source_text"]),
            "approved_at": row["approved_at"],
        }

    def retire(self, memory_id: int, *, as_of: str | None = None) -> None:
        """Reversible soft removal: keep provenance but exclude from recall."""
        if self.get(memory_id) is None:
            raise ValueError(f"memory {memory_id} not found")
        self.conn.execute(
            "UPDATE memory SET status='retired', valid_to=? WHERE id=?",
            (as_of or today(), memory_id),
        )
        self.conn.execute(
            "UPDATE memory_edge SET status='retired', updated_at=? "
            "WHERE source_id=? OR target_id=?",
            (now_iso(), memory_id, memory_id),
        )
        self.conn.commit()

    def forget(self, memory_id: int) -> dict:
        """Hard-delete one fact and same-database traces of its source.

        Backups and external mirrors are handled by `livingpc.forget` because
        they live outside this SQLite transaction.
        """
        row = self.get(memory_id)
        if row is None:
            raise ValueError(f"memory {memory_id} not found")
        category = row["category"]
        source_refs = json.loads(row["source_refs"] or "[]")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "DELETE FROM memory_edge WHERE source_id=? OR target_id=?",
                (memory_id, memory_id),
            )
            self.conn.execute(
                "UPDATE memory SET supersedes_id=NULL WHERE supersedes_id=?",
                (memory_id,),
            )
            tables = {
                r["name"] for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "curiosity_item" in tables:
                self.conn.execute(
                    "UPDATE curiosity_item SET answer=NULL, resulting_memory_id=NULL, "
                    "status='dismissed', resolved_at=? WHERE resulting_memory_id=?",
                    (now_iso(), memory_id),
                )
            if "clarification" in tables:
                self.conn.execute(
                    "DELETE FROM clarification WHERE memory_id=? OR resulting_memory_id=?",
                    (memory_id, memory_id),
                )
            self.conn.execute("DELETE FROM memory WHERE id=?", (memory_id,))
            self.conn.execute(
                "DELETE FROM category WHERE name=? AND NOT EXISTS "
                "(SELECT 1 FROM memory WHERE category=?)",
                (category, category),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"memory_id": memory_id, "category": category,
                "source_refs": source_refs}

    # --- reads ---
    def get(self, memory_id: int):
        return self.conn.execute(
            "SELECT * FROM memory WHERE id = ?", (memory_id,)
        ).fetchone()

    def active(self, category=None):
        if category:
            return self.conn.execute(
                "SELECT * FROM memory WHERE status='active' AND category=? "
                "ORDER BY category, attribute", (category,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM memory WHERE status='active' ORDER BY category, attribute"
        ).fetchall()

    def active_as_dicts(self, category=None) -> list[dict]:
        return [
            {"id": r["id"], "category": r["category"], "attribute": r["attribute"],
             "value": crypto.dec(r["value"]), "valid_from": r["valid_from"]}
            for r in self.active(category)
        ]

    # --- always-on core profile -----------------------------------------
    def upsert_core_profile_fact(self, section: str, attribute: str, value: str, *,
                                 priority: int = 50, source_kind: str = "manual",
                                 source_id: str | None = None,
                                 commit: bool = True) -> int:
        """Store one always-relevant self fact.

        Core profile facts are prompt context, not fuzzy retrieval candidates:
        they represent stable basics/constraints the user explicitly wants
        Faerie to consider broadly.
        """
        section = " ".join(str(section or "").split())
        attribute = " ".join(str(attribute or "").split())
        value = " ".join(str(value or "").split())
        if not section or not attribute:
            raise ValueError("section and attribute are required")
        if not value:
            raise ValueError("core profile value is required")
        priority = max(0, min(100, int(priority)))
        now = now_iso()
        existing = self.conn.execute(
            "SELECT id FROM core_profile_fact WHERE status='active' "
            "AND section=? AND attribute=?",
            (section, attribute),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE core_profile_fact SET value=?,priority=?,source_kind=?,"
                "source_id=?,updated_at=? WHERE id=?",
                (crypto.enc(value), priority, source_kind, source_id,
                 now, int(existing["id"])),
            )
            if commit:
                self.conn.commit()
            return int(existing["id"])
        cur = self.conn.execute(
            "INSERT INTO core_profile_fact "
            "(section,attribute,value,status,priority,source_kind,source_id,created_at,updated_at) "
            "VALUES (?,?,?,'active',?,?,?,?,?)",
            (section, attribute, crypto.enc(value), priority, source_kind,
             source_id, now, now),
        )
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def core_profile_facts(self, *, section: str | None = None,
                           active_only: bool = True,
                           limit: int | None = 50) -> list[dict]:
        where = []
        args: list[object] = []
        if active_only:
            where.append("status='active'")
        if section:
            where.append("section=?")
            args.append(str(section))
        sql = "SELECT * FROM core_profile_fact"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY priority DESC, section, attribute, id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [{
            "id": int(r["id"]),
            "section": r["section"],
            "attribute": r["attribute"],
            "value": crypto.dec(r["value"]) or "",
            "status": r["status"],
            "priority": int(r["priority"]),
            "source_kind": r["source_kind"],
            "source_id": r["source_id"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        } for r in rows]

    def core_profile_block(self, *, max_facts: int = 50,
                           max_chars: int = 4000) -> str:
        lines = []
        used = 0
        for fact in self.core_profile_facts(limit=max_facts):
            value = fact["value"]
            if len(value) > 360:
                value = value[:359].rstrip() + "…"
            line = f"- [{fact['section']}] {fact['attribute']}: {value}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) or "  (none yet)"

    def retire_core_profile_facts_by_source(self, source_kind: str, *,
                                            commit: bool = True) -> int:
        """Bulk-retire every active fact from one source (e.g. resetting Soul
        Calibration so all 13 questions resurface as unanswered)."""
        cur = self.conn.execute(
            "UPDATE core_profile_fact SET status='retired',updated_at=? "
            "WHERE status='active' AND source_kind=?",
            (now_iso(), str(source_kind or "")),
        )
        if commit:
            self.conn.commit()
        return int(cur.rowcount or 0)

    def retire_core_profile_fact(self, fact_id: int, *, commit: bool = True) -> None:
        self.conn.execute(
            "UPDATE core_profile_fact SET status='retired',updated_at=? WHERE id=?",
            (now_iso(), int(fact_id)),
        )
        if commit:
            self.conn.commit()

    def retire_core_profile_fact_key(self, section: str, attribute: str, *,
                                     commit: bool = True) -> int:
        cur = self.conn.execute(
            "UPDATE core_profile_fact SET status='retired',updated_at=? "
            "WHERE status='active' AND section=? AND attribute=?",
            (now_iso(), str(section or ""), str(attribute or "")),
        )
        if commit:
            self.conn.commit()
        return int(cur.rowcount or 0)

    def memories_as_dicts(self, include_superseded: bool = False) -> list[dict]:
        if include_superseded:
            rows = self.conn.execute(
                "SELECT * FROM memory ORDER BY category, attribute, id"
            ).fetchall()
        else:
            rows = self.active()
        return [
            {
                "id": r["id"], "subject": r["subject"], "category": r["category"],
                "attribute": r["attribute"], "value": crypto.dec(r["value"]),
                "valid_from": r["valid_from"], "valid_to": r["valid_to"],
                "status": r["status"], "supersedes_id": r["supersedes_id"],
                "confidence": r["confidence"],
            }
            for r in rows
        ]

    # --- explainable memory associations ---------------------------------
    def propose_associations(self, *, min_strength: float = 0.40,
                             max_edges: int = 250) -> int:
        """Create or refresh deterministic proposed edges between active memories.

        Approved and rejected decisions are never overwritten. The return value
        counts newly created proposals, not existing proposals whose evidence was
        refreshed.
        """
        memories = self.memories_as_dicts()
        candidates = []
        for index, left in enumerate(memories):
            for right in memories[index + 1:]:
                evidence = association_evidence(left, right)
                if evidence["strength"] >= min_strength:
                    candidates.append((evidence["strength"], left["id"], right["id"], evidence))
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        created = 0
        timestamp = now_iso()
        for strength, source_id, target_id, evidence in candidates[:max_edges]:
            existing = self.conn.execute(
                "SELECT id, status FROM memory_edge WHERE source_id=? AND target_id=? "
                "AND relation_type='related'", (source_id, target_id),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO memory_edge (source_id, target_id, relation_type, directed, "
                    "strength, status, evidence, created_at, updated_at) "
                    "VALUES (?, ?, 'related', 0, ?, 'proposed', ?, ?, ?)",
                    (source_id, target_id, strength, json.dumps(evidence), timestamp, timestamp),
                )
                created += 1
            elif existing["status"] == "proposed":
                self.conn.execute(
                    "UPDATE memory_edge SET strength=?, evidence=?, updated_at=? WHERE id=?",
                    (strength, json.dumps(evidence), timestamp, existing["id"]),
                )
        self.conn.commit()
        return created

    def add_association(self, source_id: int, target_id: int, *,
                        relation_type: str = "related", strength: float = 0.75,
                        directed: bool | None = None, status: str = "proposed",
                        evidence: dict | None = None) -> int:
        if source_id == target_id:
            raise ValueError("an association requires two different memories")
        if self.get(source_id) is None or self.get(target_id) is None:
            raise ValueError("association memory not found")
        source_id, target_id = int(source_id), int(target_id)
        relation_type = relation_type.strip() or "related"
        directed = relation_type in _DIRECTED_RELATIONS if directed is None else bool(directed)
        if not directed:
            source_id, target_id = sorted((source_id, target_id))
        strength = max(0.0, min(1.0, float(strength)))
        if status not in {"proposed", "approved", "rejected", "retired"}:
            raise ValueError(f"invalid association status: {status}")
        timestamp = now_iso()
        approved_at = timestamp if status == "approved" else None
        cur = self.conn.execute(
            "INSERT INTO memory_edge (source_id, target_id, relation_type, directed, "
            "strength, status, evidence, created_at, updated_at, approved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, target_id, relation_type, int(directed),
             strength, status, json.dumps(evidence or {"method": "manual"}), timestamp,
             timestamp, approved_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_associations(self, *, status: str | None = None,
                          active_only: bool = True):
        clauses = []
        params: list[object] = []
        if status:
            clauses.append("e.status = ?")
            params.append(status)
        if active_only:
            clauses.extend(["s.status = 'active'", "t.status = 'active'"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.conn.execute(
            "SELECT e.* FROM memory_edge e "
            "JOIN memory s ON s.id=e.source_id JOIN memory t ON t.id=e.target_id" +
            where + " ORDER BY e.strength DESC, e.id", params,
        ).fetchall()

    def update_association(self, edge_id: int, *, status: str | None = None,
                           relation_type: str | None = None,
                           strength: float | None = None) -> None:
        edge = self.conn.execute("SELECT * FROM memory_edge WHERE id=?", (edge_id,)).fetchone()
        if edge is None:
            raise ValueError(f"association {edge_id} not found")
        next_status = status or edge["status"]
        if next_status not in {"proposed", "approved", "rejected", "retired"}:
            raise ValueError(f"invalid association status: {next_status}")
        next_strength = edge["strength"] if strength is None else max(0.0, min(1.0, float(strength)))
        next_relation = relation_type.strip() if relation_type is not None else edge["relation_type"]
        next_relation = next_relation or "related"
        next_directed = int(next_relation in _DIRECTED_RELATIONS)
        timestamp = now_iso()
        approved_at = timestamp if next_status == "approved" else edge["approved_at"]
        self.conn.execute(
            "UPDATE memory_edge SET status=?, relation_type=?, directed=?, strength=?, updated_at=?, "
            "approved_at=? WHERE id=?",
            (next_status, next_relation, next_directed, next_strength, timestamp,
             approved_at, edge_id),
        )
        self.conn.commit()

    def graph_data(self, *, include_superseded: bool = False) -> dict:
        memories = self.memories_as_dicts(include_superseded=include_superseded)
        allowed_ids = {memory["id"] for memory in memories}
        edges = []
        for row in self.list_associations(active_only=not include_superseded):
            if row["source_id"] not in allowed_ids or row["target_id"] not in allowed_ids:
                continue
            edges.append({
                "id": row["id"], "source_id": row["source_id"],
                "target_id": row["target_id"], "relation_type": row["relation_type"],
                "directed": bool(row["directed"]), "strength": row["strength"],
                "status": row["status"], "evidence": json.loads(row["evidence"] or "{}"),
            })
        return {"nodes": memories, "edges": edges}

    def history(self, category: str, attribute: str):
        return self.conn.execute(
            "SELECT * FROM memory WHERE category=? AND attribute=? ORDER BY valid_from",
            (category, attribute),
        ).fetchall()

    # --- pending proposals ---
    def add_pending(self, kind: str, payload: dict, for_date: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO pending (kind, payload, created_at, for_date) VALUES (?, ?, ?, ?)",
            (kind, json.dumps(payload), now_iso(), for_date),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_pending(self):
        return self.conn.execute("SELECT * FROM pending ORDER BY id").fetchall()

    def count_pending(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def clear_pending(self, pending_id=None, *, commit: bool = True) -> None:
        if pending_id is None:
            self.conn.execute("DELETE FROM pending")
        else:
            self.conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
        if commit:
            self.conn.commit()

    # --- rejection memory (soft, scoped, clearable) ------------------------
    # We remember what you declined ONLY to avoid re-proposing the *same* item.
    # It is fed to the model as soft guidance, capped to recent entries, and can
    # be wiped anytime with clear_rejections(). It never blocks a genuinely new
    # or differently-scoped fact, and never touches approved memory.
    def add_rejection(self, kind: str, category: str, label: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO rejected (kind, category, label, created_at) VALUES (?, ?, ?, ?)",
            (kind, category, crypto.enc(label), now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_rejections(self, limit: int = 25, days: int = 14) -> list[dict]:
        """Most-recent declined items (decrypted), capped and time-windowed so
        the prompt can't bloat and old 'no's eventually stop being suppressed."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT kind, category, label FROM rejected WHERE created_at >= ? "
            "ORDER BY id DESC LIMIT ?", (cutoff, limit),
        ).fetchall()
        return [{"kind": r["kind"], "category": r["category"],
                 "label": crypto.dec(r["label"])} for r in rows]

    def count_rejections(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM rejected").fetchone()[0])

    def clear_rejections(self) -> None:
        self.conn.execute("DELETE FROM rejected")
        self.conn.commit()

    # --- meta (small key/value store; e.g. last-triage watermark) ----------
    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        if commit:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
