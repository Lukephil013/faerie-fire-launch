"""Clarifying questions — when the engine hedges or gets a date wrong, ask
instead of quietly guessing.

Memories written by triage or journal import can end up carrying an
unresolved guess ("family-adjacent figure, possibly a relative or guardian")
when the source text was ambiguous, or a date that's simply implausible (a
memory involving the Xbox dated before 2001; a "vivid personal memory" dated
to when the user was one year old). Left alone, these just sit in memory
looking like settled facts. This module finds them, asks ONE short, curious
question about each, and — once you answer — rewrites the memory to say
what's actually true. That rewrite is a normal supersession, same mechanism
as any other correction; nothing here is special-cased in storage.

Flow (driven by the GUI, mirrors feedback.py's shape):
    scan(mem, store, model)                       -> N clarifications queued
    answer(mem, store, clarification_id, text, model) -> resolves + supersedes
    dismiss(store, clarification_id)              -> closes it, never re-asked
    answer_many(mem, store, ids, text, model)      -> same answer, several at once
    dismiss_many(store, ids)                       -> dismiss several at once

Detection is deterministic (a hedge/anachronism/age/grade scan over decrypted
active memory values), so it costs nothing and can run after every import.
Hedges need the user's testimony and always become a real question — only
those touch a model. A memory with ONLY a date problem (anachronism, age, or
grade) is instead corrected automatically once a birth date is set: a
computed replacement date is applied immediately and the fix is logged
straight into "resolved" (tagged [auto]) rather than left open, since
there's nothing to ask. A memory is only ever flagged once — answered,
auto-resolved, or dismissed, it's never re-surfaced.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from .db import connect as db_connect
from .diagnostics import log_diag
from . import crypto

# --- hedge detection (deterministic, no model call) -------------------------
_HEDGE_PATTERNS = [
    r"\bpossibly\b", r"\bperhaps\b", r"\bpresumably\b", r"\bapparently\b",
    r"\bseemingly\b", r"\bmay have\b", r"\bmight have\b", r"\bmay be\b",
    r"\bmight be\b", r"\bcould be\b", r"\bunclear (?:whether|if)\b",
    r"\bnot clear (?:whether|if)\b", r"\bit's possible that\b",
    r"[a-z]+-adjacent\b", r"\bsome kind of\b", r"\bor similar\b",
    r"\bprobably\b",
]
_HEDGE_RE = re.compile("|".join(_HEDGE_PATTERNS), re.IGNORECASE)


def find_hedges(value: str) -> list[str]:
    """Distinct hedge phrases found in a memory value, in reading order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _HEDGE_RE.finditer(value or ""):
        phrase = m.group(0).lower()
        if phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- timeline plausibility (deterministic, no model call) -------------------
# Meta key holding the user's birth date (YYYY-MM-DD), set once via the GUI.
# Without it, age-implausibility checks simply don't run.
BIRTH_DATE_META_KEY = "birth_date"

# (pattern, display name, earliest plausible US release year). Order matters:
# more specific variants first, with negative lookaheads on the generic form
# so "PS2" isn't also flagged as a bare "PlayStation" match.
CONSOLE_RELEASE_YEARS = [
    (r"\bxbox\s*360\b", "Xbox 360", 2005),
    (r"\bxbox\s*one\b", "Xbox One", 2013),
    (r"\bxbox\s*series\b", "Xbox Series", 2020),
    (r"\bxbox\b(?!\s*(360|one|series))", "Xbox", 2001),
    (r"\bgamecube\b", "GameCube", 2001),
    (r"\bplaystation\s*2\b|\bps2\b", "PlayStation 2", 2000),
    (r"\bplaystation\s*3\b|\bps3\b", "PlayStation 3", 2006),
    (r"\bplaystation\s*4\b|\bps4\b", "PlayStation 4", 2013),
    (r"\bplaystation\s*5\b|\bps5\b", "PlayStation 5", 2020),
    (r"\bplaystation\b(?!\s*[2-5])|\bps1\b|\bpsx\b", "PlayStation", 1995),
    (r"\bnintendo\s*64\b|\bn64\b", "Nintendo 64", 1996),
    (r"\bgame\s*boy\s*color\b", "Game Boy Color", 1998),
    (r"\bgame\s*boy\s*advance\b|\bgba\b", "Game Boy Advance", 2001),
    (r"\bgame\s*boy\b(?!\s*(color|advance))", "Game Boy", 1989),
    (r"\bdreamcast\b", "Dreamcast", 1999),
    (r"\bwii\s*u\b", "Wii U", 2012),
    (r"\bwii\b(?!\s*u)", "Wii", 2006),
    (r"\bswitch\b", "Nintendo Switch", 2017),
    (r"\bsnes\b|\bsuper nintendo\b", "SNES", 1991),
    (r"\bsega genesis\b|\bgenesis\b", "Sega Genesis", 1989),
]


def find_anachronisms(value: str, valid_from: str) -> list[str]:
    """Named consoles/products mentioned in a memory that didn't exist yet as
    of its valid_from date — a strong, objective signal the date is wrong
    (not the content)."""
    if not value or not valid_from or len(valid_from) < 4:
        return []
    try:
        year = int(valid_from[:4])
    except ValueError:
        return []
    flags = []
    for pattern, label, release_year in CONSOLE_RELEASE_YEARS:
        if year < release_year and re.search(pattern, value, re.IGNORECASE):
            flags.append(f"{label} wasn't released until {release_year} — "
                        f"this is dated {year}")
    return flags


def _age_years(birth_date: str, on_date: str) -> float | None:
    from datetime import date as _date
    try:
        by, bm, bd = (int(x) for x in birth_date[:10].split("-"))
        oy, om, od = (int(x) for x in on_date[:10].split("-"))
        return (_date(oy, om, od) - _date(by, bm, bd)).days / 365.25
    except (ValueError, TypeError):
        return None


def find_age_flags(value: str, valid_from: str, birth_date: str | None, *,
                   min_plausible_age: float = 2.0) -> list[str]:
    """Flag a memory dated to an implausibly young age (e.g. a "vivid
    childhood memory" dated to when the user was under a year old). Only
    runs once a birth date is set (BIRTH_DATE_META_KEY); silent otherwise."""
    if not birth_date or not valid_from:
        return []
    age = _age_years(birth_date, valid_from)
    if age is None or age < 0 or age >= min_plausible_age:
        return []
    return [f"you'd have been about {age:.1f} years old on {valid_from[:10]} — "
            f"unusually young for this to be a personal memory"]


# (pattern, display label, typical age range) — catches things like "1st
# grade" dated to when the user would have been 1 year old, which the blunt
# min_plausible_age check above misses once the age is above that floor.
GRADE_AGE_RANGES = [
    (r"\bkindergarten\b", "kindergarten", 4.5, 6.5),
    (r"\b1st\s+grade\b|\bfirst\s+grade\b", "1st grade", 5.5, 7.5),
    (r"\b2nd\s+grade\b|\bsecond\s+grade\b", "2nd grade", 6.5, 8.5),
    (r"\b3rd\s+grade\b|\bthird\s+grade\b", "3rd grade", 7.5, 9.5),
    (r"\b4th\s+grade\b|\bfourth\s+grade\b", "4th grade", 8.5, 10.5),
    (r"\b5th\s+grade\b|\bfifth\s+grade\b", "5th grade", 9.5, 11.5),
    (r"\b6th\s+grade\b|\bsixth\s+grade\b", "6th grade", 10.5, 12.5),
    (r"\b7th\s+grade\b|\bseventh\s+grade\b", "7th grade", 11.5, 13.5),
    (r"\b8th\s+grade\b|\beighth\s+grade\b", "8th grade", 12.5, 14.5),
    (r"\b9th\s+grade\b|\bninth\s+grade\b", "9th grade", 13.5, 15.5),
    (r"\b10th\s+grade\b|\btenth\s+grade\b", "10th grade", 14.5, 16.5),
    (r"\b11th\s+grade\b|\beleventh\s+grade\b", "11th grade", 15.5, 17.5),
    (r"\b12th\s+grade\b|\btwelfth\s+grade\b", "12th grade", 16.5, 18.5),
    (r"\bmiddle school\b", "middle school", 10.5, 13.5),
    (r"\bhigh school\b", "high school", 13.5, 18.5),
]


# Meta key holding the user's own grade -> school-year chart (a JSON dict of
# {grade_label: {"start": "YYYY-09-01", "end": "YYYY-06-30"}}), set once via
# the Clarify tab by pasting something like "Kindergarten  2000-2001  5 years
# old". When set, it replaces the generic age-range guess below with the
# user's own record for a grade — exact instead of approximate, and it works
# even without a birth date.
GRADE_YEAR_MAP_META_KEY = "grade_year_map"

_CHART_YEAR_RANGE_RE = re.compile(r"(\d{4})\s*[‐-―\-]\s*(\d{4})")


def parse_grade_year_chart(text: str) -> dict:
    """Parse a pasted grade/school-year chart, one grade per line, e.g.:
        Kindergarten            2000-2001   5 years old
        1st grade               2001-2002   6 years old
        9th grade / freshman    2009-2010   14 years old
    into {grade_label: {"start": "YYYY-09-01", "end": "YYYY-06-30"}}, using the
    same canonical labels as GRADE_AGE_RANGES. Lines that don't contain both a
    recognizable grade name and a YYYY-YYYY range are skipped, so the whole
    block (headers, blank lines, stray notes) can be pasted as-is."""
    out: dict[str, dict[str, str]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _CHART_YEAR_RANGE_RE.search(line)
        if not m:
            continue
        label_text = line[:m.start()].split("/")[0].strip()
        if not label_text:
            continue
        for pattern, label, _lo, _hi in GRADE_AGE_RANGES:
            if re.search(pattern, label_text, re.IGNORECASE):
                out[label] = {"start": f"{m.group(1)}-09-01",
                              "end": f"{m.group(2)}-06-30"}
                break
    return out


# A real grade-year chart lists individual numbered grades (the way school
# records actually read), not a single "Middle school" or "High school" row.
# A memory phrased generically ("in middle school with Eli") should still
# benefit from that chart, so a composite label's window is synthesized from
# whichever constituent grades ARE present rather than requiring a literal
# chart entry that essentially never exists.
_COMPOSITE_GRADE_GROUPS = {
    "middle school": ["6th grade", "7th grade", "8th grade"],
    "high school": ["9th grade", "10th grade", "11th grade", "12th grade"],
}


def _grade_window(label: str, grade_year_map: dict | None) -> dict | None:
    """The user's school-year window for `label`: a literal chart entry if
    present, else — for a composite label like "middle school" — synthesized
    from whichever of its constituent grades are in the chart. None if
    neither is available."""
    grade_year_map = grade_year_map or {}
    window = grade_year_map.get(label)
    if window:
        return window
    parts = [grade_year_map[g] for g in _COMPOSITE_GRADE_GROUPS.get(label, ())
            if g in grade_year_map]
    if not parts:
        return None
    return {"start": min(p["start"] for p in parts), "end": max(p["end"] for p in parts)}


def _grade_window_mismatch(value: str, valid_from: str,
                           grade_year_map: dict | None):
    """Exact check against the user's own school-year chart, if a grade in it
    is mentioned. Returns (label, window) if the memory's date falls outside
    that grade's actual school year, else None. Needs no birth date."""
    if not grade_year_map or not value or not valid_from:
        return None
    for pattern, label, _lo, _hi in GRADE_AGE_RANGES:
        window = _grade_window(label, grade_year_map)
        if not window or not re.search(pattern, value, re.IGNORECASE):
            continue
        if not (window["start"] <= valid_from <= window["end"]):
            return label, window
    return None


def find_grade_age_flags(value: str, valid_from: str, birth_date: str | None,
                         *, grade_year_map: dict | None = None) -> list[str]:
    """Flag a memory that names a school grade inconsistent with when it was
    actually dated. Prefers the user's own grade_year_map (exact, no birth
    date needed) when the mentioned grade is in it; otherwise falls back to
    the generic age-range heuristic below (e.g. "1st grade" — usually age
    6-7 — dated to when the user would have been 1), which needs a birth date."""
    if not valid_from or not value:
        return []
    exact = _grade_window_mismatch(value, valid_from, grade_year_map)
    if exact:
        label, window = exact
        return [f"{label} was {window['start'][:4]}–{window['end'][:4]} on "
                f"your own record, but this is dated {valid_from[:10]}"]
    if not birth_date:
        return []
    age = _age_years(birth_date, valid_from)
    if age is None or age < 0:
        return []
    flags = []
    for pattern, label, lo, hi in GRADE_AGE_RANGES:
        if _grade_window(label, grade_year_map):
            continue   # already checked exactly above; don't double-flag
        if re.search(pattern, value, re.IGNORECASE) and not (lo <= age <= hi):
            flags.append(f"{label} is usually age {lo:.0f}-{hi:.0f}, but "
                        f"you'd have been about {age:.1f} on {valid_from[:10]}")
    return flags


# --- auto-resolution for pure date problems ---------------------------------
# Hedge flags need the user's testimony (only they know who Rickey is); date
# flags (anachronism/age/grade) are objective — once a birth date is set, a
# corrected date can be computed without asking anything. scan() uses these
# to fix pure date issues immediately and silently; a memory that ALSO hedges
# still gets a real question, since content is on the table too.
def _date_after_years(birth_date: str, years: float) -> str | None:
    """birth_date + `years` (approximate, calendar-year granularity), as an
    ISO date string using the birth month/day for a concrete-looking date."""
    from datetime import date as _date
    try:
        by, bm, bd = (int(x) for x in birth_date[:10].split("-"))
    except (ValueError, TypeError):
        return None
    target_year = by + int(years // 1)
    try:
        return _date(target_year, bm, bd).isoformat()
    except ValueError:   # Feb 29 landing on a non-leap year, etc.
        return _date(target_year, bm, min(bd, 28)).isoformat()


def _suggest_anachronism_year(value: str, valid_from: str) -> int | None:
    """Only consider consoles actually mismatched against valid_from (mirrors
    find_anachronisms) — a console that already existed by then shouldn't
    pull the suggestion around just because it's also mentioned."""
    if not valid_from or len(valid_from) < 4:
        return None
    try:
        year = int(valid_from[:4])
    except ValueError:
        return None
    years = [release_year for pattern, _label, release_year in CONSOLE_RELEASE_YEARS
            if year < release_year and re.search(pattern, value, re.IGNORECASE)]
    return max(years) if years else None


def suggest_corrected_date(value: str, valid_from: str, birth_date: str | None,
                           min_plausible_age: float = 2.0, *,
                           grade_year_map: dict | None = None) -> str | None:
    """The best computable replacement date for a memory that only has date
    flags (no hedge).

    An exact match against the user's own grade-year chart is authoritative
    and wins outright — the memory is corrected straight to that grade's
    charted start, whichever direction that moves the date. This matters
    because it's the one case allowed to move a date EARLIER: an old
    autobiography or journal entry commonly gets dated by when it was
    WRITTEN (e.g. 2021) rather than the year the described event actually
    happened (e.g. 6th grade, 2006) — once the user's chart says exactly
    when that grade was, there's no ambiguity left to be cautious about.

    Without an exact chart match, falls back to more conservative heuristics
    — a mentioned product's release year, birth_date + a generic grade's
    age-range midpoint, or birth_date + min_plausible_age — and only accepts
    one of those if it moves the date LATER, since they're guesses and
    correcting a guess backward in time risks overcorrecting in the wrong
    direction."""
    exact = _grade_window_mismatch(value, valid_from, grade_year_map)
    if exact:
        _label, window = exact
        return window["start"]

    candidates = []
    year = _suggest_anachronism_year(value, valid_from)
    if year is not None:
        candidates.append(f"{year}-01-01")
    if birth_date:
        midpoints = [(lo + hi) / 2 for pattern, _label, lo, hi in GRADE_AGE_RANGES
                    if re.search(pattern, value or "", re.IGNORECASE)]
        if midpoints:
            heuristic_grade_target = _date_after_years(birth_date, max(midpoints))
            if heuristic_grade_target:
                candidates.append(heuristic_grade_target)
        bare_target = _date_after_years(birth_date, min_plausible_age)
        if bare_target:
            candidates.append(bare_target)
    if not candidates:
        return None
    best = max(candidates)
    return best if (not valid_from or best > valid_from) else None


# --- storage -----------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS clarification (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id            INTEGER,
    category             TEXT,
    attribute            TEXT,
    hedges               TEXT,             -- JSON list of matched hedge phrases
    question             TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',   -- open | answered | dismissed
    answer               TEXT,
    resulting_memory_id  INTEGER,
    created_at           TEXT,
    resolved_at          TEXT,
    CHECK (status IN ('open', 'answered', 'dismissed')),
    FOREIGN KEY (memory_id) REFERENCES memory(id)
);
CREATE INDEX IF NOT EXISTS idx_clar_status ON clarification(status);
CREATE INDEX IF NOT EXISTS idx_clar_memory ON clarification(memory_id);
"""


class ClarifyStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- writes -------------------------------------------------------------
    def add(self, memory_id: int | None, category: str, attribute: str,
           hedges: list[str], question: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO clarification (memory_id, category, attribute, hedges, "
            "question, status, created_at) VALUES (?,?,?,?,?,'open',?)",
            (memory_id, category, attribute, json.dumps(hedges),
             crypto.enc(question), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_answered(self, clarification_id: int, answer: str,
                      resulting_memory_id: int | None) -> None:
        self.conn.execute(
            "UPDATE clarification SET status='answered', answer=?, "
            "resulting_memory_id=?, resolved_at=? WHERE id=?",
            (crypto.enc(answer), resulting_memory_id, _now(), clarification_id),
        )
        self.conn.commit()

    def dismiss(self, clarification_id: int) -> None:
        self.conn.execute(
            "UPDATE clarification SET status='dismissed', resolved_at=? WHERE id=?",
            (_now(), clarification_id),
        )
        self.conn.commit()

    # --- reads ----------------------------------------------------------------
    def get(self, clarification_id: int):
        return self.conn.execute(
            "SELECT * FROM clarification WHERE id=?", (clarification_id,)).fetchone()

    def _dict(self, r) -> dict:
        return {
            "id": r["id"], "memory_id": r["memory_id"], "category": r["category"],
            "attribute": r["attribute"], "hedges": json.loads(r["hedges"] or "[]"),
            "question": crypto.dec(r["question"]), "status": r["status"],
            "answer": crypto.dec(r["answer"]),
            "resulting_memory_id": r["resulting_memory_id"],
            "created_at": r["created_at"], "resolved_at": r["resolved_at"],
        }

    def open_items(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM clarification WHERE status='open' ORDER BY id").fetchall()
        return [self._dict(r) for r in rows]

    def resolved(self, limit: int = 25) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM clarification WHERE status != 'open' "
            "ORDER BY resolved_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._dict(r) for r in rows]

    def covered_memory_ids(self) -> set[int]:
        """Memories that have already been asked about (any status) — a
        memory is only ever flagged once, so answering or dismissing it is
        final and it never comes back for the same hedge."""
        rows = self.conn.execute(
            "SELECT memory_id FROM clarification WHERE memory_id IS NOT NULL").fetchall()
        return {r["memory_id"] for r in rows}

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM clarification GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def close(self) -> None:
        self.conn.close()


# --- models: phrase the question, then fold the answer back in --------------
QUESTION_SYSTEM = """\
You are the clarification stage of a personal "second brain" memory system.
The engine flagged one of two problems with a memory about the user:
(1) it hedged on a detail it wasn't actually sure about (e.g. "possibly a
relative", "family-adjacent figure"), or (2) the date attached to the memory
looks wrong — it mentions something that didn't exist yet, or implies an
implausibly young age for a personal memory. Ask ONE short, specific,
genuinely curious question that would resolve exactly the flagged issue —
tied to the actual flag and content, not a generic question. If the flag is
about a date, ask what year/age it actually was, not about the content.

Return STRICT JSON only: {"question": str}
"""

RESOLVE_SYSTEM = """\
You are the clarification-resolution stage of a personal "second brain". The
memory below was flagged (a hedge, an anachronism, or an implausible age);
the engine asked the user about it and they answered. Rewrite the memory
value as a clean, confident statement that incorporates their answer and
removes the flagged issue entirely. The user is the authority — take their
answer at face value, don't add anything they didn't say, and keep everything
else about the memory that wasn't in question.

If — and only if — their answer corrects WHEN the event happened (a
different year or age than originally dated), also return that as
revised_date in YYYY-MM-DD form (use YYYY-01-01 if only a year is known).
Otherwise revised_date must be null — most answers only correct the content,
not the date.

Return STRICT JSON only: {"revised_value": str, "revised_date": str|null}
"""


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.DOTALL)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


class StubClarifyModel:
    """Offline/deterministic — lets the whole flow run and be tested."""

    def question(self, memory: dict, hedges: list[str]) -> str:
        hedge = hedges[0] if hedges else "that detail"
        return (f'You wrote "{hedge}" for {memory.get("attribute") or "this"} — '
                f"what's actually true here?")

    def resolve(self, memory: dict, question: str, answer: str) -> dict:
        base = _HEDGE_RE.sub("", memory.get("value") or "").strip()
        base = re.sub(r"\s{2,}", " ", base).strip(" ,.")
        answer = (answer or "").strip()
        revised_value = f"{base}. {answer}".strip(". ").strip() if base else answer
        year_match = _YEAR_RE.search(answer)
        revised_date = f"{year_match.group(1)}-01-01" if year_match else None
        return {"revised_value": revised_value, "revised_date": revised_date}


class ClaudeClarifyModel:
    """Anthropic-backed; small/cheap calls — one per new hedge, one per answer."""

    def __init__(self, model: str = "claude-haiku-4-5", api_key: str | None = None,
                max_tokens: int = 400, timeout_seconds: float = 60.0):
        self.model = model
        self.max_tokens = max_tokens
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use clarify_backend=stub.")
        from anthropic import Anthropic  # lazy import
        self._client = Anthropic(api_key=key, timeout=timeout_seconds)

    def _call(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def question(self, memory: dict, hedges: list[str]) -> str:
        prompt = (f"CATEGORY: {memory.get('category')}\n"
                  f"ATTRIBUTE: {memory.get('attribute')}\n"
                  f"VALUE: {memory.get('value')}\n"
                  f"HEDGE PHRASE(S): {', '.join(hedges)}")
        log_diag("prompt", f"surface=clarify-question model={self.model} "
                 f"category={memory.get('category')}")
        data = _extract_json(self._call(QUESTION_SYSTEM, prompt))
        question = str(data.get("question") or "").strip()
        return question or f'What did you mean by "{hedges[0]}" here?'

    def resolve(self, memory: dict, question: str, answer: str) -> dict:
        prompt = (f"ORIGINAL VALUE: {memory.get('value')}\n"
                  f"QUESTION ASKED: {question}\nUSER'S ANSWER: {answer}")
        log_diag("prompt", f"surface=clarify-resolve model={self.model} "
                 f"category={memory.get('category')}")
        data = _extract_json(self._call(RESOLVE_SYSTEM, prompt))
        revised_value = str(data.get("revised_value") or "").strip() or answer
        revised_date = data.get("revised_date")
        revised_date = str(revised_date).strip() if revised_date else None
        return {"revised_value": revised_value, "revised_date": revised_date or None}


def get_clarify_model(config):
    backend = (getattr(config, "clarify_backend", "") or
              getattr(config, "inference_backend", "claude")).lower()
    if backend == "stub":
        return StubClarifyModel()
    return ClaudeClarifyModel(
        model=getattr(config, "clarify_model", "claude-haiku-4-5"),
        timeout_seconds=getattr(config, "llm_timeout_seconds", 60.0))


# --- the flow the GUI drives -------------------------------------------------
def _load_grade_year_map(mem) -> dict:
    try:
        return json.loads(mem.get_meta(GRADE_YEAR_MAP_META_KEY) or "{}")
    except (TypeError, ValueError):
        return {}


def set_grade_year_chart(mem, text: str) -> int:
    """Parse and store the user's real grade/school-year chart (Clarify tab,
    e.g. pasted straight from a records site) — exact ground truth used
    instead of the generic age-range guess wherever a grade in it is
    mentioned. Empty text clears it. Returns how many grades were recognized."""
    parsed = parse_grade_year_chart(text) if (text or "").strip() else {}
    mem.set_meta(GRADE_YEAR_MAP_META_KEY, json.dumps(parsed))
    return len(parsed)


def _auto_resolve_date(mem, store: ClarifyStore, row: dict, date_flags: list[str],
                       corrected_date: str) -> None:
    old_date = row["valid_from"]
    new_id = mem.supersede(row["id"], row["value"], as_of=corrected_date,
                          source_refs=[{"clarify_auto": True}])
    note = ("Date corrected automatically — " + "; ".join(date_flags) +
           f" (moved {old_date} → {corrected_date}).")
    cid = store.add(row["id"], row["category"], row["attribute"], date_flags,
                    f"[auto] {note}")
    store.mark_answered(cid, note, new_id)


def recheck_open_date_clarifications(mem, store: ClarifyStore, *,
                                     min_plausible_age: float = 2.0) -> int:
    """Re-evaluate every OPEN clarification that carries only date flags (no
    hedge — the content was never in question, just when it happened)
    against the CURRENT birth date and grade-year chart. Call this right
    after either changes, so saving a better record resolves questions that
    were only ever wrong because the data wasn't there yet — instead of
    leaving a stale card sitting in the tab until the user manually dismisses
    it (scan() itself only looks at memories not yet queued, so it wouldn't
    otherwise touch an already-open item).

    A clarification whose flags include a hedge is left alone; that always
    needs the user's own testimony about content, and a chart/birth-date
    change can't resolve it. Returns how many open items were resolved
    (either found to already be correct, or auto-corrected to a computed
    date)."""
    from . import crypto
    birth_date = mem.get_meta(BIRTH_DATE_META_KEY)
    grade_year_map = _load_grade_year_map(mem)
    resolved = 0
    for item in store.open_items():
        if item["memory_id"] is None:
            continue
        row = mem.get(item["memory_id"])
        if row is None:
            continue
        value = crypto.dec(row["value"])
        if find_hedges(value):
            continue   # still a real question about content, not just a date
        valid_from = row["valid_from"]
        date_flags = find_anachronisms(value, valid_from)
        date_flags += find_age_flags(value, valid_from, birth_date,
                                     min_plausible_age=min_plausible_age)
        date_flags += find_grade_age_flags(value, valid_from, birth_date,
                                           grade_year_map=grade_year_map)
        if not date_flags:
            store.mark_answered(
                item["id"],
                "[auto] no longer flagged — your saved records show this "
                "date is correct.", None)
            resolved += 1
            continue
        corrected = suggest_corrected_date(value, valid_from, birth_date,
                                           min_plausible_age,
                                           grade_year_map=grade_year_map)
        if corrected:
            new_id = mem.supersede(row["id"], value, as_of=corrected,
                                   source_refs=[{"clarify_auto_recheck": True}])
            note = ("Date corrected automatically (rechecked against your "
                    "saved records) — " + "; ".join(date_flags) +
                    f" (moved {valid_from} → {corrected}).")
            store.mark_answered(item["id"], note, new_id)
            resolved += 1
    log_diag("clarify", f"recheck resolved={resolved}")
    return resolved


def scan(mem, store: ClarifyStore, model, *, limit: int = 20,
        min_plausible_age: float = 2.0) -> int:
    """Find active memories that haven't been asked about yet and either (a)
    hedge on a detail, (b) mention a product that didn't exist yet as of
    their date, (c) are dated to an implausibly young age, or (d) name a
    school grade inconsistent with that age (e.g. "1st grade" dated to when
    the user would have been 1). Hedges need the user's testimony and always
    become a real question; a memory with ONLY date flags is instead
    corrected automatically — a computed replacement date is applied right
    away and the fix lands straight in "resolved" (tagged [auto]) since
    there's nothing to ask. All detection is free (regex/arithmetic); a model
    call only happens for something that actually needs the user's answer,
    capped by `limit` per run so a big backlog doesn't fire dozens of calls at
    once. Returns the number of clarifications queued (open + auto-resolved)."""
    covered = store.covered_memory_ids()
    birth_date = mem.get_meta(BIRTH_DATE_META_KEY)
    grade_year_map = _load_grade_year_map(mem)
    created = 0
    for row in mem.active_as_dicts():
        if created >= limit:
            break
        if row["id"] in covered:
            continue
        hedges = find_hedges(row["value"])
        date_flags = find_anachronisms(row["value"], row["valid_from"])
        date_flags += find_age_flags(row["value"], row["valid_from"], birth_date,
                                     min_plausible_age=min_plausible_age)
        date_flags += find_grade_age_flags(row["value"], row["valid_from"], birth_date,
                                           grade_year_map=grade_year_map)
        if not hedges and not date_flags:
            continue
        if not hedges:
            corrected = suggest_corrected_date(row["value"], row["valid_from"],
                                               birth_date, min_plausible_age,
                                               grade_year_map=grade_year_map)
            if corrected:
                _auto_resolve_date(mem, store, row, date_flags, corrected)
                created += 1
                continue
        all_flags = hedges + date_flags
        question = model.question(row, all_flags)
        store.add(row["id"], row["category"], row["attribute"], all_flags, question)
        created += 1
    log_diag("clarify", f"scan queued={created}")
    return created


def answer(mem, store: ClarifyStore, clarification_id: int, text: str, model) -> dict:
    """The user answered a clarifying question: rewrite the underlying memory
    (a normal supersession) and close the clarification out. Returns
    {"resulting_memory_id": int|None}."""
    row = store.get(clarification_id)
    if row is None:
        raise ValueError(f"clarification {clarification_id} not found")
    item = store._dict(row)
    if item["status"] != "open":
        raise ValueError(f"clarification {clarification_id} is already {item['status']}")
    text = (text or "").strip()
    if not text:
        raise ValueError("answer text is empty")

    new_memory_id = None
    if item["memory_id"] is not None:
        current = mem.get(item["memory_id"])
        if current is not None:
            from . import crypto
            memory_dict = {"category": current["category"],
                          "attribute": current["attribute"],
                          "value": crypto.dec(current["value"])}
            resolution = model.resolve(memory_dict, item["question"], text)
            revised_value = (resolution.get("revised_value") or "").strip()
            # Default to the memory's ORIGINAL date, not today — most answers
            # correct the content, not when it happened, and the corrected
            # fact belongs at its original place on the timeline. Only a
            # date-correcting answer (resolution gives revised_date) moves it.
            as_of = resolution.get("revised_date") or current["valid_from"]
            if revised_value:
                new_memory_id = mem.supersede(
                    item["memory_id"], revised_value, as_of=as_of,
                    source_refs=[{"clarification": clarification_id}])
    store.mark_answered(clarification_id, text, new_memory_id)
    log_diag("clarify", f"answered id={clarification_id} "
             f"resulting_memory={new_memory_id}")
    return {"resulting_memory_id": new_memory_id}


def dismiss(store: ClarifyStore, clarification_id: int) -> None:
    row = store.get(clarification_id)
    if row is None:
        raise ValueError(f"clarification {clarification_id} not found")
    store.dismiss(clarification_id)
    log_diag("clarify", f"dismissed id={clarification_id}")


# --- bulk actions (Clarify tab: select several, act on them at once) --------
def dismiss_many(store: ClarifyStore, ids: list[int]) -> dict:
    """Bulk dismiss — best-effort per id; a bad id is reported, not raised,
    so it doesn't abort the rest of the batch."""
    ok, errors = [], []
    for cid in ids:
        try:
            dismiss(store, int(cid))
            ok.append(int(cid))
        except ValueError as error:
            errors.append({"id": int(cid), "message": str(error)})
    return {"dismissed": ok, "errors": errors}


def answer_many(mem, store: ClarifyStore, ids: list[int], text: str, model) -> dict:
    """Bulk answer — apply the SAME answer text to several open clarifications
    at once (handy for a cluster of near-identical hedges from one import).
    Best-effort per id; failures are reported, not raised."""
    ok, errors = [], []
    for cid in ids:
        try:
            result = answer(mem, store, int(cid), text, model)
            ok.append({"id": int(cid), "resulting_memory_id": result["resulting_memory_id"]})
        except ValueError as error:
            errors.append({"id": int(cid), "message": str(error)})
    return {"answered": ok, "errors": errors}
