import os
import tempfile

import pytest

from livingpc import config as config_module


def _write(folder: str, body: str) -> str:
    path = os.path.join(folder, "config.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def test_windows_path_as_literal_string_loads():
    """A Windows backup path in a single-quoted (literal) TOML string keeps its
    backslashes and loads cleanly."""
    backslash = chr(92)
    path = "C:" + backslash + "Users" + backslash + "you" + backslash + "Backups"
    with tempfile.TemporaryDirectory() as folder:
        cfg_path = _write(folder, f"instance_backup_primary_dir = '{path}'\n")
        cfg = config_module.load(cfg_path)
        # _project_path leaves an absolute path untouched.
        assert cfg.instance_backup_primary_dir == path


def test_update_config_writes_a_parseable_windows_path():
    """Regression: _update_config substituted an existing key with a plain
    re.sub replacement, whose backslash reprocessing turned a valid "C:\\\\..."
    value back into an invalid single-backslash path that broke launch. The
    written file must reload cleanly with the path intact."""
    from livingpc.instance_backup import _update_config

    backslash = chr(92)
    path = backslash.join(["C:", "Users", "you", "Backups"])
    with tempfile.TemporaryDirectory() as folder:
        # Pre-existing key forces the substitution branch (the buggy path).
        cfg_path = _write(folder, 'instance_backup_primary_dir = "old"\n')
        cfg = config_module.load(cfg_path)
        _update_config(cfg, {"instance_backup_primary_dir": path})
        reloaded = config_module.load(cfg_path)
        assert reloaded.instance_backup_primary_dir == path


def test_windows_path_in_basic_string_raises_actionable_error():
    """The classic failure that stopped Faerie Fire from launching: a raw
    Windows path in a double-quoted string, where \\U is an invalid escape.
    The loader must fail with a clear, fixable message rather than silently."""
    backslash = chr(92)
    body = 'dir = "C:' + backslash + "Users" + backslash + 'you"\n'
    with tempfile.TemporaryDirectory() as folder:
        cfg_path = _write(folder, body)
        with pytest.raises(ValueError) as excinfo:
            config_module.load(cfg_path)
        message = str(excinfo.value)
        assert "config.toml is not valid TOML" in message
        assert "single quotes" in message
