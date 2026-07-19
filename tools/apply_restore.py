"""Activate a prepared restore after the GUI process has fully exited."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _wait_for_exit(pid: int, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _relaunch(view: str, *, fresh_auth_required: bool = False) -> None:
    script = os.path.join(ROOT, "gui.py")
    kwargs = {"cwd": ROOT, "close_fds": True}
    if fresh_auth_required:
        environment = os.environ.copy()
        environment.pop("ANTHROPIC_API_KEY", None)
        environment.pop("NOTION_API_KEY", None)
        kwargs["env"] = environment
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen([sys.executable, script, "--view", view], **kwargs)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Apply a verified Faerie Fire restore")
    parser.add_argument("--token", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--config", required=True)
    parser.add_argument("--view", default="self")
    args = parser.parse_args(argv)
    from livingpc.config import load
    from livingpc.instance_backup import apply_prepared_restore, discard_prepared_restore

    cfg = load(os.path.abspath(args.config))
    result = None
    fresh_auth_required = False
    try:
        if not _wait_for_exit(args.parent_pid):
            return 2
        result = apply_prepared_restore(cfg, args.token)
        if bool(getattr(result, "ok", False)):
            from livingpc import onboarding
            onboarding.mark_complete()
            onboarding.mark_restore_auth_required()
            fresh_auth_required = True
            return_code = 0
        else:
            return_code = 1
        return return_code
    finally:
        if result is None or not bool(getattr(result, "ok", False)):
            try:
                discard_prepared_restore(args.token, cfg)
            except Exception:
                pass
        try:
            _relaunch(args.view, fresh_auth_required=fresh_auth_required)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

