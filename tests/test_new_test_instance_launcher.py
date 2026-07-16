from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_new_test_instance_forces_launch_profile_and_english():
    launcher = (ROOT / "bats" / "New Test Instance.bat").read_text(encoding="utf-8")

    copy_at = launcher.index('robocopy "%SRC%" "%DEST%"')
    profile_at = launcher.index('echo profile = "launch"')
    language_at = launcher.index('echo language = "en"')
    launch_at = launcher.index('cd /d "%DEST%"')

    assert copy_at < profile_at < language_at < launch_at
    assert ') > "%DEST%\\config.toml"' in launcher


def test_smoke_gate_does_not_require_optional_node_runtime():
    smoke_test = (ROOT / "smoke_test_unified.py").read_text(encoding="utf-8")

    assert 'if shutil.which("node") is None:' in smoke_test
    assert 'return "skipped (Node.js is optional and is not installed)"' in smoke_test
    assert 're.findall(r"<script>\\s*(.*?)\\s*</script>", html, re.DOTALL)' in smoke_test
    assert 'for index, script in enumerate(scripts, start=1):' in smoke_test
