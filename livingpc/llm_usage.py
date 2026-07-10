"""Private-payload-free Claude usage accounting for cost guardrails."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from .db import connect as db_connect
from .diagnostics import DIAG_DIR

DB_PATH = os.path.join(DIAG_DIR, "llm_usage.db")
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}
# Cache pricing multipliers on top of a model's base input rate (see
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
_CACHE_WRITE_5M_MULT = 1.25
_CACHE_WRITE_1H_MULT = 2.0
_CACHE_READ_MULT = 0.1


def _connect():
    os.makedirs(DIAG_DIR, exist_ok=True)
    conn = db_connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage ("
        "id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, local_day TEXT NOT NULL, "
        "category TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL, "
        "output_tokens INTEGER NOT NULL, estimated_usd REAL NOT NULL, elapsed_ms INTEGER NOT NULL)"
    )
    # Added when prompt-caching support was wired in — usage.input_tokens
    # from the API only ever covered tokens AFTER the cache breakpoint, so
    # the cost estimate above was silently ignoring cache writes/reads
    # entirely. Migrate older DBs in place rather than dropping history.
    for column in ("cache_creation_tokens", "cache_read_tokens"):
        try:
            conn.execute(f"ALTER TABLE usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def record_response(category: str, model: str, response, elapsed_seconds: float = 0.0) -> None:
    """Persist counts only. No prompts, completions, labels, or identifiers."""
    try:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        # Split by TTL when available so 1h writes (2x) aren't undercharged
        # relative to 5m writes (1.25x); fall back to treating it all as 5m
        # if the SDK/response doesn't break it down.
        creation_detail = getattr(usage, "cache_creation", None)
        cache_write_5m = int(getattr(creation_detail, "ephemeral_5m_input_tokens", 0) or 0) \
            if creation_detail is not None else cache_creation_tokens
        cache_write_1h = int(getattr(creation_detail, "ephemeral_1h_input_tokens", 0) or 0) \
            if creation_detail is not None else 0
        input_rate, output_rate = PRICES.get(model, (0.0, 0.0))
        cost = (
            input_tokens * input_rate
            + cache_write_5m * input_rate * _CACHE_WRITE_5M_MULT
            + cache_write_1h * input_rate * _CACHE_WRITE_1H_MULT
            + cache_read_tokens * input_rate * _CACHE_READ_MULT
        ) / 1_000_000 + output_tokens * output_rate / 1_000_000
        now_utc = datetime.now(timezone.utc)
        local_day = datetime.now().astimezone().date().isoformat()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO usage (occurred_at,local_day,category,model,input_tokens,"
                "output_tokens,estimated_usd,elapsed_ms,cache_creation_tokens,cache_read_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now_utc.isoformat(), local_day, str(category), str(model), input_tokens,
                 output_tokens, cost, max(0, int(elapsed_seconds * 1000)),
                 cache_creation_tokens, cache_read_tokens),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def daily_summary(day: str | None = None) -> dict:
    day = day or datetime.now().astimezone().date().isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT category,COUNT(*) calls,SUM(input_tokens) input_tokens,"
            "SUM(output_tokens) output_tokens,SUM(estimated_usd) estimated_usd,"
            "SUM(cache_creation_tokens) cache_creation_tokens,"
            "SUM(cache_read_tokens) cache_read_tokens "
            "FROM usage WHERE local_day=? GROUP BY category ORDER BY category", (day,)
        ).fetchall()
        categories = [{k: row[k] for k in row.keys()} for row in rows]
        cache_read = sum(int(r["cache_read_tokens"] or 0) for r in rows)
        cache_creation = sum(int(r["cache_creation_tokens"] or 0) for r in rows)
        # A simple hit-rate signal: of all cache-eligible prefix tokens seen
        # today (writes + reads), what fraction were reads (i.e. reused
        # rather than paid for at the higher write price)?
        cache_eligible = cache_read + cache_creation
        return {
            "day": day,
            "categories": categories,
            "calls": sum(int(r["calls"] or 0) for r in rows),
            "input_tokens": sum(int(r["input_tokens"] or 0) for r in rows),
            "output_tokens": sum(int(r["output_tokens"] or 0) for r in rows),
            "estimated_usd": sum(float(r["estimated_usd"] or 0) for r in rows),
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
            "cache_hit_rate": (cache_read / cache_eligible) if cache_eligible else None,
        }
    finally:
        conn.close()
