"""Print bounded, privacy-safe context for one Faerie Fire subsystem."""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_REPORT_LINES = 240


@dataclass(frozen=True)
class Area:
    flow: str
    files: tuple[str, ...]
    tests: tuple[str, ...]
    verify: str = "python -m pytest -q"


AREAS = {
    "capture": Area(
        "tray.py opens Command Center and, in personal profile, owns one service thread; "
        "launch profile disables capture/collectors.",
        ("tray.py", "livingpc/service.py", "livingpc/sampler.py", "livingpc/capture/window.py",
         "livingpc/capture/screen.py", "livingpc/capture/extras.py", "capture_status.py"),
        ("tests/test_sampler.py", "tests/test_collectors.py", "tests/test_storage.py"),
    ),
    "triage": Area(
        "events -> aggregate -> redact -> relevant memories + catalog -> LLM -> auto-committed facts; "
        "journal_import backfills exported journals chronologically with dated facts.",
        ("run_triage.py", "livingpc/triage/aggregate.py", "livingpc/triage/redact.py",
         "livingpc/triage/pipeline.py", "livingpc/triage/llm.py", "livingpc/memory_context.py",
         "livingpc/journal_import.py", "livingpc/journal_filter.py",
         "tools/import_journal.py"),
        ("tests/test_triage.py", "tests/test_incremental.py", "tests/test_rejections.py",
         "tests/test_memory_context.py", "tests/test_journal_import.py",
         "tests/test_journal_filter.py"),
    ),
    "companion": Area(
        "Command Center embeds the companion bridge; legacy companion.py is locked unless opted in; "
        "personal profile adds recent screen context while launch profile uses only explicit chat/memory/investigation context.",
        ("companion.py", "livingpc/companion/companion.html", "livingpc/companion/brain.py",
         "livingpc/companion/history.py", "livingpc/companion/personas.py", "livingpc/companion/voice.py",
         "livingpc/companion/ears.py", "livingpc/memory_context.py"),
        ("tests/test_companion.py", "tests/test_ears.py"),
    ),
    "filing": Area(
        "brain dump (companion /file or CLI) -> redact -> doc catalog -> LLM -> append-only marked "
        "entries in projects/*.md, undoable by entry id; approval-gated distill restructures one doc "
        "with a pre-distill history copy; nightly pass zips projects/ beside the memory backups. "
        "Also companion extensibility: skills/*.py slash commands (/teach drafts one, installs only "
        "on explicit approval) and /remind reminders fired by the daemon's 30s poll.",
        ("livingpc/filing.py", "tools/file_dump.py", "livingpc/companion/brain.py",
         "livingpc/config.py", "livingpc/triage/redact.py",
         "livingpc/skills.py", "livingpc/reminders.py",
         "skills/remind.py", "skills/today.py", "skills/briefing.py"),
        ("tests/test_filing.py", "tests/test_skills.py"),
    ),
    "review": Area(
        "pywebview GUI owns persistent inference investigations and memory review; accepted "
        "canonical beliefs absorb repeated candidates; native agent windows stage changes for one "
        "explicit commit; approved curiosity profiles produce local daily mastery snapshots; the "
        "encrypted Soul/Root/Branch/Leaf tree keeps completion separate from mastery while bounded "
        "GoalAI agents report and harvest upward, with Soul-approved crossover routes; one claimed "
        "8 PM daily cycle runs incremental inference, curiosities, and dirty GoalAI paths before "
        "housekeeping, while metadata-only usage accounting tracks tokens and estimated cost.",
        ("gui.py", "agent_window.py", "run_triage.py", "livingpc/memory.py", "livingpc/triage/types.py",
         "livingpc/inference.py", "livingpc/inference_review.py",
         "livingpc/inference_inquiry.py", "livingpc/feedback.py", "livingpc/clarify.py",
         "livingpc/curiosity.py",
         "livingpc/curiosity_metrics.py", "livingpc/context_attachment.py",
         "livingpc/goals.py", "livingpc/goal_ai.py",
         "livingpc/llm_usage.py",
         "livingpc/inference_scheduler.py", "livingpc/reflection_cadence.py",
         "livingpc/notion_sync.py", "livingpc/config.py", "livingpc/docx_text.py",
         "tools/check_notion.py", "tools/notion_curiosity_status.py",
         "assets/notion/life-hub-hero.png", "assets/notion/life-hub-focus.png",
         "assets/notion/life-hub-footer.png", "assets/notion/curiosity-cover-journal.png",
         "assets/notion/curiosity-cover-observatory.png",
         "assets/notion/curiosity-cover-ripples.png",
         "livingpc/ui/__init__.py", "livingpc/ui/memory.html",
         "livingpc/ui/agent_window.html"),
        ("tests/test_memory.py", "tests/test_rejections.py",
         "tests/test_triage.py", "tests/test_inference.py",
         "tests/test_inference_review.py", "tests/test_ui_bridges.py",
         "tests/test_feedback.py", "tests/test_inference_inquiry.py", "tests/test_agent_window.py",
         "tests/test_clarify.py", "tests/test_curiosity.py",
         "tests/test_curiosity_metrics.py", "tests/test_context_attachment.py",
         "tests/test_inference_scheduler.py",
         "tests/test_reflection_cadence.py", "tests/test_upward_spiral_journeys.py",
         "tests/test_notion_sync.py", "tests/test_goals.py", "tests/test_goal_ai.py"),
    ),
    "storage": Area(
        "capture writes data/living_computer.db; approved facts and pending proposals use data/memory.db; "
        "all SQLite connections go through livingpc/db.py (WAL + busy timeout) so cross-process "
        "readers and the writer never block each other; "
        "nightly hygiene consolidates memory and snapshots it into data/backups; "
        "explicit forgetting removes a fact, linked traces, backups, and configured mirrors.",
        ("livingpc/config.py", "livingpc/db.py", "livingpc/storage.py", "livingpc/memory.py",
         "livingpc/crypto.py", "livingpc/backup.py", "livingpc/consolidate.py",
         "livingpc/forget.py", "encrypt_db.py", "tools/backup_memory.py",
         "tools/consolidate_memory.py", "tools/forget_memory.py"),
        ("tests/test_db.py", "tests/test_storage.py", "tests/test_memory.py",
         "tests/test_encryption_pending.py",
         "tests/test_backup.py", "tests/test_consolidate.py", "tests/test_forget.py"),
    ),
    "diagnostics": Area(
        "control UI (pywebview, livingpc/ui/capture.html) invokes status/reset/collectors; "
        "bundles expose metadata and scoped UI evidence.",
        ("capture_control.py", "livingpc/diagnostics.py", "collect_diagnostics.py",
         "collect_companion_diagnostics.py", "reset_capture.py", "capture_status.py",
         "livingpc/notify.py", "livingpc/ui/capture.html", "livingpc/ui/assistant.html"),
        ("tests/test_collectors.py", "tests/test_ui_bridges.py", "tests/test_notify.py"),
    ),
}


def _python_facts(relative: str) -> tuple[list[str], list[str]]:
    path = ROOT / relative
    if path.suffix != ".py" or not path.exists():
        return [], []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return [], []
    symbols = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ][:12]
    imports = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module = ("." * node.level) + node.module
            if module.startswith(("livingpc", ".")):
                imports.append(module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names if alias.name.startswith("livingpc"))
    return symbols, sorted(set(imports))[:8]


def _config_keys() -> list[str]:
    tree = ast.parse((ROOT / "livingpc/config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            return [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
    return []


def _git_changes() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"], cwd=ROOT, capture_output=True,
            text=True, timeout=10, check=False,
        )
        return result.stdout.splitlines()[:30]
    except (OSError, subprocess.SubprocessError):
        return ["(git status unavailable)"]


def validate_manifest() -> list[str]:
    errors = []
    required = {
        ".githooks/pre-commit",
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "FEATURES.md",
        "docs/HANDOFF.md",
        "docs/INDEX.md",
        "tools/update_handoff.py",
    }
    for area in AREAS.values():
        required.update(area.files)
        required.update(area.tests)
    for relative in sorted(required):
        if not (ROOT / relative).exists():
            errors.append(f"missing: {relative}")
    return errors


def render_context(area_name: str) -> str:
    names = list(AREAS) if area_name == "all" else [area_name]
    lines = ["# Faerie Fire project context", "", "Runtime data: data/", "Launchers: bats/"]
    for name in names:
        area = AREAS[name]
        lines.extend(["", f"## {name}", area.flow, "", "Files:"])
        for relative in area.files:
            symbols, imports = _python_facts(relative)
            detail = f"; symbols={','.join(symbols)}" if symbols else ""
            if imports:
                detail += f"; local_imports={','.join(imports)}"
            lines.append(f"- {relative}{detail}")
        lines.append("Tests: " + ", ".join(area.tests))
        lines.append("Verify: " + area.verify)
    lines.extend(["", "Config keys:", ", ".join(_config_keys()), "", "Git changes:"])
    changes = _git_changes()
    lines.extend(changes or ["(clean)"])
    if len(lines) > MAX_REPORT_LINES:
        lines = lines[: MAX_REPORT_LINES - 1] + ["(report truncated)"]
    return "\n".join(lines) + "\n"


def token_report() -> str:
    """Compare full and bounded prompt context without printing private content."""
    try:
        from livingpc.companion.brain import Companion, StubChat
        from livingpc.config import load
        from livingpc.memory import MemoryStore, today
        from livingpc.memory_context import estimate_tokens, format_memories, select_memories
        from livingpc.storage import EventLog, now_iso
        from livingpc.triage.aggregate import build_summary, day_bounds
        from livingpc.triage.llm import SYSTEM_PROMPT, build_user_prompt
        from livingpc.triage.redact import redact

        cfg = load("config.toml")
        memory = MemoryStore(cfg.memory_db_path)
        try:
            active = memory.active_as_dicts()
            rejected = memory.recent_rejections()
            start = memory.get_meta("last_triage_ts") or day_bounds(today())[0]
        finally:
            memory.close()
        events = EventLog(cfg.db_path)
        try:
            summary = redact(build_summary(events, start, now_iso(), "current incremental summary"))
        finally:
            events.close()

        triage_selection = select_memories(
            active, summary, max_items=cfg.triage_memory_max_items,
            max_chars=cfg.triage_memory_max_chars,
            value_max_chars=cfg.triage_memory_value_max_chars,
        )
        old_declined = "\n".join(
            f'[{item.get("category", "")}] {item.get("label", "")}' for item in rejected
        )
        old_triage_prompt = (
            "ACTIVE MEMORIES:\n" + format_memories(active, include_id=True)
            + "\nRECENTLY DECLINED:\n" + old_declined
            + "\nTODAY'S ACTIVITY SUMMARY:\n" + summary
        )
        old_triage = len(SYSTEM_PROMPT) + len(old_triage_prompt)
        new_triage_prompt = build_user_prompt(
            summary, triage_selection.memories, rejected, all_memories=active
        )
        new_triage = len(SYSTEM_PROMPT) + len(new_triage_prompt)

        companion = Companion(cfg=cfg, chat=StubChat())
        screen = companion._screen_block()
        companion_selection = select_memories(
            active, screen, max_items=cfg.companion_memory_max_items,
            max_chars=cfg.companion_memory_max_chars,
            value_max_chars=cfg.companion_memory_value_max_chars,
        )
        full_memory_chars = len(format_memories(active))
        old_companion = len(companion.persona.system) + len(screen) + full_memory_chars
        new_companion = (
            len(companion.persona.system) + len(screen) + companion_selection.selected_chars
        )

        assistant_selection = select_memories(
            active, screen, max_items=cfg.assistant_memory_max_items,
            max_chars=cfg.assistant_memory_max_chars,
            value_max_chars=cfg.assistant_memory_value_max_chars,
        )
        old_assistant = full_memory_chars + len(screen)
        new_assistant = assistant_selection.selected_chars + len(screen)

        def row(label, old_chars, new_chars, selected):
            reduction = 0 if not old_chars else round((1 - new_chars / old_chars) * 100)
            return (
                f"{label}: full~{estimate_tokens(old_chars)} tokens; "
                f"bounded~{estimate_tokens(new_chars)} tokens; reduction={reduction}%; "
                f"memories={len(selected.memories)}/{len(active)}"
            )

        return "\n".join([
            "# Privacy-safe token estimates (approximately 4 chars/token)",
            row("triage input", old_triage, new_triage, triage_selection),
            row("companion base before history", old_companion, new_companion, companion_selection),
            row("assistant text before question/image", old_assistant, new_assistant, assistant_selection),
            "No memory, OCR, clipboard, URL, window-title, or conversation content is printed.",
        ]) + "\n"
    except Exception as error:
        return f"Token report unavailable: {type(error).__name__}: {error}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("area", nargs="?", choices=[*AREAS, "all"], default="all")
    parser.add_argument("--tokens", action="store_true", help="append privacy-safe prompt estimates")
    parser.add_argument("--verify", action="store_true", help="validate every mapped file and test")
    args = parser.parse_args()

    if args.verify:
        errors = validate_manifest()
        if errors:
            print("Context map validation failed:")
            print("\n".join(f"- {error}" for error in errors))
            return 1
        print("Context map validation passed.")
    print(render_context(args.area), end="")
    if args.tokens:
        print(token_report(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
