from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bats" / "New Test Instance.bat"


def _launcher_text() -> str:
    return LAUNCHER.read_text(encoding="utf-8").replace("\r\n", "\n")


def _robocopy_option(text: str, option: str) -> set[str]:
    match = re.search(rf"(?im)^\s*/{option}\s+([^\r\n]+)", text)
    assert match is not None, f"missing robocopy /{option} option"
    return {token.casefold() for token in match.group(1).rstrip(" ^").split()}


def test_new_test_instance_forces_launch_profile_and_english() -> None:
    launcher = _launcher_text()

    copy_at = launcher.index('robocopy "%SRC%" "%DEST%"')
    profile_at = launcher.index('echo profile = "launch"')
    language_at = launcher.index('echo language = "en"')
    launch_at = launcher.index('cd /d "%DEST%"')

    assert copy_at < profile_at < language_at < launch_at
    assert ') > "%DEST%\\config.toml"' in launcher


def test_smoke_gate_does_not_require_optional_node_runtime() -> None:
    smoke_test = (ROOT / "smoke_test_unified.py").read_text(encoding="utf-8")

    assert 'if shutil.which("node") is None:' in smoke_test
    assert 'return "skipped (Node.js is optional and is not installed)"' in smoke_test
    assert 're.findall(r"<script>\\s*(.*?)\\s*</script>", html, re.DOTALL)' in smoke_test
    assert 'for index, script in enumerate(scripts, start=1):' in smoke_test


def test_disposable_instance_excludes_personal_content() -> None:
    text = _launcher_text()

    excluded_directories = _robocopy_option(text, "XD")
    assert {"data", "projects", "skills", "vault"} <= excluded_directories

    excluded_files = _robocopy_option(text, "XF")
    assert "personas.json" in excluded_files


def test_only_committed_skill_files_are_added_back() -> None:
    text = _launcher_text()
    normalized = " ".join(text.split()).casefold()

    assert 'git -c "%src%" archive --format=zip' in normalized
    assert "head skills" in normalized
    assert "expand-archive -literalpath $env:ff_skills_archive" in normalized
    assert "ls-files -- skills" not in normalized
    assert "copy-item" not in normalized
    assert "get-childitem" not in normalized
