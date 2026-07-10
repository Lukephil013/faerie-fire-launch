"""Memory consolidation — the scale plan for the second brain.

The memory graph only grows: triage auto-commits facts nightly, the inference
loop files evidence every ~25 minutes, and rejections accumulate forever. At ~100
facts that's fine; at a few thousand, recall quality and the O(n^2) association
pass degrade. This module is the deterministic, offline hygiene pass:

1. **Merge duplicate facts.** Active facts with the same category+attribute whose
   values are the same (normalized) or nearly the same (token Jaccard >= threshold)
   are collapsed: the newest survives untouched, older copies are *closed*
   (valid_to set, status 'superseded', a `consolidated_into` note in their
   source_refs). Nothing is ever deleted — the trajectory survives, exactly like
   a supersession.
2. **Prune stale rejection rows.** Rejections are soft guidance read only from
   the last 14 days (`recent_rejections`); rows older than the retention window
   are dead weight and are deleted. This does not touch memories.
3. **Prune stale inference evidence.** Evidence rows only inform synthesis of
   current themes; rows older than the retention window are deleted (0 disables).

Distinct-value facts under the same attribute are NEVER merged — two projects
are two facts. Only near-identical wordings collapse. Runs nightly in the daemon
(before backup, so snapshots capture the tidy state) and on demand via
`python tools/consolidate_memory.py`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .memory import MemoryStore, _jaccard, _tokens, today
from .storage import now_iso

_WS_RE = re.compile(r"\s+")


def _norm(value: str) -> str:
    return _WS_RE.sub(" ", str(value or "").strip().casefold())


def _is_duplicate(a: str, b: str, similarity: float) -> bool:
    na, nb = _norm(a), _norm(b)
    if na and na == nb:
        return True
    return _jaccard(_tokens(a), _tokens(b)) >= similarity


def find_duplicates(mem: MemoryStore, *, similarity: float = 0.85) -> list[dict]:
    """Groups of active facts that say the same thing.

    Returns [{"survivor": fact, "duplicates": [fact, ...]}]; survivor is the
    newest (highest id). Facts are dicts with decrypted values.
    """
    groups: dict[tuple, list[dict]] = {}
    for fact in mem.active_as_dicts():
        key = (fact["category"] or "", _norm(fact["attribute"]))
        groups.setdefault(key, []).append(fact)

    found = []
    for facts in groups.values():
        if len(facts) < 2:
            continue
        facts = sorted(facts, key=lambda f: f["id"], reverse=True)  # newest first
        claimed: set[int] = set()
        for i, survivor in enumerate(facts):
            if survivor["id"] in claimed:
                continue
            dups = [
                other for other in facts[i + 1:]
                if other["id"] not in claimed
                and _is_duplicate(survivor["value"], other["value"], similarity)
            ]
            if dups:
                claimed.update(d["id"] for d in dups)
                found.append({"survivor": survivor, "duplicates": dups})
    return found


def _close_duplicate(mem: MemoryStore, dup_id: int, survivor_id: int) -> None:
    """Close a duplicate like a supersession: never delete, only annotate."""
    row = mem.get(dup_id)
    try:
        refs = json.loads(row["source_refs"] or "[]")
    except (TypeError, ValueError):
        refs = []
    refs.append({"consolidated_into": int(survivor_id), "at": now_iso()})
    mem.conn.execute(
        "UPDATE memory SET valid_to = ?, status = 'superseded', source_refs = ? "
        "WHERE id = ?",
        (today(), json.dumps(refs), dup_id),
    )


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _prune_rejections(mem: MemoryStore, retention_days: int, dry_run: bool) -> int:
    if retention_days <= 0:
        return 0
    cutoff = _cutoff(retention_days)
    if dry_run:
        return int(mem.conn.execute(
            "SELECT COUNT(*) FROM rejected WHERE created_at < ?", (cutoff,)
        ).fetchone()[0])
    return mem.conn.execute(
        "DELETE FROM rejected WHERE created_at < ?", (cutoff,)).rowcount


def _prune_evidence(mem: MemoryStore, retention_days: int, dry_run: bool) -> int:
    """Evidence lives in the inference schema inside the same memory.db; the
    table may not exist if the inference engine never ran."""
    if retention_days <= 0:
        return 0
    exists = mem.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'"
    ).fetchone()
    if not exists:
        return 0
    cutoff = _cutoff(retention_days)
    if dry_run:
        return int(mem.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE created_at < ?", (cutoff,)
        ).fetchone()[0])
    return mem.conn.execute(
        "DELETE FROM evidence WHERE created_at < ?", (cutoff,)).rowcount


def report(mem: MemoryStore) -> dict:
    """Size snapshot: where the growth is. Counts only, no values."""
    def _count(sql: str) -> int:
        return int(mem.conn.execute(sql).fetchone()[0])
    per_category = {
        r["category"] or "(none)": r["n"] for r in mem.conn.execute(
            "SELECT category, COUNT(*) AS n FROM memory WHERE status='active' "
            "GROUP BY category ORDER BY n DESC")
    }
    has_evidence = mem.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'").fetchone()
    return {
        "active": _count("SELECT COUNT(*) FROM memory WHERE status='active'"),
        "superseded": _count("SELECT COUNT(*) FROM memory WHERE status='superseded'"),
        "edges": _count("SELECT COUNT(*) FROM memory_edge"),
        "rejections": _count("SELECT COUNT(*) FROM rejected"),
        "evidence": _count("SELECT COUNT(*) FROM evidence") if has_evidence else 0,
        "per_category": per_category,
    }


def consolidate(mem: MemoryStore, *, similarity: float = 0.85,
                rejection_retention_days: int = 90,
                evidence_retention_days: int = 180,
                dry_run: bool = False) -> dict:
    """Run the full hygiene pass. Returns what happened (or would happen).

    {"merged", "groups", "pruned_rejections", "pruned_evidence",
     "active_before", "active_after", "merges": [(dup_id, survivor_id), ...]}
    """
    before = report(mem)["active"]
    groups = find_duplicates(mem, similarity=similarity)
    merges = [
        (dup["id"], group["survivor"]["id"])
        for group in groups for dup in group["duplicates"]
    ]
    if not dry_run:
        for dup_id, survivor_id in merges:
            _close_duplicate(mem, dup_id, survivor_id)
    pruned_rej = _prune_rejections(mem, rejection_retention_days, dry_run)
    pruned_ev = _prune_evidence(mem, evidence_retention_days, dry_run)
    if not dry_run:
        mem.conn.commit()
    return {
        "merged": len(merges), "groups": len(groups),
        "pruned_rejections": pruned_rej, "pruned_evidence": pruned_ev,
        "active_before": before,
        "active_after": before - (0 if dry_run else len(merges)),
        "merges": merges, "dry_run": dry_run,
    }
