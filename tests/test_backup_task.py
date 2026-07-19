from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from livingpc import backup_task


class _Node(SimpleNamespace):
    def __getattr__(self, name):
        value = _Node()
        setattr(self, name, value)
        return value


class _Collection:
    def __init__(self):
        self.created = []

    def Create(self, kind):
        node = _Node(kind=kind)
        self.created.append(node)
        return node


class _Folder:
    def __init__(self):
        self.registered = None
        self.present = False

    def RegisterTaskDefinition(self, *args):
        self.registered = args
        self.present = True

    def GetTask(self, _name):
        if not self.present:
            raise LookupError("missing")
        return object()

    def DeleteTask(self, _name, _flags):
        if not self.present:
            raise LookupError("missing")
        self.present = False


class _Service:
    def __init__(self):
        self.folder = _Folder()
        self.task = _Node(
            RegistrationInfo=_Node(), Settings=_Node(),
            Triggers=_Collection(), Actions=_Collection(),
        )

    def GetFolder(self, _path):
        return self.folder

    def NewTask(self, _flags):
        return self.task


def _cfg(tmp_path):
    return SimpleNamespace(
        instance_backup_enabled=True,
        instance_backup_primary_dir=str(tmp_path.resolve()),
        instance_backup_hour=20,
    )


def test_register_builds_interactive_daily_task(tmp_path):
    service = _Service()
    with (mock.patch.object(backup_task.os, "name", "nt"),
          mock.patch.object(backup_task, "_task_service", return_value=service),
          mock.patch.object(backup_task, "APP_DIR", str(tmp_path))):
        result = backup_task.register_backup_task(
            _cfg(tmp_path), python_executable=str(tmp_path / "python.exe"),
            config_path=str(tmp_path / "config.toml"),
        )
    assert result.ok and result.installed
    assert service.task.Settings.StartWhenAvailable is True
    assert service.task.Triggers.created[0].DaysInterval == 1
    assert service.task.Actions.created[0].WorkingDirectory == str(tmp_path)
    assert "scheduled" in service.task.Actions.created[0].Arguments
    assert service.folder.registered[-1] == backup_task._TASK_LOGON_INTERACTIVE_TOKEN


def test_register_rejects_relative_or_disabled_destination(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.instance_backup_primary_dir = "relative"
    with mock.patch.object(backup_task.os, "name", "nt"):
        assert backup_task.register_backup_task(cfg).error_code == "primary_not_absolute"
    cfg.instance_backup_primary_dir = str(tmp_path.resolve())
    cfg.instance_backup_enabled = False
    with mock.patch.object(backup_task.os, "name", "nt"):
        assert backup_task.register_backup_task(cfg).error_code == "backup_not_enabled"


def test_status_and_unregister_are_idempotent(tmp_path):
    service = _Service()
    with (mock.patch.object(backup_task.os, "name", "nt"),
          mock.patch.object(backup_task, "_task_service", return_value=service)):
        assert backup_task.backup_task_status().installed is False
        service.folder.present = True
        assert backup_task.backup_task_status().installed is True
        removed = backup_task.unregister_backup_task()
        assert removed.ok and not removed.installed
        again = backup_task.unregister_backup_task()
        assert again.ok and not again.installed


def test_non_windows_is_explicitly_unsupported():
    with mock.patch.object(backup_task.os, "name", "posix"):
        status = backup_task.backup_task_status()
    assert not status.ok
    assert status.error_code == "unsupported_platform"
