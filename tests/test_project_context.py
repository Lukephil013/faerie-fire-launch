"""Tests for the bounded agent context utility."""
from tools import project_context


def test_context_manifest_is_current():
    assert project_context.validate_manifest() == []


def test_subsystem_report_is_bounded_and_routed():
    report = project_context.render_context("triage")
    assert "livingpc/triage/llm.py" in report
    assert "tests/test_memory_context.py" in report
    assert len(report.splitlines()) <= project_context.MAX_REPORT_LINES


def test_every_requested_area_exists():
    assert set(project_context.AREAS) == {
        "capture", "triage", "companion", "filing", "review", "storage",
        "diagnostics"
    }


def test_agent_bootstrap_files_stay_small():
    budgets = {"AGENTS.md": 800, "CLAUDE.md": 100, "docs/HANDOFF.md": 600}
    for relative, max_tokens in budgets.items():
        chars = (project_context.ROOT / relative).read_text(encoding="utf-8")
        assert (len(chars) + 3) // 4 <= max_tokens, relative


def test_manifest_validation_reports_stale_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(project_context, "ROOT", tmp_path)
    monkeypatch.setattr(
        project_context,
        "AREAS",
        {"fake": project_context.Area("flow", ("missing.py",), ("tests/missing.py",))},
    )
    errors = project_context.validate_manifest()
    assert "missing: missing.py" in errors
    assert "missing: tests/missing.py" in errors
