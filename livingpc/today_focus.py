"""Pick which active Leaves (tasks) are most worth focusing on today.

This is intentionally a *model* judgment call rather than a fixed sort —
priority/due-date alone don't capture "this is actually what matters right
now." The result is cached once per calendar day in MemoryStore's meta table
so opening the Command Center repeatedly doesn't re-spend tokens; a manual
refresh (force=True) recomputes immediately.

If there's no API key, the backend is set to "stub", or the model call
fails for any reason, we fall back to a simple priority/due-date heuristic
so the widget is never just empty.
"""
from __future__ import annotations

import json
import os
import time
import hashlib
from datetime import date

from .diagnostics import log_diag

_META_DATE_KEY = "today_focus_date"
_META_DATA_KEY = "today_focus_json"
_META_SIGNATURE_KEY = "today_focus_signature"

_SYSTEM = (
    "You help someone decide which 1-3 active tasks (called Leaves) are most "
    "worth focusing on today, out of a longer list. Weigh stated priority, "
    "due dates, and anything that looks stale or blocked. Reply with ONLY a "
    "JSON array, no prose, no markdown fences, like: "
    '[{"id": 12, "mode":"main", "reason": "one short sentence"}]. '
    "Pick one main task and, when useful, one low_energy alternative. If "
    "everything looks equally low-stakes, it's fine to pick just 1, or none "
    "at all if nothing stands out."
)


def _collect_leaves(node, out=None):
    out = out if out is not None else []
    if not node:
        return out
    if node.get("type") == "task" and node.get("status") not in ("archived", "paused"):
        out.append(node)
    for child in (node.get("children") or []):
        _collect_leaves(child, out)
    return out


def _fallback_picks(active_leaves):
    order = {"high": 3, "normal": 2, "low": 1}

    def rank(t):
        p = order.get(t.get("priority") or "normal", 2)
        due = t.get("due_date") or "9999-99-99"
        return (-p, due)

    ranked = sorted(active_leaves, key=rank)
    return [{"id": t["id"], "title": t.get("title", ""),
             "mode": "main" if i == 0 else "low_energy",
             "reason": "Sorted by priority, due date, and recent goal state."}
            for i, t in enumerate(ranked[:2])]


def _ask_model(config, active_leaves):
    backend = (getattr(config, "curiosity_backend", "") or
               getattr(config, "inference_backend", "claude")).lower()
    if backend == "stub":
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    from anthropic import Anthropic
    model = getattr(config, "curiosity_model", "claude-haiku-4-5")
    client = Anthropic(api_key=api_key,
                       timeout=getattr(config, "llm_timeout_seconds", 60.0))
    lines = [f"- id={t['id']} title={t.get('title', '')!r} "
             f"priority={t.get('priority', 'normal')} due={t.get('due_date') or 'none'} "
             f"completion={t.get('completion') or 'unknown'} mastery={t.get('mastery') or 'unknown'} "
             f"updated={t.get('updated_at') or 'unknown'} description={str(t.get('description') or '')[:280]!r}"
             for t in active_leaves[:40]]
    prompt = "Active Leaves:\n" + "\n".join(lines)
    started = time.monotonic()
    msg = client.messages.create(model=model, max_tokens=400, system=_SYSTEM,
                                  messages=[{"role": "user", "content": prompt}])
    from .llm_usage import record_response
    record_response("today_focus", model, msg, time.monotonic() - started)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return None
    data = json.loads(text[start:end + 1])
    by_id = {t["id"]: t for t in active_leaves}
    picks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        if tid not in by_id:
            continue
        mode = str(item.get("mode") or "main").strip().lower()
        if mode not in {"main", "low_energy"}:
            mode = "main"
        picks.append({"id": tid, "title": by_id[tid].get("title", ""), "mode": mode,
                      "reason": str(item.get("reason") or "").strip()[:200]})
    return picks[:3] or None


def get_today_focus(config, mem, tree, *, force=False):
    today = date.today().isoformat()
    active = [t for t in _collect_leaves(tree) if t.get("status") != "completed"]
    signature = hashlib.sha256(json.dumps([
        {key: task.get(key) for key in ("id", "title", "description", "priority", "due_date",
                                         "completion", "mastery", "updated_at", "status")}
        for task in active
    ], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if not force:
        cached_date = mem.get_meta(_META_DATE_KEY)
        if cached_date == today and mem.get_meta(_META_SIGNATURE_KEY) == signature:
            cached = mem.get_meta(_META_DATA_KEY)
            if cached is not None:
                try:
                    return {"ok": True, "date": today, "picks": json.loads(cached),
                             "source": "cached"}
                except ValueError:
                    pass

    if not active:
        mem.set_meta(_META_DATE_KEY, today)
        mem.set_meta(_META_DATA_KEY, json.dumps([]))
        mem.set_meta(_META_SIGNATURE_KEY, signature)
        return {"ok": True, "date": today, "picks": [], "source": "none"}

    picks = None
    try:
        picks = _ask_model(config, active)
    except Exception as error:
        log_diag("today_focus", f"model pick failed error={type(error).__name__}: {error}")
        picks = None
    source = "model"
    if not picks:
        picks = _fallback_picks(active)
        source = "fallback"

    mem.set_meta(_META_DATE_KEY, today)
    mem.set_meta(_META_DATA_KEY, json.dumps(picks))
    mem.set_meta(_META_SIGNATURE_KEY, signature)
    return {"ok": True, "date": today, "picks": picks, "source": source}
