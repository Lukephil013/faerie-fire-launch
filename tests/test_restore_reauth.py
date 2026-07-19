from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import gui
from livingpc import onboarding


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "livingpc" / "ui" / "memory.html"


def _load_restore_helper():
    spec = importlib.util.spec_from_file_location(
        "restore_reauth_helper", ROOT / "tools" / "apply_restore.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _config():
    return SimpleNamespace(
        profile="launch",
        inference_surface_confidence=0.8,
        companion_backend="claude",
        inference_backend="claude",
        memory_db_path="must-not-open.db",
    )


def test_restore_auth_marker_drops_old_local_and_inherited_credentials(
        tmp_path, monkeypatch):
    key_file = tmp_path / "api_key.secret"
    marker = tmp_path / ".restore_auth_required"
    key_file.write_bytes(b"old-machine-key")
    monkeypatch.setattr(onboarding, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(onboarding, "_KEY_FILE", str(key_file))
    monkeypatch.setattr(onboarding, "_RESTORE_AUTH_MARKER", str(marker))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-anthropic")
    monkeypatch.setenv("NOTION_API_KEY", "inherited-notion")

    onboarding.mark_restore_auth_required()

    assert marker.read_text(encoding="utf-8") == "1"
    assert not key_file.exists()
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "NOTION_API_KEY" not in os.environ

    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-again")
    monkeypatch.setenv("NOTION_API_KEY", "inherited-again")
    assert onboarding.apply_stored_key() is False
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "NOTION_API_KEY" not in os.environ

    onboarding.clear_restore_auth_required()
    assert not marker.exists()


def test_bootstrap_and_bridge_finish_restore_auth_without_replacing_soul(monkeypatch):
    state = {"required": True, "saved": False}
    monkeypatch.setattr(onboarding, "restore_auth_required", lambda: state["required"])
    monkeypatch.setattr(onboarding, "is_complete", lambda: True)
    monkeypatch.setattr(onboarding, "validate_api_key", lambda _key: (True, ""))
    monkeypatch.setattr(
        onboarding, "save_api_key", lambda _key: state.__setitem__("saved", True))
    monkeypatch.setattr(onboarding, "has_stored_key", lambda: state["saved"])
    monkeypatch.setattr(
        onboarding, "clear_restore_auth_required",
        lambda: state.__setitem__("required", False),
    )
    api = gui.GuiApi(cfg=_config())

    bootstrap = api.app_bootstrap()
    assert bootstrap["needs_onboarding"] is True
    assert bootstrap["restore_auth_required"] is True

    saved = api.onboarding_save_key("sk-ant-new-local-key")
    assert saved == {"ok": True, "restore_auth_completed": True}
    assert state == {"required": False, "saved": True}
    assert api.onboarding_finish_restore_auth() == {"ok": True}

    state["required"] = True
    blocked = api.onboarding_create_soul("Replacement", "Must not be written")
    assert blocked["ok"] is False
    assert "already has a Soul" in blocked["message"]


def test_restore_helper_marks_auth_required_and_relaunches_with_clean_environment(
        monkeypatch):
    helper = _load_restore_helper()
    events = []
    fake_config = types.ModuleType("livingpc.config")
    fake_config.load = lambda _path: SimpleNamespace()
    fake_backup = types.ModuleType("livingpc.instance_backup")
    fake_backup.apply_prepared_restore = (
        lambda _cfg, token: events.append(("apply", token)) or SimpleNamespace(ok=True))
    fake_backup.discard_prepared_restore = (
        lambda _token, _cfg: events.append(("discard", _token)))
    monkeypatch.setitem(sys.modules, "livingpc.config", fake_config)
    monkeypatch.setitem(sys.modules, "livingpc.instance_backup", fake_backup)
    monkeypatch.setattr(helper, "_wait_for_exit", lambda _pid: True)
    monkeypatch.setattr(onboarding, "mark_complete", lambda: events.append("complete"))
    monkeypatch.setattr(
        onboarding, "mark_restore_auth_required", lambda: events.append("fresh-auth"))
    monkeypatch.setattr(
        helper, "_relaunch",
        lambda view, *, fresh_auth_required=False:
            events.append(("relaunch", view, fresh_auth_required)),
    )

    code = helper.main([
        "--token", "opaque", "--parent-pid", "123", "--config", "config.toml",
        "--view", "self",
    ])

    assert code == 0
    assert events == [
        ("apply", "opaque"), "complete", "fresh-auth", ("relaunch", "self", True),
    ]


def test_restore_helper_relaunch_does_not_inherit_external_credentials(monkeypatch):
    helper = _load_restore_helper()
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-anthropic")
    monkeypatch.setenv("NOTION_API_KEY", "old-notion")
    monkeypatch.setattr(helper.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))

    helper._relaunch("self", fresh_auth_required=True)

    environment = calls[0][1]["env"]
    assert "ANTHROPIC_API_KEY" not in environment
    assert "NOTION_API_KEY" not in environment


def test_restore_auth_ui_saves_and_exits_before_any_soul_step():
    html = HTML_PATH.read_text(encoding="utf-8")
    assert 'id="onboard-key-title"' in html
    assert 'id="onboard-restore-card"' in html
    assert "Notion and browser integrations stay paused" in html
    assert "restoreAuthMode=!!info.restore_auth_required" in html
    assert "return restoreAuthMode?'key'" in html

    key_start = html.index("const keyBtn=$('onboard-key-continue')")
    key_end = html.index("const skipBtn=$('onboard-skip-btn')", key_start)
    key_handler = html[key_start:key_end]
    restore_guard = key_handler.index("if(restoreAuthMode)")
    finish = key_handler.index("pywebview.api.onboarding_finish_restore_auth()")
    close = key_handler.index("closeOnboardingAndEnter()")
    soul = key_handler.index("onboardShowStep('soul')")
    assert restore_guard < finish < close < soul

    skip_end = html.index("const soulBtn=$('onboard-soul-continue')", key_end)
    skip_handler = html[key_end:skip_end]
    assert skip_handler.index("if(restoreAuthMode)") < skip_handler.index(
        "onboardShowStep('soul')")
    assert "closeOnboardingAndEnter()" in skip_handler

