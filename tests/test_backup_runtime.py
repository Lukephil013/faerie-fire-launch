from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from livingpc.backup_runtime import BackupRuntime, RETRY_SECONDS


def _cfg(enabled=True):
    return SimpleNamespace(instance_backup_enabled=enabled)


def test_disabled_runtime_does_not_touch_engine():
    calls = []
    runtime = BackupRuntime(
        _cfg(False), status_fn=lambda _cfg: calls.append("status"),
        create_fn=lambda *_args, **_kwargs: calls.append("create"))
    assert runtime.check_once() is False
    assert calls == []


def test_overdue_backup_runs_and_clears_retry_state():
    calls = []
    runtime = BackupRuntime(
        _cfg(), status_fn=lambda _cfg: SimpleNamespace(ok=True, due=True),
        create_fn=lambda _cfg, reason: (
            calls.append(reason) or SimpleNamespace(ok=True, verified=True)))
    assert runtime.check_once() is True
    assert calls == ["startup_overdue"]
    assert runtime.state().last_error_code == ""


def test_failure_schedules_hourly_retry_without_private_error_text():
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)

    def explode(_cfg):
        raise OSError("C:/private/location")

    runtime = BackupRuntime(_cfg(), status_fn=explode,
                            create_fn=lambda *_args, **_kwargs: None,
                            now_fn=lambda: now)
    assert runtime.check_once() is False
    state = runtime.state()
    assert state.last_error_code == "runtime_OSError"
    assert "private" not in state.last_error_code
    assert state.next_attempt_utc == (
        now.timestamp() + RETRY_SECONDS and
        datetime.fromtimestamp(now.timestamp() + RETRY_SECONDS,
                               tz=timezone.utc).isoformat())

