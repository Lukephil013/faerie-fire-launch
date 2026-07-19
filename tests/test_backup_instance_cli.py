from __future__ import annotations

import dataclasses
import importlib.util
import os
from types import SimpleNamespace
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "backup_instance_cli", os.path.join(ROOT, "tools", "backup_instance.py"))
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


@dataclasses.dataclass
class _Result:
    ok: bool = True
    token: str = "must-not-print"
    error_code: str = ""


def test_plain_never_serializes_secret_fields():
    value = cli._plain({"token": "x", "passphrase": "y", "nested": _Result()})
    assert "token" not in value
    assert "passphrase" not in value
    assert "token" not in value["nested"]


def test_scheduled_skips_when_not_due(monkeypatch, capsys):
    import livingpc.instance_backup as module

    monkeypatch.setattr(module, "backup_status",
                        lambda _cfg: SimpleNamespace(ok=True, due=False))
    create = mock.Mock()
    monkeypatch.setattr(module, "create_instance_backup", create)
    monkeypatch.setattr(cli, "load", lambda _path: object())
    args = SimpleNamespace(config="config.toml")
    assert cli._scheduled(args) == 0
    create.assert_not_called()
    assert "not_due" in capsys.readouterr().out


def test_restore_parser_has_no_passphrase_argument():
    parser = cli.build_parser()
    args = parser.parse_args(["restore", "sample.ffbackup", "--yes"])
    assert not hasattr(args, "passphrase")
    assert args.bundle == "sample.ffbackup"


def test_task_command_shape():
    args = cli.build_parser().parse_args(["schedule", "status"])
    assert args.action == "status"
