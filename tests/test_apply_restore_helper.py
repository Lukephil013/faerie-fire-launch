from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "apply_restore_helper", ROOT / "tools" / "apply_restore.py")
helper = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(helper)


def test_wait_for_parent_exit_before_activation(monkeypatch):
    states = iter((True, True, False))
    sleeps = []
    monkeypatch.setattr(helper, "_pid_alive", lambda _pid: next(states))
    monkeypatch.setattr(helper.time, "sleep", lambda seconds: sleeps.append(seconds))
    assert helper._wait_for_exit(123, timeout=10) is True
    assert sleeps == [0.1, 0.1]


def test_helper_cli_requires_token_parent_and_config():
    source = (ROOT / "tools" / "apply_restore.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--token", required=True)' in source
    assert 'parser.add_argument("--parent-pid", required=True, type=int)' in source
    assert 'parser.add_argument("--config", required=True)' in source
    assert "apply_prepared_restore(cfg, args.token)" in source
    assert source.index("_wait_for_exit(args.parent_pid)") < source.index(
        "apply_prepared_restore(cfg, args.token)")

