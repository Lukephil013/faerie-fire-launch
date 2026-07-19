"""Create, inspect, schedule, and restore portable Faerie Fire backups.

Recovery passphrases are accepted only through ``getpass``.  They are never
command-line arguments, environment variables, config values, or log fields.
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.config import load  # noqa: E402


def _plain(value):
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name))
                for field in dataclasses.fields(value)
                if field.name not in {"token", "passphrase", "key", "secret"}}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()
                if str(key).lower() not in {"token", "passphrase", "key", "secret"}}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _print(value) -> None:
    print(json.dumps(_plain(value), indent=2, sort_keys=True, default=str))


def _ok(value) -> bool:
    return bool(getattr(value, "ok", True))


def _cfg(args):
    return load(os.path.abspath(args.config))


def _status(args) -> int:
    from livingpc.instance_backup import backup_status

    result = backup_status(_cfg(args))
    _print(result)
    return 0 if _ok(result) else 1


def _create(args) -> int:
    from livingpc.instance_backup import create_instance_backup

    result = create_instance_backup(_cfg(args), reason=args.reason)
    _print(result)
    return 0 if _ok(result) else 1


def _scheduled(args) -> int:
    from livingpc.instance_backup import backup_status, create_instance_backup

    cfg = _cfg(args)
    status = backup_status(cfg)
    if not _ok(status):
        _print(status)
        return 1
    if not bool(getattr(status, "due", True)):
        _print({"ok": True, "skipped": "not_due",
                "checked_utc": datetime.now(timezone.utc).isoformat()})
        return 0
    result = create_instance_backup(cfg, reason="scheduled")
    _print(result)
    return 0 if _ok(result) else 1


def _restore(args) -> int:
    from livingpc.instance_backup import (
        apply_prepared_restore,
        discard_prepared_restore,
        inspect_backup,
        prepare_restore,
    )

    cfg = _cfg(args)
    info = inspect_backup(args.bundle)
    _print(info)
    passphrase = getpass.getpass("Recovery passphrase: ")
    prepared = None
    finished = False
    try:
        prepared = prepare_restore(cfg, args.bundle, passphrase)
        _print({"ok": getattr(prepared, "ok", True),
                "ready": getattr(prepared, "ok", True),
                "preview": getattr(prepared, "preview", None),
                "warnings": getattr(prepared, "warnings", ())})
        if not _ok(prepared):
            return 1
        if not args.yes:
            confirm = input("Replace this Faerie Fire profile with the staged backup? [y/N] ")
            if confirm.strip().lower() not in {"y", "yes"}:
                discard_prepared_restore(getattr(prepared, "token"), cfg)
                finished = True
                print("Restore cancelled.")
                return 0
        result = apply_prepared_restore(cfg, getattr(prepared, "token"))
        _print(result)
        finished = _ok(result)
        return 0 if _ok(result) else 1
    finally:
        passphrase = ""
        if prepared is not None and not finished:
            # apply/discard are idempotent; this also removes abandoned staging
            # after validation or confirmation failures.
            try:
                discard_prepared_restore(getattr(prepared, "token"), cfg)
            except Exception:
                pass


def _schedule(args) -> int:
    from livingpc.backup_task import (
        backup_task_status,
        register_backup_task,
        unregister_backup_task,
    )

    if args.action == "install":
        result = register_backup_task(_cfg(args), config_path=os.path.abspath(args.config))
    elif args.action == "remove":
        result = unregister_backup_task()
    else:
        result = backup_task_status()
    _print(result)
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable Faerie Fire backup and recovery")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, handler):
        item = sub.add_parser(name)
        item.add_argument("--config", default=os.path.join(ROOT, "config.toml"))
        item.set_defaults(handler=handler)
        return item

    command("status", _status)
    create = command("create", _create)
    create.add_argument("--reason", choices=("manual", "scheduled", "pre_restore"),
                        default="manual")
    command("scheduled", _scheduled)
    restore = command("restore", _restore)
    restore.add_argument("bundle")
    restore.add_argument("--yes", action="store_true",
                         help="confirm whole-profile replacement after validation")
    schedule = command("schedule", _schedule)
    schedule.add_argument("action", choices=("install", "remove", "status"))
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        # Exception text can contain user paths or parser input.  The CLI emits
        # a stable class-only code; detailed payloads are intentionally absent.
        print(f"backup_error:{type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
