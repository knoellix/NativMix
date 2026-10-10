"""Unit tests for ConfigManager crossfader persistence fields (Task 4)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils.config_manager import ConfigManager


def _make_config(tmp_config_path: Path, tmp_profiles_dir: Path) -> ConfigManager:
    return ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)


# ── cross_side (per-channel) ────────────────────────────────────────────────


def test_get_cross_side_defaults_to_none(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    assert cfg.get_cross_side(0) == "none"


def test_set_cross_side_normalizes_value(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_cross_side(0, "A")
    assert cfg.get_cross_side(0) == "a"
    cfg.set_cross_side(0, "bogus")
    assert cfg.get_cross_side(0) == "none"


def test_set_cross_side_forces_none_on_usb_control_channel(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_usb_channel_index(1)
    cfg.set_cross_side(1, "b")
    assert cfg.get_cross_side(1) == "none"


# ── crossfader_enabled ───────────────────────────────────────────────────────


def test_crossfader_enabled_default_false(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    assert cfg.get_crossfader_enabled() is False


def test_set_crossfader_enabled(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_enabled(True)
    assert cfg.get_crossfader_enabled() is True


# ── crossfader_position ──────────────────────────────────────────────────────


def test_crossfader_position_default(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    assert cfg.get_crossfader_position() == 0.5


def test_set_crossfader_position_clamps(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_position(1.5)
    assert cfg.get_crossfader_position() == 1.0
    cfg.set_crossfader_position(-0.5)
    assert cfg.get_crossfader_position() == 0.0
    cfg.set_crossfader_position(0.3)
    assert cfg.get_crossfader_position() == 0.3


# ── crossfader_usb_channel_index ─────────────────────────────────────────────


def test_crossfader_usb_channel_index_default_none(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    assert cfg.get_crossfader_usb_channel_index() is None


def test_set_crossfader_usb_channel_index_clears_apps_and_cross_side(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_cross_side(2, "a")
    cfg.set_app_names(2, ["Spotify"])
    cfg.set_crossfader_usb_channel_index(2)
    assert cfg.get_crossfader_usb_channel_index() == 2
    assert cfg.get_cross_side(2) == "none"
    assert cfg.get_app_names(2) == []


def test_set_crossfader_usb_channel_index_none_clears(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_usb_channel_index(1)
    cfg.set_crossfader_usb_channel_index(None)
    assert cfg.get_crossfader_usb_channel_index() is None


# ── crossfader MIDI binding ──────────────────────────────────────────────────


def test_crossfader_midi_binding_default(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    assert cfg.get_crossfader_midi_binding() == (None, 0)


def test_set_crossfader_midi_binding(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_midi_binding(42, midi_channel=3)
    assert cfg.get_crossfader_midi_binding() == (42, 3)


def test_set_crossfader_midi_binding_clamps_channel(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_midi_binding(10, midi_channel=99)
    cc, ch = cfg.get_crossfader_midi_binding()
    assert cc == 10
    assert ch == 15


# ── apply_profile mirrors crossfader state ──────────────────────────────────


def test_apply_profile_mirrors_crossfader_fields(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    profile = {
        "id": "profile-x",
        "channels": [],
        "crossfader_enabled": True,
        "crossfader_position": 0.75,
        "crossfader_usb_channel_index": 3,
        "crossfader_midi_cc": 7,
        "crossfader_midi_channel": 1,
    }
    cfg.apply_profile(profile)
    assert cfg.get_crossfader_enabled() is True
    assert cfg.get_crossfader_position() == 0.75
    assert cfg.get_crossfader_usb_channel_index() == 3
    assert cfg.get_crossfader_midi_binding() == (7, 1)


def test_apply_profile_defaults_missing_crossfader_fields(qtbot, tmp_config_path, tmp_profiles_dir):
    """Profiles without crossfader fields (pre-feature) apply safe defaults."""
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_enabled(True)  # dirty the mirror first
    profile = {"id": "profile-y", "channels": []}
    cfg.apply_profile(profile)
    assert cfg.get_crossfader_enabled() is False
    assert cfg.get_crossfader_position() == 0.5


def test_get_crossfader_state_returns_copy(qtbot, tmp_config_path, tmp_profiles_dir):
    cfg = _make_config(tmp_config_path, tmp_profiles_dir)
    cfg.set_crossfader_enabled(True)
    state = cfg.get_crossfader_state()
    assert state["crossfader_enabled"] is True
    state["crossfader_enabled"] = False
    # Mutating the returned dict must not affect internal state.
    assert cfg.get_crossfader_enabled() is True
