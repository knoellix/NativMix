import json
from pathlib import Path

import pytest


def _load_manager(config_path: Path, profiles_dir: Path):
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from nativmix.utils.config_manager import ConfigManager

    return ConfigManager(config_path=config_path, profiles_dir=profiles_dir)


def _v6_config(num_channels: int = 5) -> dict:
    """Minimal v6 config with channels[] included."""
    return {
        "version": 6,
        "hardware": {
            "port": None,
            "auto_search_device": True,
            "num_channels": num_channels,
            "input_mode": "usb",
            "midi_device": "",
            "midi_channel_count": 0,
            "baud_rate": 9600,
        },
        "settings": {
            "threshold": 0.01,
            "invert_map": [False] * num_channels,
            "v_sink_map": [False] * num_channels,
            "transparency": True,
            "compact_mode": False,
            "stay_open": False,
            "show_invert_option": False,
            "debug_logging": False,
        },
        "channels": [
            {
                "index": i,
                "label": None,
                "is_midi": False,
                "app_names": ["spotify"] if i == 0 else [],
                "midi_cc": None,
                "midi_mute_cc": None,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "hardware_id": None,
                "volume": 0.8 if i == 0 else 1.0,
            }
            for i in range(num_channels)
        ],
    }


def test_migration_creates_profile_1(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_migration_preserves_channel_data(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    p = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert p["channels"][0]["app_names"] == ["spotify"]
    assert p["channels"][0]["volume"] == pytest.approx(0.8)


def test_migration_config_has_no_channels_key(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert "channels" not in saved


def test_migration_sets_active_profile(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert saved["active_profile"] == "profile-1"


def test_migration_sets_current_config_version(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    assert saved["version"] == 8
    assert saved["settings"].get("check_for_updates") is True
    assert saved["settings"].get("update_dismissed_version") is None


def test_fresh_install_creates_profile_1(tmp_config_path, tmp_profiles_dir):
    """Fresh install (no config file) → profile-1 created from defaults."""
    _load_manager(tmp_config_path, tmp_profiles_dir)
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_migration_removes_invert_and_vsink_maps(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    saved = json.loads(tmp_config_path.read_text())
    settings = saved.get("settings", {})
    assert "invert_map" not in settings
    assert "v_sink_map" not in settings


def test_migration_profile_channel_count_matches_channels(tmp_config_path, tmp_profiles_dir):
    tmp_config_path.write_text(json.dumps(_v6_config(5)))
    _load_manager(tmp_config_path, tmp_profiles_dir)
    p = json.loads((tmp_profiles_dir / "profile-1.json").read_text())
    assert p["channel_count"] == len(p["channels"])
    assert p["channel_count"] == 5
