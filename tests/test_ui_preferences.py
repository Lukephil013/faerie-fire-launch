from livingpc.ui_preferences import UiPreferenceStore
from livingpc.config import Config
from gui import GuiApi


def test_visual_preferences_survive_store_reopen(tmp_path):
    db = str(tmp_path / "memory.db")
    collapsed = {
        "goal-9::What did this experiment teach?::0": False,
        "curiosity-2::Evidence::1": True,
    }
    store = UiPreferenceStore(db)
    store.set("mascot_skin", "cat")
    store.set("details_open", collapsed)
    store.set("soul_calibration_enabled", False)
    store.close()

    reopened = UiPreferenceStore(db)
    try:
        assert reopened.get_all() == {
            "mascot_skin": "cat", "details_open": collapsed,
            "soul_calibration_enabled": False}
    finally:
        reopened.close()


def test_visual_preferences_reject_unbounded_or_invalid_values(tmp_path):
    store = UiPreferenceStore(str(tmp_path / "memory.db"))
    try:
        for key, value in (("mascot_skin", "unknown"),
                           ("unbounded_key", "value"),
                           ("details_open", []),
                           ("soul_calibration_enabled", "false"),
                           ("growth_map_layout", [])):
            try:
                store.set(key, value)
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted invalid preference: {key}")
    finally:
        store.close()


def test_music_preferences_survive_reopen_and_clamp_and_validate(tmp_path):
    db = str(tmp_path / "memory.db")
    store = UiPreferenceStore(db)
    store.set("music_enabled", False)
    store.set("music_volume", 0.65)
    store.set("music_volume", 5)     # out of range -> clamped to 1.0
    store.close()

    reopened = UiPreferenceStore(db)
    try:
        got = reopened.get_all()
        assert got["music_enabled"] is False
        assert got["music_volume"] == 1.0
        reopened.set("music_volume", -3)   # clamps up to 0.0
        assert reopened.get_all()["music_volume"] == 0.0
        for key, value in (("music_enabled", "yes"), ("music_volume", "loud")):
            try:
                reopened.set(key, value)
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted invalid preference: {key}")
    finally:
        reopened.close()


def test_growth_map_layout_survives_restart_and_bounds_numeric_state(tmp_path):
    db = str(tmp_path / "memory.db")
    store = UiPreferenceStore(db)
    store.set("growth_map_layout", {
        "positions": {
            "12": {"x": 431.25, "y": -82.5},
            "13": {"x": 999999, "y": -999999},
            "not-a-node": {"x": 1, "y": 2},
        },
        "views": {
            "growth-map-tree": {"scale": 2.4, "panX": 125, "panY": -40},
            "bad": {"scale": "not-a-number", "panX": 0, "panY": 0},
        },
    })
    store.close()

    reopened = UiPreferenceStore(db)
    try:
        layout = reopened.get_all()["growth_map_layout"]
        assert layout["positions"]["12"] == {"x": 431.25, "y": -82.5}
        assert layout["positions"]["13"] == {"x": 100000.0, "y": -100000.0}
        assert "not-a-node" not in layout["positions"]
        assert layout["views"] == {
            "growth-map-tree": {"scale": 2.4, "panX": 125.0, "panY": -40.0}}
    finally:
        reopened.close()


def test_gui_bridge_reads_preferences_after_restart(tmp_path):
    cfg = Config(memory_db_path=str(tmp_path / "memory.db"))
    first = GuiApi(cfg)
    assert first.ui_preference_set("mascot_skin", "knight")["ok"]
    assert first.ui_preference_set("details_open", {"scope::panel::0": False})["ok"]
    assert first.ui_preference_set("soul_calibration_enabled", False)["ok"]
    assert first.ui_preference_set("growth_map_layout", {
        "positions": {"7": {"x": 10, "y": 20}},
        "views": {"growth-map-tree": {"scale": 1.5, "panX": 2, "panY": 3}},
    })["ok"]

    reopened = GuiApi(cfg).ui_preferences_get()

    assert reopened == {"ok": True, "preferences": {
        "mascot_skin": "knight",
        "details_open": {"scope::panel::0": False},
        "soul_calibration_enabled": False,
        "growth_map_layout": {
            "positions": {"7": {"x": 10.0, "y": 20.0}},
            "views": {"growth-map-tree": {
                "scale": 1.5, "panX": 2.0, "panY": 3.0}},
        },
    }}
    assert not first.ui_preference_set("mascot_skin", "unknown")["ok"]
