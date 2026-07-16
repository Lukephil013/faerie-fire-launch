"""Durable, bounded preferences for the desktop UI.

The embedded webview's localStorage is useful as a fast cache but is not a
reliable source of truth across full application restarts. These preferences
live in the existing private memory database and contain no prompt payloads.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from . import crypto
from .db import connect as db_connect


_ALLOWED_SKINS = {"classic", "dark", "cat", "knight", "meditate"}
_ALLOWED_KEYS = {
    "mascot_skin", "details_open", "growth_map_layout",
    "soul_calibration_enabled",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_preference (
    preference_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated(key: str, value):
    key = str(key or "").strip()
    if key not in _ALLOWED_KEYS:
        raise ValueError("unsupported UI preference")
    if key == "mascot_skin":
        value = str(value or "")
        if value not in _ALLOWED_SKINS:
            raise ValueError("invalid mascot skin")
        return value
    if key == "soul_calibration_enabled":
        if not isinstance(value, bool):
            raise ValueError("soul_calibration_enabled must be a boolean")
        return value
    if key == "growth_map_layout":
        if not isinstance(value, dict):
            raise ValueError("growth_map_layout must be an object")
        positions = value.get("positions") or {}
        views = value.get("views") or {}
        if not isinstance(positions, dict) or not isinstance(views, dict):
            raise ValueError("growth map positions and views must be objects")
        clean_positions = {}
        for raw_id, raw_position in list(positions.items())[-1000:]:
            node_id = str(raw_id or "").strip()
            if not node_id.isdigit() or not isinstance(raw_position, dict):
                continue
            try:
                x, y = float(raw_position.get("x")), float(raw_position.get("y"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                clean_positions[node_id] = {
                    "x": max(-100000.0, min(100000.0, x)),
                    "y": max(-100000.0, min(100000.0, y)),
                }
        clean_views = {}
        for raw_key, raw_view in list(views.items())[-16:]:
            view_key = str(raw_key or "").strip()[:80]
            if not view_key or not isinstance(raw_view, dict):
                continue
            try:
                scale = float(raw_view.get("scale", 1))
                pan_x = float(raw_view.get("panX", 0))
                pan_y = float(raw_view.get("panY", 0))
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(number) for number in (scale, pan_x, pan_y)):
                clean_views[view_key] = {
                    "scale": max(.15, min(8.0, scale)),
                    "panX": max(-100000.0, min(100000.0, pan_x)),
                    "panY": max(-100000.0, min(100000.0, pan_y)),
                }
        return {"positions": clean_positions, "views": clean_views}
    if not isinstance(value, dict):
        raise ValueError("details_open must be an object")
    cleaned = {}
    for raw_key, raw_value in list(value.items())[-1000:]:
        item_key = str(raw_key or "").strip()[:240]
        if item_key:
            cleaned[item_key] = bool(raw_value)
    return cleaned


class UiPreferenceStore:
    def __init__(self, db_path: str):
        self.conn = db_connect(db_path)
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_all(self) -> dict:
        result = {}
        for key, encrypted in self.conn.execute(
                "SELECT preference_key,value_json FROM ui_preference"):
            if key not in _ALLOWED_KEYS:
                continue
            try:
                result[key] = _validated(
                    key, json.loads(crypto.dec(encrypted) or "null"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def set(self, key: str, value) -> None:
        key = str(key or "").strip()
        value = _validated(key, value)
        encoded = crypto.enc(json.dumps(value, ensure_ascii=False,
                                        separators=(",", ":")))
        self.conn.execute(
            "INSERT INTO ui_preference (preference_key,value_json,updated_at) "
            "VALUES (?,?,?) ON CONFLICT(preference_key) DO UPDATE SET "
            "value_json=excluded.value_json,updated_at=excluded.updated_at",
            (key, encoded, _now()),
        )
        self.conn.commit()
