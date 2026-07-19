from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import gui


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "livingpc" / "ui" / "memory.html"


def _config(tmp_path: Path):
    return SimpleNamespace(
        inference_surface_confidence=0.8,
        instance_backup_enabled=False,
        instance_backup_primary_dir="",
        instance_backup_secondary_dir="",
        instance_backup_include_blobs=False,
    )


def _module(monkeypatch, name: str, **values):
    module = types.ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_backup_controls_and_onboarding_restore_are_present():
    html = HTML_PATH.read_text(encoding="utf-8")
    assert html.index('id="onboard-restore-backup"') < html.index('id="onboard-key-input"')
    for element_id in (
        "settings-backup-status",
        "settings-backup-configure",
        "settings-backup-now",
        "settings-backup-restore",
        "settings-backup-open",
        "backup-setup-modal",
        "backup-restore-modal",
        "backup-restore-confirm",
    ):
        assert f'id="{element_id}"' in html
    assert 'type="password" id="backup-passphrase"' in html
    assert 'type="password" id="backup-passphrase-confirm"' in html
    assert 'type="password" id="backup-restore-passphrase"' in html
    assert "pywebview.api.backup_prepare_restore(passphrase)" in html
    assert "pywebview.api.backup_confirm_restore()" in html


def test_backup_page_clears_password_fields_immediately_after_bridge_calls():
    html = HTML_PATH.read_text(encoding="utf-8")
    configure = html[html.index("function saveBackupSetup"):
                     html.index("function resetBackupRestoreModal")]
    call = configure.index("pywebview.api.backup_configure(")
    wait = configure.index("bridgeTimeout(request")
    assert call < configure.index("$('backup-passphrase').value='';") < wait
    assert call < configure.index("$('backup-passphrase-confirm').value='';") < wait
    assert "localStorage" not in configure

    restore = html[html.index("function prepareBackupRestore"):
                   html.index("function confirmBackupRestore")]
    call = restore.index("pywebview.api.backup_prepare_restore(passphrase)")
    wait = restore.index("bridgeTimeout(request")
    assert call < restore.index("$('backup-restore-passphrase').value='';") < wait
    assert "localStorage" not in restore


def test_backup_bridge_configures_and_creates_without_exposing_paths_or_secrets(
        tmp_path, monkeypatch):
    primary = tmp_path / "portable-primary"
    secondary = tmp_path / "portable-secondary"
    primary.mkdir(); secondary.mkdir()
    selected = iter(([str(primary)], [str(secondary)]))

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return next(selected)

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FOLDER_DIALOG="folder"))
    calls = {}

    @dataclass
    class Result:
        ok: bool = True
        enabled: bool = True
        token: str = "must-not-cross-the-bridge"
        passphrase: str = "must-not-cross-the-bridge"

    def configure(cfg, **kwargs):
        calls["configure"] = kwargs
        return Result()

    def create(cfg, *, reason):
        calls["reason"] = reason
        return Result()

    _module(
        monkeypatch,
        "livingpc.instance_backup",
        configure_instance_backup=configure,
        create_instance_backup=create,
    )
    _module(
        monkeypatch,
        "livingpc.backup_task",
        register_backup_task=lambda _cfg: SimpleNamespace(ok=True, installed=True),
    )
    api = gui.GuiApi(cfg=_config(tmp_path))
    api._window = Window()

    primary_choice = api.backup_choose_primary()
    secondary_choice = api.backup_choose_secondary()
    assert primary_choice == {"ok": True, "label": primary.name}
    assert secondary_choice == {"ok": True, "label": secondary.name}
    assert str(primary) not in repr(primary_choice)

    configured = api.backup_configure("a long recovery phrase", "a long recovery phrase")
    assert configured["ok"] is True
    assert "token" not in configured and "passphrase" not in configured
    assert calls["configure"]["primary_dir"] == str(primary)
    assert calls["configure"]["secondary_dir"] == str(secondary)
    assert calls["configure"]["passphrase"] == "a long recovery phrase"
    assert api.cfg.instance_backup_enabled is True

    created = api.backup_now()
    assert created["ok"] is True and "token" not in created
    assert calls["reason"] == "user_requested"


def test_restore_bridge_stages_privately_and_only_requests_close_after_confirmation(
        tmp_path, monkeypatch):
    archive = tmp_path / "profile.ffbackup"
    archive.write_bytes(b"bundle")
    discarded = []

    @dataclass
    class Prepared:
        ok: bool
        token: str
        verified: bool = True

    _module(
        monkeypatch,
        "livingpc.instance_backup",
        inspect_backup=lambda _path: {"ok": True, "format_version": 1},
        prepare_restore=lambda _cfg, _path, _pass: Prepared(True, "opaque-restore-token"),
        discard_prepared_restore=lambda token: discarded.append(token),
    )
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG="open"))

    class Window:
        destroyed = False

        def create_file_dialog(self, *_args, **_kwargs):
            return [str(archive)]

        def destroy(self):
            self.destroyed = True

    class ImmediateTimer:
        def __init__(self, _delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    monkeypatch.setattr(gui.threading, "Timer", ImmediateTimer)
    api = gui.GuiApi(cfg=_config(tmp_path))
    api._window = Window()

    chosen = api.backup_choose_restore()
    assert chosen["ok"] is True and chosen["label"] == archive.name
    assert str(archive.parent) not in repr(chosen)
    prepared = api.backup_prepare_restore("recovery phrase")
    assert prepared["prepared"] is True and "token" not in prepared
    assert api._restore_apply_requested is False
    assert api._window.destroyed is False

    confirmed = api.backup_confirm_restore()
    assert confirmed == {"ok": True, "closing": True}
    assert api._restore_apply_requested is True
    assert api._window.destroyed is True
    assert discarded == []


def test_gui_runtime_delegates_restore_only_after_webview_returns():
    source = (ROOT / "gui.py").read_text(encoding="utf-8")
    start = source.index("webview.start()")
    stop = source.index("backup_runtime.stop()", start)
    helper = source.index('"tools", "apply_restore.py"', stop)
    parent_wait = source.index('"--parent-pid", str(os.getpid())', helper)
    assert start < stop < helper < parent_wait
    assert "apply_prepared_restore(api.cfg, token)" not in source[stop:]
    assert "BackupRuntime(api.cfg)" in source
