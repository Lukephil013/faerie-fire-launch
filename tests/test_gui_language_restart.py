from __future__ import annotations

import os
import subprocess
import sys

import gui


class _Window:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class _ImmediateTimer:
    def __init__(self, _delay, callback):
        self.callback = callback

    def start(self):
        self.callback()


def _api():
    api = gui.GuiApi.__new__(gui.GuiApi)
    api.initial_view = "self"
    api._window = _Window()
    return api


def test_language_restart_persists_then_launches_and_closes(monkeypatch):
    api = _api()
    saved = []
    launched = []
    monkeypatch.setattr(gui, "app_language", lambda: "en")
    monkeypatch.setattr(gui, "set_app_language", saved.append)
    monkeypatch.setattr(gui.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(gui.subprocess, "Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    result = api.app_restart_language("ko", "investigations")

    assert result == {"ok": True, "language": "ko", "restarting": True}
    assert saved == ["ko"]
    assert launched
    args, kwargs = launched[0]
    assert args == [sys.executable, os.path.abspath(gui.__file__), "--view", "curiosity"]
    assert kwargs["cwd"] == os.path.dirname(os.path.abspath(gui.__file__))
    assert api._window.destroyed is True


def test_language_restart_rolls_back_when_relaunch_fails(monkeypatch):
    api = _api()
    saved = []
    monkeypatch.setattr(gui, "app_language", lambda: "en")
    monkeypatch.setattr(gui, "set_app_language", saved.append)

    def fail(_args, **_kwargs):
        raise OSError("cannot relaunch")

    monkeypatch.setattr(gui.subprocess, "Popen", fail)
    result = api.app_restart_language("ko", "self")

    assert result["ok"] is False
    assert "cannot relaunch" in result["message"]
    assert saved == ["ko", "en"]
    assert api._window.destroyed is False


def test_language_restart_rejects_unknown_language():
    result = _api().app_restart_language("fr", "self")
    assert result["ok"] is False
    assert "Language must be" in result["message"]
