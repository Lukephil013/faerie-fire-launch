"""Tests for the tracked, privacy-safe agent handoff generator."""
from tools import update_handoff


def test_infer_areas_from_changed_files():
    areas = update_handoff.infer_areas([
        "livingpc/triage/llm.py",
        "tests/test_triage.py",
        "AGENTS.md",
    ])
    assert "triage" in areas
    assert "agent-context" in areas


def test_worktree_parser_preserves_dotfiles(monkeypatch):
    monkeypatch.setattr(
        update_handoff,
        "_git",
        lambda *args: " M .gitignore\n?? AGENTS.md",
    )
    assert update_handoff.changed_files(staged=False) == [".gitignore", "AGENTS.md"]


def test_rendered_handoff_is_bounded_and_actionable():
    text = update_handoff.render_handoff(
        generated_at="2026-06-27T00:00:00+00:00",
        branch="main",
        head="abc1234",
        files=[f"livingpc/file_{index}.py" for index in range(30)],
        areas=["triage"],
        recent_commits=["abc1234 prior change"],
    )
    assert len(text) <= 3000
    assert "python tools/project_context.py triage" in text
    assert "and 10 more" in text
    assert "Test status is not inferred" in text


def test_handoff_contains_metadata_only():
    text = update_handoff.render_handoff(
        generated_at="2026-06-27T00:00:00+00:00",
        branch="main",
        head="abc1234",
        files=["companion.py"],
        areas=["companion"],
        recent_commits=[],
    )
    assert "memory, OCR, clipboard, conversation" in text
    assert "python tools/project_context.py companion" in text
