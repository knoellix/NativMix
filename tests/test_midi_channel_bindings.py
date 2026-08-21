"""Tests for MIDI channel (0-15) + CC bindings (#30)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402

from nativmix.utils.config_manager import ConfigManager  # noqa: E402


def test_midi_mappings_include_midi_channel(tmp_config_path, tmp_profiles_dir) -> None:
    channels = make_profile(channel_count=3)["channels"]
    channels[1]["is_midi"] = True
    channels[1]["midi_cc"] = 7
    channels[1]["midi_channel"] = 0
    channels[2]["is_midi"] = True
    channels[2]["midi_cc"] = 7
    channels[2]["midi_channel"] = 3
    profile = make_profile(channel_count=3, channels=channels)
    write_profile(tmp_profiles_dir, profile)

    tmp_config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "port": None,
                    "auto_search_device": True,
                    "num_channels": 3,
                    "input_mode": "hybrid",
                    "midi_device": "",
                    "midi_channel_count": 2,
                    "baud_rate": 9600,
                },
                "settings": {
                    "threshold": 0.01,
                    "transparency": True,
                    "compact_mode": False,
                    "stay_open": False,
                    "show_invert_option": False,
                    "debug_logging": False,
                    "midi_fader_feedback": False,
                },
            }
        )
    )

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)

    assert config.get_all_midi_mappings() == {(0, 7): 1, (3, 7): 2}
    assert config.get_midi_channel(1) == 0
    assert config.get_midi_channel(2) == 3


def test_missing_midi_channel_defaults_to_zero(tmp_config_path, tmp_profiles_dir) -> None:
    channels = make_profile(channel_count=2)["channels"]
    channels[1]["is_midi"] = True
    channels[1]["midi_cc"] = 11
    # no midi_channel field — legacy profile
    profile = make_profile(channel_count=2, channels=channels)
    write_profile(tmp_profiles_dir, profile)

    tmp_config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "port": None,
                    "auto_search_device": True,
                    "num_channels": 2,
                    "input_mode": "hybrid",
                    "midi_device": "",
                    "midi_channel_count": 1,
                    "baud_rate": 9600,
                },
                "settings": {"threshold": 0.01},
            }
        )
    )

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    assert config.get_midi_channel(1) == 0
    assert config.get_all_midi_mappings() == {(0, 11): 1}


def test_set_midi_cc_stores_midi_channel(tmp_config_path, tmp_profiles_dir) -> None:
    profile = make_profile(channel_count=2)
    profile["channels"][1]["is_midi"] = True
    write_profile(tmp_profiles_dir, profile)
    tmp_config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "num_channels": 2,
                    "input_mode": "hybrid",
                    "midi_channel_count": 1,
                },
                "settings": {},
            }
        )
    )
    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)
    config.set_midi_cc(1, 20, midi_channel=5)
    assert config.get_midi_cc(1) == 20
    assert config.get_midi_channel(1) == 5
    assert config.get_all_midi_mappings() == {(5, 20): 1}
