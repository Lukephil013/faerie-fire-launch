"""Chronological journal backfill — Notion (or any) journals -> the memory graph.

Journals are the highest-signal data the second brain can get: self-reported
values, fears, motivations, and goals that capture can only guess at from
behaviour. But a raw dump would poison memory — hundreds of stale "facts"
stamped with today's date, out-of-order supersessions, no trajectory. So this
module ingests them the way the design demands: chronologically, in monthly
batches, with facts dated by the entry that evidences them.

Input: a folder of markdown/text files (default data/notion/, gitignored).
Each file may start with a `---` front-matter block (title, default_year,
exported_at) and contains entries delimited by date-marker lines ("06/16",
"6/8", "04/05/2026"). Notion's native export can be dropped in unchanged.

Pipeline per monthly batch, oldest month first:
    entries -> redact() -> model proposes dated facts (strict JSON)
    -> per fact: duplicate? skip · updates an existing fact? supersede(as_of=date)
       · older than what we know? skip · else add(valid_from=date)
A watermark (meta key `journal_import_watermark`) makes re-runs resume where
the last one stopped; undated preamble text is processed last, dated by the
file's exported_at. Run consolidation after a large import.

Backends mirror triage: ClaudeJournalModel (cloud) and StubJournalModel
(offline, deterministic, for tests and dry runs).
"""
from __future__ import annotations

import json
import os
import re

from .consolidate import _is_duplicate, _norm
from .diagnostics import log_diag
from .journal_filter import filter_entries
from .memory import MemoryStore
from .memory_context import estimate_tokens, select_memories
from .triage.redact import redact

WATERMARK_KEY = "journal_import_watermark"
IMPORTED_FILES_KEY = "journal_imported_files"
UNDATED_BATCH = "undated"

# a line that is (only) a date marker: 06/16, 6/8, 04/05/2026, 06/29/29
_DATE_LINE = re.compile(
    r"^\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*(\d{2,4}))?\s*$")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# --- parsing ---------------------------------------------------------------
def parse_front_matter(text: str) -> tuple[dict, str]:
    """Return ({key: value}, body). Missing front matter -> ({}, text)."""
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end():]


def _entry_date(month: int, day: int, year_part: str | None,
                default_year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = default_year
    if year_part:
        if len(year_part) == 4:
            year = int(year_part)   # explicit 4-digit year is always trusted
        elif len(year_part) == 2 and int(year_part) >= 20:
            year = 2000 + int(year_part)
            if year > default_year + 1:
                year = default_year   # degenerate "06/29/29" -> not 2029
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_entries(body: str, default_year: int) -> list[dict]:
    """Split a journal body into [{date: 'YYYY-MM-DD'|None, text}].

    Text before the first date marker becomes one undated entry (standing
    notes). Date markers themselves are consumed.
    """
    entries: list[dict] = []
    current_date: str | None = None
    lines: list[str] = []

    def flush():
        text = "\n".join(lines).strip()
        if text:
            entries.append({"date": current_date, "text": text})
        lines.clear()

    for line in body.splitlines():
        match = _DATE_LINE.match(line)
        if match:
            date = _entry_date(int(match.group(1)), int(match.group(2)),
                               match.group(3), default_year)
            if date:
                flush()
                current_date = date
                continue
        lines.append(line)
    flush()
    return entries


def load_journals(journal_dir: str) -> list[dict]:
    """All .md/.txt files -> [{source, exported_at, entries}], name order."""
    journals = []
    if not os.path.isdir(journal_dir):
        return journals
    for name in sorted(os.listdir(journal_dir)):
        if not name.lower().endswith((".md", ".txt")):
            continue
        path = os.path.join(journal_dir, name)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            meta, body = parse_front_matter(f.read())
        default_year = int(meta.get("default_year") or 2000)
        journals.append({
            "source": meta.get("title") or os.path.splitext(name)[0],
            "file": name,
            "exported_at": meta.get("exported_at") or "",
            "entries": parse_entries(body, default_year),
        })
    return journals


# --- date sanity checks (surfaced in the Import tab + CLI before commit) ----
_PROSE_DATE = re.compile(
    r"(?:written|updated|added)\s+(?:on\s+)?\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}",
    re.IGNORECASE)


def validate_dates(journal: dict, *, today: str | None = None) -> list[str]:
    """Warnings for one loaded journal (from load_journals). Empty = clean.

    Catches the ways dates silently go wrong: a missing/implausible
    default_year, a year rollover inside one file (Dec -> Jan with bare MM/DD
    markers goes BACKWARD in time), dates in the future, files that are mostly
    undated (facts would be stamped with the export date), and prose dates the
    parser can't use ("Written on 05/27/21" mid-sentence).
    """
    from datetime import date as _d
    today = today or _d.today().isoformat()
    warnings: list[str] = []
    entries = journal.get("entries") or []
    dated = [e for e in entries if e.get("date")]

    if not journal.get("exported_at"):
        warnings.append("no front matter — default_year fell back to 2000; "
                        "add a header or re-drop with the right year")
    if dated:
        years = {e["date"][:4] for e in dated}
        if "2000" in years:
            warnings.append("entries dated year 2000 — default_year is missing "
                            "or wrong in the header")
        future = [e["date"] for e in dated if e["date"] > today]
        if future:
            warnings.append(f"{len(future)} entrie(s) dated in the future "
                            f"(first: {future[0]}) — check the year")
        # rollover: a later entry in the file jumping back > 300 days usually
        # means Dec -> Jan crossed a year with bare MM/DD markers
        prev = None
        for e in dated:
            if prev:
                gap = abs((int(prev[:4]) - int(e["date"][:4])) * 365
                          + (int(prev[5:7]) - int(e["date"][5:7])) * 30)
                if 300 < gap < 430:   # ~11-14 months apart within one file
                    warnings.append(
                        f"possible year rollover: {prev} sits next to "
                        f"{e['date']} in the same file — if it crosses New "
                        "Year, use explicit years (01/05/2026) or split it")
                    break
            prev = e["date"]
    if entries and len(dated) < len(entries) / 2:
        warnings.append(f"{len(entries) - len(dated)} of {len(entries)} "
                        "entrie(s) undated — their facts get stamped with the "
                        "export date, not history")
    undated_text = " ".join(e["text"] for e in entries if not e.get("date"))
    if _PROSE_DATE.search(undated_text):
        warnings.append("contains prose dates (\"written on …\") the parser "
                        "can't use — put them on their own line as MM/DD/YYYY")
    return warnings


# --- per-file import tracking (drives the ✓ imported badge in the GUI) ------
def _file_hash(path: str) -> str:
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def record_imported_files(mem: MemoryStore, journal_dir: str) -> None:
    """After a full (non-dry, non-single-month) import: remember each staged
    file's content hash, so the GUI can mark what's been committed."""
    from datetime import date
    try:
        existing = json.loads(mem.get_meta(IMPORTED_FILES_KEY) or "{}")
    except ValueError:
        existing = {}
    for name in sorted(os.listdir(journal_dir)):
        if name.lower().endswith((".md", ".txt")):
            existing[name] = {"hash": _file_hash(os.path.join(journal_dir, name)),
                              "at": date.today().isoformat()}
    mem.set_meta(IMPORTED_FILES_KEY, json.dumps(existing))


def imported_file_status(mem: MemoryStore, journal_dir: str) -> dict:
    """{filename: 'imported'|'changed'|'new'} for everything staged."""
    try:
        record = json.loads(mem.get_meta(IMPORTED_FILES_KEY) or "{}")
    except ValueError:
        record = {}
    status = {}
    for name in sorted(os.listdir(journal_dir)) if os.path.isdir(journal_dir) else []:
        if not name.lower().endswith((".md", ".txt")):
            continue
        known = record.get(name)
        if not known:
            status[name] = "new"
        elif known.get("hash") == _file_hash(os.path.join(journal_dir, name)):
            status[name] = "imported"
        else:
            status[name] = "changed"
    return status


def batch_by_month(journals: list[dict]) -> list[tuple[str, list[dict]]]:
    """[('YYYY-MM', [entry+source...]) oldest first, then ('undated', [...])].

    Undated entries are standing notes; they run LAST (dated by exported_at)
    so dated history builds the trajectory before current-state notes land.
    """
    months: dict[str, list[dict]] = {}
    undated: list[dict] = []
    for journal in journals:
        for entry in journal["entries"]:
            item = {**entry, "source": journal["source"],
                    "exported_at": journal["exported_at"]}
            if entry["date"]:
                months.setdefault(entry["date"][:7], []).append(item)
            else:
                undated.append(item)
    batches = [(month, sorted(items, key=lambda e: e["date"]))
               for month, items in sorted(months.items())]
    if undated:
        batches.append((UNDATED_BATCH, undated))
    return batches


# --- models ------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are the journal-import engine of a personal "second brain". You receive one
month of the user's private journal entries (each with a date and source
journal), plus a catalog of facts already believed about them. Journals are
self-reported inner life — treat them as the most authoritative signal there is
about values, fears, motivations, goals, relationships, health patterns, and
projects.

Extract every DURABLE fact the text genuinely supports. Return STRICT JSON only:
{ "statements": [ {"category": str, "attribute": str, "value": str,
                    "confidence": 0-1, "date": "YYYY-MM-DD"} ] }

Rules:
- Favor identity, values, motivations, fears, goals, relationships, formative
  events, and trajectories over logistics. Never pad with weak facts — but
  never compress distinct facts into one either. A thin journal month may
  yield 2-5 facts; a dense retrospective document (an autobiography, a life
  history, an annual review) supports MANY distinct facts — extract them all,
  one per distinct person, period, pattern, or insight.
- "date" is the entry date that best evidences the fact — never invent dates.
- category: short lowercase noun (identity, values, fears, goals, health,
  relationships, projects, work, gaming...). attribute: a stable key phrase.
  value: 1-2 plain sentences about the user.
- If a fact updates something in the existing catalog, state the NEW version
  plainly (the importer handles supersession); do not restate unchanged facts.
- Journal text may be raw and emotionally intense. Summarize insight
  non-judgmentally in the user's own framing; never editorialize or diagnose.
"""

DEEP_ADDENDUM = """

DEEP RETROSPECTIVE MODE: this text is one section of a dense retrospective
document (autobiography / life history / annual review). Extract EXHAUSTIVELY —
every distinct durable fact, often 10-20 per section: each person, place,
period, formative event, pattern, and insight gets its own statement.
"date" here means when the fact BECAME TRUE (event time), when the text
supports it: explicit dates, ages combined with a stated birth year, or named
life periods. Use YYYY-01-01 when only the year is inferable. If event time is
genuinely unclear, omit "date" and the document's writing date will be used.
Never date anything after the writing date, and never fabricate precision.
"""


def _format_batch(month: str, entries: list[dict], max_chars: int) -> list[str]:
    """Redacted, chunked text blocks for one monthly batch."""
    blocks, current, size = [], [], 0
    for entry in entries:
        piece = (f"[{entry['date'] or 'undated'} · {entry['source']}]\n"
                 f"{redact(entry['text'])}\n")
        if current and size + len(piece) > max_chars:
            blocks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        blocks.append("".join(current))
    return blocks


def _parse_statements(text: str) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return []
    statements = data.get("statements") or []
    return [s for s in statements if isinstance(s, dict)
            and s.get("category") and s.get("attribute") and s.get("value")]


class StubJournalModel:
    """Offline/deterministic: one low-effort fact per dated entry. Lets the
    whole import pipeline run and be tested with no API key."""

    def propose(self, month: str, block: str, memory_catalog: list[dict],
                deep: bool = False) -> list[dict]:
        statements = []
        for match in re.finditer(r"\[(\d{4}-\d{2}-\d{2}) · ([^\]]+)\]\n(.+?)(?=\n\[|\Z)",
                                 block, re.DOTALL):
            date, source, text = match.groups()
            first = " ".join(text.strip().split())[:80]
            statements.append({
                "category": "journal-stub", "attribute": f"{source} {date}",
                "value": first, "confidence": 0.9, "date": date,
            })
        return statements


class ClaudeJournalModel:
    """Cloud model. Only redacted journal text + the memory catalog are sent."""

    def __init__(self, model: str, *, timeout_seconds: float = 120.0,
                 max_tokens: int = 4000, memory_max_items: int = 30,
                 memory_max_chars: int = 1600, api_key: str | None = None):
        self.model = model
        self.max_tokens = max_tokens
        self.memory_max_items = memory_max_items
        self.memory_max_chars = memory_max_chars
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use --backend stub.")
        from anthropic import Anthropic  # lazy import
        self._client = Anthropic(api_key=key, timeout=timeout_seconds)

    def propose(self, month: str, block: str, memory_catalog: list[dict],
                deep: bool = False) -> list[dict]:
        selection = select_memories(
            memory_catalog, block,
            max_items=self.memory_max_items, max_chars=self.memory_max_chars)
        catalog = "\n".join(
            f"- [{m['id']}] {m['category']}/{m['attribute']}: {m['value']}"
            for m in selection.memories) or "(none yet)"
        prompt = (f"Month: {month}\n\nExisting beliefs (do not restate):\n"
                  f"{catalog}\n\nJournal entries:\n{block}")
        system = SYSTEM_PROMPT + (DEEP_ADDENDUM if deep else "")
        log_diag("prompt", f"surface=journal-import month={month} deep={deep} "
                 f"memories={len(selection.memories)}/{len(memory_catalog)} "
                 f"input_chars={len(system) + len(prompt)} "
                 f"estimated_tokens={estimate_tokens(len(system) + len(prompt))}")
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text")
        return _parse_statements(text)


def get_journal_model(cfg, backend: str | None = None):
    choice = backend or getattr(cfg, "llm_backend", "claude")
    if choice == "stub":
        return StubJournalModel()
    return ClaudeJournalModel(
        getattr(cfg, "journal_import_model", "claude-sonnet-4-6"),
        timeout_seconds=getattr(cfg, "llm_timeout_seconds", 60.0) * 2,
        memory_max_items=getattr(cfg, "triage_memory_max_items", 30),
        memory_max_chars=getattr(cfg, "triage_memory_max_chars", 1600))


# --- the import --------------------------------------------------------------
def _classify_statement(statement: dict, fallback_date: str,
                        active: list[dict]) -> tuple[str, dict | None, dict]:
    """Decide a proposed fact's fate WITHOUT writing: returns
    (outcome, matching_active_fact_or_None, normalized_fields). Shared by the
    real import and the dry run, so dry runs report true duplicate/stale
    counts against current memory instead of raw model proposals."""
    date = statement.get("date") or fallback_date
    category = str(statement["category"]).strip().lower()
    attribute = str(statement["attribute"]).strip()
    value = str(statement["value"]).strip()
    fields = {"date": date, "category": category,
              "attribute": attribute, "value": value}
    existing = next(
        (fact for fact in active
         if (fact["category"] or "").lower() == category
         and _norm(fact["attribute"]) == _norm(attribute)), None)
    if existing is None:
        return "added", None, fields
    if _is_duplicate(existing["value"], value, 0.85):
        return "duplicate", existing, fields
    if date < (existing.get("valid_from") or ""):
        return "stale", existing, fields
    return "superseded", existing, fields


def _apply_statement(mem: MemoryStore, statement: dict, fallback_date: str,
                     active: list[dict],
                     recorded: str | None = None) -> tuple[str, dict | None]:
    """Commit one proposed fact. Returns (outcome, updated_active_row).
    `recorded` (deep mode) = the retrospective's writing date, kept as
    provenance when it differs from the fact's event date."""
    outcome, existing, f = _classify_statement(statement, fallback_date, active)
    date, category = f["date"], f["category"]
    attribute, value = f["attribute"], f["value"]
    ref = {"journal_import": date}
    if recorded and recorded != date:
        ref["recorded"] = recorded
    if outcome == "added":
        new_id = mem.add(category, attribute, value, valid_from=date,
                         confidence=statement.get("confidence"),
                         source_refs=[ref])
        return "added", {"id": new_id, "category": category,
                         "attribute": attribute, "value": value,
                         "valid_from": date}
    if outcome in ("duplicate", "stale"):
        return outcome, None
    new_id = mem.supersede(existing["id"], value, attribute=attribute,
                           confidence=statement.get("confidence"),
                           source_refs=[ref], as_of=date)
    existing.update({"id": new_id, "value": value, "valid_from": date})
    return "superseded", None


def _refilter_batches(batches: list, cfg) -> tuple[list, dict]:
    """Run the local relevance filter over the whole corpus (chronological
    order preserved, so dedupe keeps the oldest copy), then regroup."""
    flat = [entry for _, entries in batches for entry in entries]
    kept, fstats = filter_entries(
        flat,
        min_chars=int(getattr(cfg, "journal_filter_min_chars", 80)),
        min_score=float(getattr(cfg, "journal_filter_min_score", 1.0)),
        similarity=float(getattr(cfg, "journal_filter_similarity", 0.90)),
        max_chars=int(getattr(cfg, "journal_entry_max_chars", 6000)))
    months: dict[str, list[dict]] = {}
    undated: list[dict] = []
    for entry in kept:
        if entry.get("date"):
            months.setdefault(entry["date"][:7], []).append(entry)
        else:
            undated.append(entry)
    rebatched = [(m, sorted(v, key=lambda e: e["date"]))
                 for m, v in sorted(months.items())]
    if undated:
        rebatched.append((UNDATED_BATCH, undated))
    return rebatched, fstats


def import_journals(cfg, mem: MemoryStore, *, model=None,
                    journal_dir: str | None = None, dry_run: bool = False,
                    min_confidence: float | None = None,
                    only_month: str | None = None,
                    reset: bool = False,
                    filter_enabled: bool | None = None,
                    deep: bool = False) -> dict:
    """Run the chronological import. Returns counters + per-month detail."""
    journal_dir = journal_dir or getattr(cfg, "journal_dir", "data/notion")
    model = model or get_journal_model(cfg)
    gate = (min_confidence if min_confidence is not None
            else getattr(cfg, "journal_min_confidence", 0.7))
    batch_max_chars = int(getattr(cfg, "journal_batch_max_chars", 24000))
    use_filter = (filter_enabled if filter_enabled is not None
                  else bool(getattr(cfg, "journal_filter_enabled", True)))

    batches = batch_by_month(load_journals(journal_dir))
    filter_stats = None
    if use_filter:
        batches, filter_stats = _refilter_batches(batches, cfg)
    watermark = None if reset else mem.get_meta(WATERMARK_KEY)
    stats = {"batches": 0, "entries": 0, "added": 0, "superseded": 0,
             "duplicate": 0, "stale": 0, "low_confidence": 0, "months": [],
             "filter": filter_stats}

    active = mem.active_as_dicts()
    for month, entries in batches:
        if only_month and month != only_month:
            continue
        if watermark and month != UNDATED_BATCH and month <= watermark:
            continue
        if month == UNDATED_BATCH:
            fallback = (max((e.get("exported_at") or "") for e in entries))[:10]
        else:
            fallback = entries[-1]["date"]   # sorted ascending: batch max
        month_stats = {"month": month, "entries": len(entries), "added": 0,
                       "superseded": 0, "duplicate": 0, "stale": 0,
                       "low_confidence": 0}
        if deep:
            # per-entry calls: exhaustive extraction, event-time dating
            units = [(_format_batch(month, [entry], batch_max_chars)[0],
                      (entry.get("date") or fallback))
                     for entry in entries]
        else:
            units = [(block, fallback)
                     for block in _format_batch(month, entries, batch_max_chars)]
        for block, unit_fallback in units:
            kwargs = {"deep": True} if deep else {}
            for statement in model.propose(month, block, active, **kwargs):
                if (statement.get("confidence") or 0) < gate:
                    month_stats["low_confidence"] += 1
                    continue
                sdate = str(statement.get("date") or "")
                if deep:
                    # event dating allowed, but never future (past the writing
                    # date) and never absurd — those are invented
                    if not sdate or sdate > unit_fallback or sdate < "1900":
                        statement["date"] = None   # -> writing date
                elif month != UNDATED_BATCH and not sdate.startswith(month):
                    # normal mode: the model must not date a fact outside the
                    # batch it read; anything else -> clamp to the batch
                    statement["date"] = None   # falls back to the batch max
                if dry_run:
                    # classify against real memory (and simulate commits in the
                    # local `active` list) so counts match a real run
                    outcome, existing, f = _classify_statement(
                        statement, unit_fallback, active)
                    month_stats[outcome] += 1
                    if outcome == "added":
                        active.append({"id": None, "category": f["category"],
                                       "attribute": f["attribute"],
                                       "value": f["value"],
                                       "valid_from": f["date"]})
                    elif outcome == "superseded" and existing:
                        existing.update({"value": f["value"],
                                         "valid_from": f["date"]})
                    continue
                outcome, new_row = _apply_statement(
                    mem, statement, unit_fallback, active,
                    recorded=unit_fallback if deep else None)
                month_stats[outcome] += 1
                if new_row:
                    active.append(new_row)
        stats["batches"] += 1
        stats["entries"] += len(entries)
        for key in ("added", "superseded", "duplicate", "stale", "low_confidence"):
            stats[key] += month_stats[key]
        stats["months"].append(month_stats)
        if not dry_run and month != UNDATED_BATCH:
            # never regress: a --month re-run of an old batch must not pull the
            # watermark backwards and cause a silent re-walk next full run
            current = mem.get_meta(WATERMARK_KEY)
            if not current or month > current:
                mem.set_meta(WATERMARK_KEY, month)
        log_diag("journal", f"import month={month} entries={len(entries)} "
                 f"added={month_stats['added']} superseded={month_stats['superseded']} "
                 f"dry_run={dry_run}")
    if not dry_run and not only_month:
        record_imported_files(mem, journal_dir)
    return stats
