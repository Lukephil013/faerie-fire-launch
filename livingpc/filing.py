"""Filing engine — brain dumps -> living project documents.

The prose counterpart to triage: triage turns *observed activity* into atomic
facts; filing turns *deliberate brain dumps* into coherent per-project Markdown
docs under `projects_dir`. One inbox (the companion chat or the CLI), and the
model decides whether a dump appends to an existing project doc, starts a new
one, or needs a clarifying question.

Invariants (see docs/filing_plan.md):
- Machine writes are APPEND-ONLY: dated `###` entries under `## Log`, plus the
  leading summary blockquote (the single sanctioned in-place edit).
- Every appended entry carries a `<!-- ff:entry <id> -->` marker so a filing
  can be undone precisely. Hand edits anywhere else are never touched.
- Dumps pass through the triage redaction scrub before any LLM call.
- Diagnostics log counts/chars only, never content.
- Restructuring ("distill") is approval-gated: it proposes, the caller shows a
  diff, and only an explicit apply writes — with a pre-distill copy saved to
  `projects_dir/.history/` first.

Backend pattern copied from livingpc/triage/llm.py: Claude (cloud), stub
(offline, used by tests), and ollama (local, experimental).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import _project_path
from .diagnostics import log_diag
from .memory_context import estimate_tokens
from .triage.redact import redact

ENTRY_MARK_RE = re.compile(r"<!--\s*ff:entry\s+([A-Za-z0-9]+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
LOG_HEADING = "## Log"


# --------------------------------------------------------------------- helpers
def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "untitled"


def new_entry_id() -> str:
    """Time-sortable id: timestamp + randomness. No new dependencies."""
    return datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(4)


def projects_dir_for(cfg) -> str:
    return _project_path(getattr(cfg, "projects_dir", "projects"))


# --------------------------------------------------------------------- catalog
def read_doc(path: str) -> dict:
    """Parse one project doc: title, summary blockquote, headings, size."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    title = ""
    summary_lines: list[str] = []
    headings: list[str] = []
    seen_title = False
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level, label = m.group(1), m.group(2).strip()
            if level == "#" and not seen_title:
                title = label
                seen_title = True
            else:
                headings.append(f"{level} {ENTRY_MARK_RE.sub('', label).strip()}")
            continue
        if seen_title and line.startswith(">") and not headings:
            summary_lines.append(line.lstrip("> ").strip())
    name = os.path.splitext(os.path.basename(path))[0]
    return {"slug": name, "title": title or name,
            "summary": " ".join(summary_lines).strip(),
            "headings": headings, "chars": len(text), "path": path}


def build_catalog(projects_dir: str) -> list[dict]:
    if not os.path.isdir(projects_dir):
        return []
    docs = []
    for fname in sorted(os.listdir(projects_dir)):
        if fname.lower().endswith(".md"):
            docs.append(read_doc(os.path.join(projects_dir, fname)))
    return docs


def format_catalog(catalog: list[dict], max_chars: int = 8000) -> str:
    """Compact catalog block for the prompt, capped at max_chars."""
    if not catalog:
        return "(no project docs yet)"
    lines: list[str] = []
    for doc in catalog:
        lines.append(f"- slug: {doc['slug']}  title: {doc['title']}")
        if doc["summary"]:
            lines.append(f"  summary: {doc['summary'][:300]}")
        if doc["headings"]:
            shown = doc["headings"][:12]
            lines.append("  sections: " + " | ".join(h[:80] for h in shown))
    out = "\n".join(lines)
    return out[:max_chars]


def projects_overview(projects_dir: str) -> list[dict]:
    """For the /projects command and the CLI list mode."""
    return [{"slug": d["slug"], "title": d["title"], "summary": d["summary"]}
            for d in build_catalog(projects_dir)]


# ------------------------------------------------------------------ LLM layer
@dataclass
class Filing:
    action: str = "append"          # 'append' | 'create'
    project: str = ""               # existing slug, or a new human title
    section_title: str = ""
    markdown: str = ""
    summary_update: str | None = None
    confidence: float = 0.0


@dataclass
class FilingResult:
    filings: list = field(default_factory=list)
    clarify: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "FilingResult":
        filings = []
        for raw in data.get("filings") or []:
            if not isinstance(raw, dict):
                continue
            filings.append(Filing(
                action=str(raw.get("action") or "append").lower(),
                project=str(raw.get("project") or "").strip(),
                section_title=str(raw.get("section_title") or "").strip(),
                markdown=str(raw.get("markdown") or "").strip(),
                summary_update=(str(raw["summary_update"]).strip()
                                if raw.get("summary_update") else None),
                confidence=float(raw.get("confidence") or 0.0),
            ))
        clarify = data.get("clarify")
        return cls(filings=filings,
                   clarify=str(clarify).strip() if clarify else None)


SYSTEM_PROMPT = """\
You are the filing clerk of a personal "second brain". The user dumps raw
thoughts — a paragraph or an essay about an idea — and you file them into their
project documents. You receive a catalog of existing project docs (slug, title,
summary, section headings) and the dump.

Return STRICT JSON only (no prose, no markdown fences) with this shape:
{
  "filings": [ {"action": "append" | "create",
                "project": "existing-slug-or-New Project Title",
                "section_title": "short label for this entry",
                "markdown": "the cleaned entry text",
                "summary_update": "refreshed one-paragraph doc summary or null",
                "confidence": 0.0} ],
  "clarify": "a question to ask the user instead, or null"
}

Rules:
- PREFER appending to an existing project (use its slug). Create only when the
  dump clearly belongs to no existing doc; then "project" is a new human title.
- A multi-topic dump may split into several filings, one per project. Do not
  scatter one coherent thought across docs.
- Clean lightly: fix typos, add paragraph breaks, keep the user's voice and ALL
  of their content. Never summarize away substance — the dump is the record.
- "section_title" is a few words naming the idea, like a commit subject.
- "summary_update": only when this entry meaningfully changes what the project
  is about; otherwise null. One paragraph, plain text.
- If you cannot tell where the dump belongs, or it reads like a question or a
  chat message rather than material to file, return an empty "filings" list and
  set "clarify" to a short question. When unsure, clarify — do not guess.
- confidence is YOUR confidence that this filing is the right home for it.
"""


def build_user_prompt(dump: str, catalog_text: str) -> str:
    return (
        "EXISTING PROJECT DOCS:\n" + catalog_text + "\n\n"
        "THE DUMP TO FILE:\n" + dump + "\n\n"
        "File it as STRICT JSON per the schema."
    )


def parse_response(text: str) -> FilingResult:
    """Extract the JSON object from a model response (forgiving, like triage)."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return FilingResult()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return FilingResult()
    return FilingResult.from_dict(data)


class StubBackend:
    """Offline backend: files everything into projects/inbox.md so the whole
    pipeline (and the tests) run without an API key."""

    def file(self, dump: str, catalog_text: str) -> FilingResult:
        first_line = next((l.strip() for l in dump.splitlines() if l.strip()), "note")
        return FilingResult(filings=[Filing(
            action="append", project="inbox",
            section_title=first_line[:60], markdown=dump.strip(),
            summary_update=None, confidence=1.0,
        )])

    def distill(self, doc_text: str) -> str:
        return doc_text  # identity: distill proposes no change offline


class ClaudeBackend:
    """Cloud backend via the Anthropic API. Only the redacted dump and the
    doc catalog (titles/summaries/headings, no bodies) are sent."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None,
                 max_tokens: int = 4000, timeout_seconds: float = 60.0,
                 max_retries: int = 0):
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use --backend stub."
            )
        from anthropic import Anthropic  # lazy import

        self._client = Anthropic(api_key=self._api_key,
                                 timeout=timeout_seconds, max_retries=max_retries)

    def _call(self, system: str, user: str, surface: str) -> str:
        input_chars = len(system) + len(user)
        log_diag("prompt", f"surface={surface} input_chars={input_chars} "
                           f"estimated_tokens={estimate_tokens(input_chars)}")
        started = time.monotonic()
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            system=system, messages=[{"role": "user", "content": user}],
        )
        from .llm_usage import record_response
        record_response("other", self.model, msg, time.monotonic() - started)
        return "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text")

    def file(self, dump: str, catalog_text: str) -> FilingResult:
        text = self._call(SYSTEM_PROMPT, build_user_prompt(dump, catalog_text),
                          "filing")
        return parse_response(text)

    def distill(self, doc_text: str) -> str:
        text = self._call(DISTILL_SYSTEM_PROMPT, doc_text, "filing-distill")
        return _strip_fences(text)


class OllamaBackend:
    """Local backend via the Ollama HTTP API (experimental). Same contract;
    expect more babysitting than Claude on filing decisions."""

    def __init__(self, url: str = "http://localhost:11434",
                 model: str = "qwen2.5:14b", timeout_seconds: float = 120.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout_seconds

    def _call(self, system: str, user: str) -> str:
        import urllib.request
        payload = json.dumps({
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ((data.get("message") or {}).get("content")) or ""

    def file(self, dump: str, catalog_text: str) -> FilingResult:
        return parse_response(self._call(SYSTEM_PROMPT,
                                         build_user_prompt(dump, catalog_text)))

    def distill(self, doc_text: str) -> str:
        return _strip_fences(self._call(DISTILL_SYSTEM_PROMPT, doc_text))


def get_backend(cfg, backend: str | None = None):
    """Pick a backend: explicit arg > filing_backend > llm_backend."""
    name = (backend or getattr(cfg, "filing_backend", "")
            or getattr(cfg, "llm_backend", "claude")).lower()
    if name == "stub":
        return StubBackend()
    if name == "ollama":
        return OllamaBackend(
            url=getattr(cfg, "filing_ollama_url", "http://localhost:11434"),
            model=getattr(cfg, "filing_ollama_model", "qwen2.5:14b"),
        )
    if name == "claude":
        return ClaudeBackend(
            model=getattr(cfg, "filing_model", "claude-sonnet-4-6"),
            timeout_seconds=getattr(cfg, "llm_timeout_seconds", 60.0),
            max_retries=getattr(cfg, "llm_max_retries", 0),
        )
    raise ValueError(f"unknown filing backend: {name}")


# -------------------------------------------------------------------- applier
def _doc_skeleton(title: str, summary: str = "") -> str:
    parts = [f"# {title}", ""]
    if summary:
        parts += [f"> {summary}", ""]
    parts += [LOG_HEADING, ""]
    return "\n".join(parts)


def _entry_block(filing: Filing, entry_id: str, now: datetime) -> str:
    date = now.date().isoformat()
    title = filing.section_title or "note"
    return (f"### {date} — {title}  <!-- ff:entry {entry_id} -->\n\n"
            f"{filing.markdown.strip()}\n")


def _apply_summary_update(text: str, summary: str) -> str:
    """Replace (or insert) the leading blockquote after the H1 title — the one
    sanctioned in-place edit. Everything else is untouched."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    # copy up to and including the title line
    while i < len(lines):
        out.append(lines[i])
        if lines[i].startswith("# "):
            i += 1
            break
        i += 1
    # skip existing blank lines + blockquote
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and lines[i].lstrip().startswith(">"):
        i += 1
    out += ["", f"> {summary.strip()}"]
    rest = lines[i:]
    if rest and rest[0].strip():
        out.append("")
    out += rest
    return "\n".join(out)


def apply_filing(projects_dir: str, filing: Filing, *,
                 now: datetime | None = None) -> dict:
    """Apply one filing. Returns {entry_id, path, slug, title, created}.

    Append-only: creates the doc if needed, appends a marked entry under
    `## Log`, optionally refreshes the summary blockquote. Never writes
    outside projects_dir (slugs are sanitized to a flat namespace).
    """
    now = now or datetime.now()
    os.makedirs(projects_dir, exist_ok=True)
    want = slug(os.path.basename(filing.project))
    path = os.path.join(projects_dir, f"{want}.md")
    created = False
    if filing.action == "create" or not os.path.exists(path):
        title = filing.project if filing.action == "create" else want
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_doc_skeleton(title, filing.summary_update or ""))
            created = True

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if LOG_HEADING not in text:
        text = text.rstrip("\n") + f"\n\n{LOG_HEADING}\n"
    if filing.summary_update and not created:
        text = _apply_summary_update(text, filing.summary_update)

    entry_id = new_entry_id()
    text = text.rstrip("\n") + "\n\n" + _entry_block(filing, entry_id, now)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    doc = read_doc(path)
    log_diag("filing", f"applied action={filing.action} created={created} "
                       f"entry_chars={len(filing.markdown)} doc_chars={doc['chars']}")
    return {"entry_id": entry_id, "path": path, "slug": want,
            "title": doc["title"], "created": created}


def _is_skeleton(text: str) -> bool:
    """True when a doc holds no content beyond title/summary/empty Log."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("# ") or s.startswith(">") or s == LOG_HEADING:
            continue
        return False
    return True


def undo(projects_dir: str, entry_id: str) -> dict:
    """Remove exactly the entry with this id. Deletes the doc only when the
    entry was its sole content. Returns {found, path, deleted_doc}."""
    entry_id = entry_id.strip()
    for doc in build_catalog(projects_dir):
        with open(doc["path"], "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        start = None
        for i, line in enumerate(lines):
            m = ENTRY_MARK_RE.search(line)
            if m and m.group(1) == entry_id:
                start = i
                break
        if start is None:
            continue
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _HEADING_RE.match(lines[j]):
                end = j
                break
        remaining = lines[:start] + lines[end:]
        text = "\n".join(remaining).rstrip("\n") + "\n"
        if _is_skeleton(text):
            os.remove(doc["path"])
            log_diag("filing", f"undo entry removed; empty doc deleted")
            return {"found": True, "path": doc["path"], "deleted_doc": True}
        with open(doc["path"], "w", encoding="utf-8") as f:
            f.write(text)
        log_diag("filing", "undo entry removed")
        return {"found": True, "path": doc["path"], "deleted_doc": False}
    return {"found": False, "path": "", "deleted_doc": False}


# --------------------------------------------------------------- orchestrator
def file_dump(cfg, dump_text: str, *, backend=None, dry_run: bool = False,
              now: datetime | None = None) -> dict:
    """The pipeline: redact -> catalog -> LLM -> gate -> apply (+ memory copy).

    Returns {"filed": [apply results | proposals], "clarify": str|None,
             "dry_run": bool}. Raises nothing model-related past this point in
    normal operation only for backend construction; callers that must never
    die (the companion) wrap this best-effort.
    """
    dump = redact(dump_text or "").strip()
    if not dump:
        return {"filed": [], "clarify": "There was nothing to file.",
                "dry_run": dry_run}
    projects_dir = projects_dir_for(cfg)
    backend = backend or get_backend(cfg)
    catalog_text = format_catalog(
        build_catalog(projects_dir),
        int(getattr(cfg, "filing_catalog_max_chars", 8000)))
    result = backend.file(dump, catalog_text)

    gate = float(getattr(cfg, "filing_min_confidence", 0.6))
    confident = [f for f in result.filings
                 if f.markdown and (f.confidence or 0) >= gate]
    if not confident:
        clarify = result.clarify or (
            "I couldn't confidently place that. Which project is it about?"
            if result.filings else
            "I couldn't tell where that belongs. Which project is it about?")
        log_diag("filing", f"clarify filings={len(result.filings)} gate={gate}")
        return {"filed": [], "clarify": clarify, "dry_run": dry_run}

    if dry_run:
        proposals = [{"action": f.action, "project": f.project,
                      "section_title": f.section_title,
                      "confidence": f.confidence,
                      "chars": len(f.markdown)} for f in confident]
        return {"filed": proposals, "clarify": None, "dry_run": True}

    applied = [apply_filing(projects_dir, f, now=now) for f in confident]
    if getattr(cfg, "filing_to_memory", False):
        try:
            save_dump_for_memory(cfg, dump_text, now=now)
        except OSError:
            log_diag("filing", "memory copy failed (OSError)")
    log_diag("filing", f"filed count={len(applied)} "
                       f"created={sum(1 for a in applied if a['created'])}")
    return {"filed": applied, "clarify": None, "dry_run": False}


def save_dump_for_memory(cfg, dump_text: str, *,
                         now: datetime | None = None) -> str:
    """Write the raw dump as a journal-format file so the existing journal
    import path (`tools/import_journal.py --journal-dir <filing_journal_dir>`)
    can feed it into the memory graph. Secondary output; off by default."""
    now = now or datetime.now()
    out_dir = _project_path(getattr(cfg, "filing_journal_dir", "data/filed_dumps"))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"dump-{now.strftime('%Y%m%d-%H%M%S')}.md")
    body = (f"---\ntitle: Filed dump\nexported_at: {now.date().isoformat()}\n"
            f"default_year: {now.year}\n---\n"
            f"{now.month:02d}/{now.day:02d}/{now.year}\n\n{dump_text.strip()}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# --------------------------------------------------------------------- distill
DISTILL_SYSTEM_PROMPT = """\
You restructure one project document from a personal "second brain". Its
`## Log` has accumulated many dated entries; produce a better-organized version
of the WHOLE document.

Rules:
- Return ONLY the full replacement Markdown document. No commentary, no fences.
- Keep the `# Title` on the first line and a one-paragraph `>` summary after it.
- PRESERVE ALL INFORMATION. You may merge, reorder, and deduplicate entries
  into thematic sections, but no fact, idea, link, or nuance may be dropped.
- Keep the user's voice. Tighten, don't paraphrase into corporate prose.
- Keep a `## Log` section at the end for future appends (it may be empty).
"""


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:markdown|md)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip() + "\n"


def distill_project(cfg, project_slug: str, *, backend=None,
                    apply: bool = False, now: datetime | None = None) -> dict:
    """Propose (and optionally apply) a restructured version of one doc.

    Approval gate lives in the CALLER: run once without apply to get the diff,
    show it, and only on explicit user approval call again with apply=True.
    A pre-distill copy is always saved to projects_dir/.history/ before any
    rewrite — this is the one sanctioned whole-doc write.
    """
    now = now or datetime.now()
    projects_dir = projects_dir_for(cfg)
    path = os.path.join(projects_dir, f"{slug(os.path.basename(project_slug))}.md")
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such project doc: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        original = f.read()

    backend = backend or get_backend(cfg)
    proposed = backend.distill(original)
    diff = "\n".join(difflib.unified_diff(
        original.splitlines(), proposed.splitlines(),
        fromfile=f"{os.path.basename(path)} (current)",
        tofile=f"{os.path.basename(path)} (proposed)", lineterm=""))

    result = {"path": path, "proposed": proposed, "diff": diff,
              "changed": proposed.strip() != original.strip(), "applied": False}
    if apply and result["changed"]:
        history = os.path.join(projects_dir, ".history")
        os.makedirs(history, exist_ok=True)
        stamp = now.strftime("%Y%m%d-%H%M%S")
        base = os.path.splitext(os.path.basename(path))[0]
        with open(os.path.join(history, f"{base}-{stamp}.md"), "w",
                  encoding="utf-8") as f:
            f.write(original)
        with open(path, "w", encoding="utf-8") as f:
            f.write(proposed)
        result["applied"] = True
        log_diag("filing", f"distill applied doc_chars={len(proposed)} "
                           f"(history copy saved)")
    return result


# --------------------------------------------------------------------- backup
def snapshot_projects(projects_dir: str, backup_dir: str, *,
                      keep: int = 14, now: datetime | None = None) -> dict:
    """Zip projects_dir into backup_dir (rotating set, like memory backups)."""
    import zipfile
    if not os.path.isdir(projects_dir) or not any(
            n.lower().endswith(".md") for n in os.listdir(projects_dir)):
        return {"path": "", "kept": 0, "pruned": 0}
    os.makedirs(backup_dir, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"projects-{stamp}.zip")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(projects_dir):
            for name in files:
                if name.lower().endswith(".md"):
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, projects_dir))
    pat = re.compile(r"^projects-\d{8}-\d{6}\.zip$")
    snaps = sorted(n for n in os.listdir(backup_dir) if pat.match(n))
    pruned = 0
    for name in snaps[:-max(1, int(keep))]:
        os.remove(os.path.join(backup_dir, name))
        pruned += 1
    return {"path": dest, "kept": min(len(snaps), max(1, int(keep))),
            "pruned": pruned}
