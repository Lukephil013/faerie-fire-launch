"""On-demand activity reports: markdown summaries of what's actually in the
app's databases.

Scoped deliberately to what a 'launch' profile build (Faerie Fire Launch /
Faerie Fire Korean) populates: the Growth tree, Investigations, GoalAI agent
activity (user-triggered, not scheduled, in this profile), chats, and Soul
Calibration facts. Screen/capture summaries, the passive Inference engine,
general Memory (fact/pending/rejected), and Journal Import are intentionally
left out — launch profile hard-disables ocr/browser/clipboard capture and the
nightly triage/inference scheduler (see config.py load()), so those tables
never get populated there and would only ever show empty sections.

Both reports read the same single SQLite file (cfg.memory_db_path) that
GoalStore/CuriosityStore/ChatStore/MemoryStore all already point at, so this
module talks to it directly with plain SQL rather than instantiating every
store class.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import crypto

TYPE_LABEL = {"umbrella": "Soul", "overgoal": "Root", "subgoal": "Branch", "task": "Leaf"}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _dec(value) -> str:
    if not value:
        return ""
    try:
        return crypto.dec(value) or ""
    except Exception:
        return str(value)


def _in_range(ts: str | None, start: str | None, end: str | None) -> bool:
    if not ts:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts >= end:
        return False
    return True


def _local_day_bounds_utc(date_str: str) -> tuple[str, str]:
    """A local calendar day -> [start, end) as UTC ISO strings, matching how
    created_at/updated_at are stored (datetime.now(timezone.utc).isoformat())."""
    local_tz = datetime.now().astimezone().tzinfo
    start_local = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat())


def _to_local_day(ts: str | None) -> str:
    """UTC ISO timestamp -> local YYYY-MM-DD, for the full-report timeline."""
    if not ts:
        return "?"
    try:
        value = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date().isoformat()
    except Exception:
        return ts[:10] if len(ts) >= 10 else "?"


# --------------------------------------------------------------- tree lookup
def _load_nodes(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT id,parent_id,node_type,title,status,created_at,updated_at,"
        "completed_at FROM goal_node ORDER BY parent_id,position,id").fetchall()
    nodes = {}
    for r in rows:
        nodes[r["id"]] = {
            "id": r["id"], "parent_id": r["parent_id"],
            "type": TYPE_LABEL.get(r["node_type"], r["node_type"]),
            "title": _dec(r["title"]) or "(untitled)",
            "status": r["status"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "completed_at": r["completed_at"],
        }
    return nodes


def _node_path(nodes: dict[int, dict], node_id: int | None) -> str:
    parts = []
    seen = set()
    while node_id is not None and node_id in nodes and node_id not in seen:
        seen.add(node_id)
        node = nodes[node_id]
        parts.append(f"[{node['type']}] {node['title']}")
        node_id = node["parent_id"]
    return " > ".join(reversed(parts)) if parts else "(unknown node)"


def _node_ref(nodes: dict[int, dict], node_id: int | None) -> str:
    if node_id in nodes:
        node = nodes[node_id]
        return f"**{node['title']}** ({node['type']})"
    return "*(node no longer exists)*"


# --------------------------------------------------------------------- Growth
def _section_growth(conn, nodes, start, end) -> tuple[str, list[str]]:
    lines = []
    day_keys = []
    created = [n for n in nodes.values() if _in_range(n["created_at"], start, end)]
    created_ids = {n["id"] for n in created}
    updated = [n for n in nodes.values()
               if _in_range(n["updated_at"], start, end) and n["id"] not in created_ids]
    completed = [n for n in nodes.values() if _in_range(n["completed_at"], start, end)]
    evidence = conn.execute(
        "SELECT goal_id,source_kind,label,id FROM goal_evidence_link ORDER BY id").fetchall()
    origins = conn.execute(
        "SELECT goal_id,source_kind,source_label,created_at FROM goal_origin"
    ).fetchall()
    origin_by_goal = {r["goal_id"]: r for r in origins}

    if not (created or updated or completed):
        lines.append("_No Growth tree changes._")
    if created:
        lines.append("**New nodes:**")
        for n in sorted(created, key=lambda n: n["created_at"]):
            origin = origin_by_goal.get(n["id"])
            via = f" — from {origin['source_kind']}" if origin else ""
            lines.append(f"- {_node_path(nodes, n['parent_id']) or 'Soul'} > "
                         f"**{n['title']}** ({n['type']}){via} · {n['created_at']}")
            day_keys.append(n["created_at"])
    if updated:
        lines.append("\n**Updated nodes:**")
        for n in sorted(updated, key=lambda n: n["updated_at"]):
            lines.append(f"- {_node_path(nodes, n['id'])} · {n['updated_at']}")
            day_keys.append(n["updated_at"])
    if completed:
        lines.append("\n**Completed:**")
        for n in sorted(completed, key=lambda n: n["completed_at"]):
            lines.append(f"- {_node_path(nodes, n['id'])} · {n['completed_at']}")
            day_keys.append(n["completed_at"])

    ev_lines = []
    for row in evidence:
        # goal_evidence_link has no created_at column, so it can't be date
        # filtered — only shown in the full report, grouped under its node.
        if start is None and end is None and row["goal_id"] in nodes:
            ev_lines.append(f"- {_node_path(nodes, row['goal_id'])} — "
                           f"{_dec(row['label']) or row['source_kind']}")
    if ev_lines:
        lines.append("\n**Evidence attached (all time):**")
        lines.extend(ev_lines)

    return "\n".join(lines), day_keys


# --------------------------------------------------------- Investigations
def _section_investigations(conn, start, end) -> tuple[str, list[str]]:
    lines = []
    day_keys = []
    curiosities = conn.execute(
        "SELECT id,directive,label,status,created_at FROM curiosity ORDER BY id").fetchall()
    cur_label = {r["id"]: _dec(r["label"]) or "(untitled)" for r in curiosities}
    new_cur = [r for r in curiosities if _in_range(r["created_at"], start, end)]
    if new_cur:
        lines.append("**New investigations:**")
        for r in new_cur:
            lines.append(f"- \"{cur_label[r['id']]}\" ({r['status']}) · {r['created_at']}")
            day_keys.append(r["created_at"])

    items = conn.execute(
        "SELECT id,curiosity_id,kind,status,created_at,resolved_at FROM curiosity_item"
    ).fetchall()
    new_items = [r for r in items if _in_range(r["created_at"], start, end)]
    resolved_items = [r for r in items if _in_range(r["resolved_at"], start, end)]
    if new_items:
        lines.append("\n**New questions/suggestions:**")
        for r in new_items:
            label = cur_label.get(r["curiosity_id"], "(unknown investigation)")
            lines.append(f"- [{r['kind']}] on \"{label}\" · {r['created_at']}")
            day_keys.append(r["created_at"])
    if resolved_items:
        lines.append("\n**Resolved questions/suggestions:**")
        for r in resolved_items:
            label = cur_label.get(r["curiosity_id"], "(unknown investigation)")
            lines.append(f"- [{r['kind']}] on \"{label}\" -> {r['status']} · {r['resolved_at']}")
            day_keys.append(r["resolved_at"])

    proposals = conn.execute(
        "SELECT id,curiosity_id,proposal_type,status,created_at,resolved_at "
        "FROM curiosity_classification_proposal").fetchall()
    new_props = [r for r in proposals if _in_range(r["created_at"], start, end)]
    resolved_props = [r for r in proposals if _in_range(r["resolved_at"], start, end)]
    if new_props:
        lines.append("\n**New placement proposals:**")
        for r in new_props:
            label = cur_label.get(r["curiosity_id"], "(unknown investigation)")
            lines.append(f"- \"{label}\" -> {r['proposal_type']} · {r['created_at']}")
            day_keys.append(r["created_at"])
    if resolved_props:
        lines.append("\n**Resolved placement proposals:**")
        for r in resolved_props:
            label = cur_label.get(r["curiosity_id"], "(unknown investigation)")
            lines.append(f"- \"{label}\" -> {r['proposal_type']} ({r['status']}) · {r['resolved_at']}")
            day_keys.append(r["resolved_at"])

    if not lines:
        lines.append("_No Investigation activity._")
    return "\n".join(lines), day_keys


# --------------------------------------------------------- GoalAI activity
def _section_goal_ai(conn, nodes, start, end) -> tuple[str, list[str]]:
    lines = []
    day_keys = []
    assessments = conn.execute(
        "SELECT id,node_id,health,confidence,created_at FROM goal_agent_assessment"
    ).fetchall()
    new_assess = [r for r in assessments if _in_range(r["created_at"], start, end)]
    if new_assess:
        lines.append("**Assessments run:**")
        for r in new_assess:
            lines.append(f"- {_node_ref(nodes, r['node_id'])} — {r['health']} "
                         f"({round((r['confidence'] or 0) * 100)}% confidence) · {r['created_at']}")
            day_keys.append(r["created_at"])

    questions = conn.execute(
        "SELECT id,node_id,status,created_at,resolved_at FROM goal_agent_question").fetchall()
    new_q = [r for r in questions if _in_range(r["created_at"], start, end)]
    resolved_q = [r for r in questions if _in_range(r["resolved_at"], start, end)]
    if new_q:
        lines.append("\n**New follow-up questions:**")
        for r in new_q:
            lines.append(f"- {_node_ref(nodes, r['node_id'])} · {r['created_at']}")
            day_keys.append(r["created_at"])
    if resolved_q:
        lines.append("\n**Questions resolved:**")
        for r in resolved_q:
            lines.append(f"- {_node_ref(nodes, r['node_id'])} -> {r['status']} · {r['resolved_at']}")
            day_keys.append(r["resolved_at"])

    proposals = conn.execute(
        "SELECT id,agent_node_id,target_node_id,proposal_type,status,created_at,"
        "resolved_at FROM goal_agent_proposal").fetchall()
    new_p = [r for r in proposals if _in_range(r["created_at"], start, end)]
    resolved_p = [r for r in proposals if _in_range(r["resolved_at"], start, end)]
    if new_p:
        lines.append("\n**New proposals:**")
        for r in new_p:
            lines.append(f"- {r['proposal_type']} on {_node_ref(nodes, r['target_node_id'])} "
                         f"(from {_node_ref(nodes, r['agent_node_id'])}) · {r['created_at']}")
            day_keys.append(r["created_at"])
    if resolved_p:
        lines.append("\n**Resolved proposals:**")
        for r in resolved_p:
            lines.append(f"- {r['proposal_type']} on {_node_ref(nodes, r['target_node_id'])} "
                         f"-> {r['status']} · {r['resolved_at']}")
            day_keys.append(r["resolved_at"])

    harvests = conn.execute(
        "SELECT id,source_node_id,status,created_at,committed_at FROM goal_harvest"
    ).fetchall()
    new_h = [r for r in harvests if _in_range(r["created_at"], start, end)]
    committed_h = [r for r in harvests if _in_range(r["committed_at"], start, end)]
    if new_h:
        lines.append("\n**Harvests drafted:**")
        for r in new_h:
            lines.append(f"- from {_node_ref(nodes, r['source_node_id'])} · {r['created_at']}")
            day_keys.append(r["created_at"])
    if committed_h:
        lines.append("\n**Harvests committed:**")
        for r in committed_h:
            lines.append(f"- from {_node_ref(nodes, r['source_node_id'])} · {r['committed_at']}")
            day_keys.append(r["committed_at"])

    if not lines:
        lines.append("_No GoalAI agent activity._ (This only happens when you manually ask "
                     "an agent to review or draft something — nothing runs on a schedule "
                     "in this profile.)")
    return "\n".join(lines), day_keys


# ------------------------------------------------------------------- Chats
def _section_chats(conn, nodes, start, end) -> tuple[str, list[str]]:
    lines = []
    day_keys = []
    cc_messages = conn.execute(
        "SELECT id,chat_id,created_at FROM companion_message").fetchall()
    cc_in_range = [r for r in cc_messages if _in_range(r["created_at"], start, end)]
    if cc_in_range:
        by_chat: dict[str, int] = {}
        for r in cc_in_range:
            by_chat[r["chat_id"]] = by_chat.get(r["chat_id"], 0) + 1
            day_keys.append(r["created_at"])
        lines.append(f"**Command Center:** {len(cc_in_range)} message(s) "
                     f"across {len(by_chat)} conversation(s).")

    agent_messages = conn.execute(
        "SELECT id,node_id,created_at FROM goal_agent_message").fetchall()
    agent_in_range = [r for r in agent_messages if _in_range(r["created_at"], start, end)]
    if agent_in_range:
        by_node: dict[int, int] = {}
        for r in agent_in_range:
            by_node[r["node_id"]] = by_node.get(r["node_id"], 0) + 1
            day_keys.append(r["created_at"])
        lines.append("\n**Per-node agent chats:**")
        for node_id, count in by_node.items():
            lines.append(f"- {_node_ref(nodes, node_id)}: {count} message(s)")

    if not lines:
        lines.append("_No chat activity._")
    return "\n".join(lines), day_keys


# --------------------------------------------------------- Soul Calibration
def _section_soul_facts(conn, start, end) -> tuple[str, list[str]]:
    lines = []
    day_keys = []
    rows = conn.execute(
        "SELECT id,section,attribute,value,source_kind,created_at,updated_at "
        "FROM core_profile_fact ORDER BY section,attribute").fetchall()
    new_facts = [r for r in rows if _in_range(r["created_at"], start, end)]
    new_fact_ids = {r["id"] for r in new_facts}
    updated_facts = [r for r in rows if _in_range(r["updated_at"], start, end)
                     and r["id"] not in new_fact_ids]
    if new_facts:
        lines.append("**New facts saved:**")
        for r in new_facts:
            lines.append(f"- [{r['section']}] {r['attribute']}: "
                         f"{_dec(r['value'])[:120]} (via {r['source_kind']}) · {r['created_at']}")
            day_keys.append(r["created_at"])
    if updated_facts:
        lines.append("\n**Facts updated:**")
        for r in updated_facts:
            lines.append(f"- [{r['section']}] {r['attribute']}: "
                         f"{_dec(r['value'])[:120]} · {r['updated_at']}")
            day_keys.append(r["updated_at"])
    if not lines:
        lines.append("_No Soul Calibration / profile-fact changes._")
    return "\n".join(lines), day_keys


# ------------------------------------------------------------------ builder
def _build_report(cfg, *, title: str, start: str | None, end: str | None,
                  include_timeline: bool) -> str:
    conn = _connect(cfg.memory_db_path)
    try:
        nodes = _load_nodes(conn)
        all_day_keys: list[str] = []
        growth_md, keys = _section_growth(conn, nodes, start, end); all_day_keys += keys
        inv_md, keys = _section_investigations(conn, start, end); all_day_keys += keys
        goalai_md, keys = _section_goal_ai(conn, nodes, start, end); all_day_keys += keys
        chats_md, keys = _section_chats(conn, nodes, start, end); all_day_keys += keys
        soul_md, keys = _section_soul_facts(conn, start, end); all_day_keys += keys
    finally:
        conn.close()

    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    parts = [f"# {title}", f"_Generated {generated}_", ""]

    if include_timeline and all_day_keys:
        counts: dict[str, int] = {}
        for ts in all_day_keys:
            day = _to_local_day(ts)
            counts[day] = counts.get(day, 0) + 1
        parts.append("## Timeline (entries per day)")
        parts.append("")
        parts.append("| Date | Entries |")
        parts.append("|---|---|")
        for day in sorted(counts):
            parts.append(f"| {day} | {counts[day]} |")
        parts.append("")

    parts += [
        "## Growth Tree (Soul / Root / Branch / Leaf)", "", growth_md, "",
        "## Investigations", "", inv_md, "",
        "## GoalAI Agent Activity", "", goalai_md, "",
        "## Chats", "", chats_md, "",
        "## Soul Calibration / Profile Facts", "", soul_md, "",
    ]
    return "\n".join(parts)


def build_daily_report(cfg, date_str: str | None = None) -> tuple[str, str]:
    """Returns (markdown, date_str_used)."""
    date_str = date_str or datetime.now().astimezone().date().isoformat()
    start, end = _local_day_bounds_utc(date_str)
    title = f"Faerie Fire — Daily Report — {date_str}"
    return _build_report(cfg, title=title, start=start, end=end,
                         include_timeline=False), date_str


def build_full_report(cfg) -> str:
    title = "Faerie Fire — Full Activity Report (all time)"
    return _build_report(cfg, title=title, start=None, end=None, include_timeline=True)


def reports_dir(cfg) -> str:
    from .config import APP_DIR
    path = os.path.join(APP_DIR, "reports")
    os.makedirs(path, exist_ok=True)
    return path


def save_daily_report(cfg) -> tuple[str, str]:
    """Writes reports/daily/YYYY-MM-DD.md, returns (path, markdown)."""
    markdown, date_str = build_daily_report(cfg)
    folder = os.path.join(reports_dir(cfg), "daily")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{date_str}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markdown)
    return path, markdown


def save_full_report(cfg) -> tuple[str, str]:
    """Writes reports/full_report_<timestamp>.md, returns (path, markdown)."""
    markdown = build_full_report(cfg)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(reports_dir(cfg), f"full_report_{stamp}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markdown)
    return path, markdown
