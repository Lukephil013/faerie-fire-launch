"""Turn a day of raw events into a compact, per-app summary for triage.

The summary is the only thing (after redaction) that goes to the LLM, so it
should be information-dense but small: per app, how long, which windows, and a
deduped sample of the OCR text seen.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta

from ..storage import EventLog
from .. import crypto


_INTERNAL_PYTHON_APPS = {"python.exe", "pythonw.exe"}


def is_internal_ui(app: str | None, window_title: str | None) -> bool:
    """Return true for Faerie Fire's own Python desktop windows.

    Review UI OCR is a rendered, possibly clipped copy of data already stored in
    memory. Feeding it back into triage creates self-referential proposals and can
    make complete answers look truncated.
    """
    normalized_app = (app or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    title = (window_title or "").strip().lower()
    return normalized_app in _INTERNAL_PYTHON_APPS and title.startswith("faerie fire")


def day_bounds(date_str: str) -> tuple[str, str]:
    """Return [start, end) ISO timestamps spanning the local calendar day."""
    start = datetime.fromisoformat(date_str + "T00:00:00")
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _session_minutes(rows) -> float:
    total = 0.0
    for r in rows:
        if r["start_ts"] and r["end_ts"]:
            try:
                a = datetime.fromisoformat(r["start_ts"])
                b = datetime.fromisoformat(r["end_ts"])
                total += (b - a).total_seconds()
            except ValueError:
                pass
    return total / 60.0


def _dedup_lines(texts, cap_chars: int) -> str:
    """Merge OCR payloads, drop duplicate lines, cap total length."""
    seen: "OrderedDict[str, None]" = OrderedDict()
    for t in texts:
        for line in (t or "").split("\n"):
            line = line.strip()
            if len(line) < 3:
                continue
            if line not in seen:
                seen[line] = None
    joined = " | ".join(seen.keys())
    return joined[:cap_chars]


def build_summary(store: EventLog, start: str, end: str, title: str,
                  cap_per_app: int = 1200) -> str:
    """Per-app activity summary for the time window [start, end)."""
    conn = store.conn

    sessions = conn.execute(
        "SELECT app, window_title, start_ts, end_ts FROM sessions "
        "WHERE start_ts >= ? AND start_ts < ?",
        (start, end),
    ).fetchall()

    ocr_rows = conn.execute(
        "SELECT app, window_title, text_payload FROM events "
        "WHERE type IN ('ocr', 'browser', 'clipboard') AND ts >= ? AND ts < ? "
        "AND text_payload IS NOT NULL AND text_payload != '' "
        "ORDER BY ts",
        (start, end),
    ).fetchall()

    if not sessions and not ocr_rows:
        return f"# {title}\n\n(No activity recorded.)"

    by_app_sessions = defaultdict(list)
    for s in sessions:
        title_text = crypto.dec(s["window_title"])
        if is_internal_ui(s["app"], title_text):
            continue
        by_app_sessions[s["app"] or "(unknown)"].append(s)

    by_app_titles = defaultdict(set)
    by_app_text = defaultdict(list)
    for r in ocr_rows:
        app = r["app"] or "(unknown)"
        title_text = crypto.dec(r["window_title"])
        if is_internal_ui(r["app"], title_text):
            continue
        if title_text:
            by_app_titles[app].add(title_text)
        by_app_text[app].append(crypto.dec(r["text_payload"]))

    apps = sorted(
        set(by_app_sessions) | set(by_app_text),
        key=lambda a: _session_minutes(by_app_sessions.get(a, [])),
        reverse=True,
    )

    lines = [f"# {title}", ""]
    for app in apps:
        mins = _session_minutes(by_app_sessions.get(app, []))
        n_sessions = len(by_app_sessions.get(app, []))
        lines.append(f"## {app}  —  {n_sessions} session(s), ~{mins:.0f} min")
        titles = sorted(by_app_titles.get(app, []))
        if titles:
            lines.append("Windows: " + "; ".join(titles[:8]))
        text = _dedup_lines(by_app_text.get(app, []), cap_per_app)
        if text:
            lines.append("On-screen text: " + text)
        lines.append("")

    return "\n".join(lines).strip()


def build_day_summary(store: EventLog, date_str: str, cap_per_app: int = 1200) -> str:
    """Whole-day summary (used for a specific past --date)."""
    start, end = day_bounds(date_str)
    return build_summary(store, start, end,
                         f"Activity summary for {date_str}", cap_per_app)
