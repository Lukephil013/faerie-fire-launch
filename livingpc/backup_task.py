"""Per-user Windows Task Scheduler integration for portable backups.

The task runs only in the interactive user's security context so DPAPI can
unwrap the repository key without storing a Windows password.  App-startup
fallbacks cover jobs missed while the user was logged out.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import APP_DIR


_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3
_TASK_ACTION_EXEC = 0
_TASK_TRIGGER_DAILY = 2
_TASK_INSTANCES_IGNORE_NEW = 2


@dataclass(frozen=True)
class BackupTaskStatus:
    ok: bool
    installed: bool
    name: str
    error_code: str = ""


def backup_task_name(app_dir: str = APP_DIR) -> str:
    """Stable, privacy-safe task name for one installation folder."""
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(app_dir)).encode()).hexdigest()[:10]
    return f"Faerie Fire Backup {digest}"


def _task_service():
    import win32com.client  # type: ignore[import-untyped]

    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def _start_boundary(hour: int) -> str:
    hour = max(0, min(23, int(hour)))
    now = datetime.now().astimezone()
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return start.isoformat(timespec="seconds")


def register_backup_task(cfg, *, python_executable: str | None = None,
                         config_path: str | None = None) -> BackupTaskStatus:
    """Create/update the current user's daily backup task without elevation."""
    name = backup_task_name()
    if os.name != "nt":
        return BackupTaskStatus(False, False, name, "unsupported_platform")
    if not getattr(cfg, "instance_backup_enabled", False):
        return BackupTaskStatus(False, False, name, "backup_not_enabled")
    primary = str(getattr(cfg, "instance_backup_primary_dir", "") or "")
    if not primary or not os.path.isabs(primary):
        return BackupTaskStatus(False, False, name, "primary_not_absolute")

    executable = os.path.abspath(python_executable or sys.executable)
    script = os.path.join(APP_DIR, "tools", "backup_instance.py")
    config_path = os.path.abspath(config_path or os.path.join(APP_DIR, "config.toml"))
    arguments = subprocess.list2cmdline([script, "scheduled", "--config", config_path])
    try:
        service = _task_service()
        folder = service.GetFolder("\\")
        task = service.NewTask(0)
        task.RegistrationInfo.Description = (
            "Creates an encrypted Faerie Fire recovery backup for the current user."
        )
        task.Settings.Enabled = True
        task.Settings.StartWhenAvailable = True
        task.Settings.DisallowStartIfOnBatteries = False
        task.Settings.StopIfGoingOnBatteries = False
        task.Settings.ExecutionTimeLimit = "PT2H"
        task.Settings.MultipleInstances = _TASK_INSTANCES_IGNORE_NEW

        trigger = task.Triggers.Create(_TASK_TRIGGER_DAILY)
        trigger.StartBoundary = _start_boundary(
            getattr(cfg, "instance_backup_hour", 20))
        trigger.DaysInterval = 1
        trigger.Enabled = True

        action = task.Actions.Create(_TASK_ACTION_EXEC)
        action.Path = executable
        action.Arguments = arguments
        action.WorkingDirectory = APP_DIR
        folder.RegisterTaskDefinition(
            name, task, _TASK_CREATE_OR_UPDATE, "", "",
            _TASK_LOGON_INTERACTIVE_TOKEN,
        )
        return BackupTaskStatus(True, True, name)
    except Exception as error:  # diagnostics receive only the class, never task paths
        return BackupTaskStatus(False, False, name,
                                f"task_register_{type(error).__name__}")


def unregister_backup_task() -> BackupTaskStatus:
    name = backup_task_name()
    if os.name != "nt":
        return BackupTaskStatus(False, False, name, "unsupported_platform")
    try:
        folder = _task_service().GetFolder("\\")
        folder.DeleteTask(name, 0)
        return BackupTaskStatus(True, False, name)
    except Exception as error:
        # Missing tasks are already in the desired state. COM error text varies
        # by Windows version, so status() is used to distinguish that case.
        status = backup_task_status()
        if status.ok and not status.installed:
            return status
        return BackupTaskStatus(False, False, name,
                                f"task_remove_{type(error).__name__}")


def backup_task_status() -> BackupTaskStatus:
    name = backup_task_name()
    if os.name != "nt":
        return BackupTaskStatus(False, False, name, "unsupported_platform")
    try:
        folder = _task_service().GetFolder("\\")
        try:
            folder.GetTask(name)
        except Exception:
            return BackupTaskStatus(True, False, name)
        return BackupTaskStatus(True, True, name)
    except Exception as error:
        return BackupTaskStatus(False, False, name,
                                f"task_status_{type(error).__name__}")
