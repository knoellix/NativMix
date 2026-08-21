"""Tests for single volume MIDI Learn binding + mute LED helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402

from nativmix.hardware.midi import (  # noqa: E402
    _LED_HUE_MUTED,
    _LED_HUE_UNMUTED,
    _example_led_cc_for_mute,
)
from nativmix.utils.config_manager import ConfigManager  # noqa: E402


def _write_hybrid_config(tmp_config_path: Path, profile: dict, midi_channel_count: int = 1) -> None:
    tmp_config_path.write_text(
        json.dumps(
            {
                "version": 7,
                "active_profile": profile["id"],
                "hardware": {
                    "port": None,
                    "auto_search_device": True,
                    "num_channels": profile["channel_count"],
                    "input_mode": "hybrid",
                    "midi_device": "",
                    "midi_channel_count": midi_channel_count,
                    "baud_rate": 9600,
                },
                "settings": {"threshold": 0.01},
            }
        )
    )


def test_legacy_midi_cc_migrates_to_bindings(tmp_config_path, tmp_profiles_dir) -> None:
    channels = make_profile(channel_count=2)["channels"]
    channels[1]["is_midi"] = True
    channels[1]["midi_cc"] = 7
    channels[1]["midi_channel"] = 2
    profile = make_profile(channel_count=2, channels=channels)
    write_profile(tmp_profiles_dir, profile)
    _write_hybrid_config(tmp_config_path, profile)

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)

    assert config.get_midi_binding_count(1) == 1
    assert config.get_midi_binding(1, 0) == {"cc": 7, "midi_channel": 2}
    assert config.get_all_midi_mappings() == {(2, 7): 1}


def test_extra_bindings_truncated_to_one(tmp_config_path, tmp_profiles_dir) -> None:
    """Older multi-learn profiles keep only the first volume binding."""
    channels = make_profile(channel_count=2)["channels"]
    channels[1]["is_midi"] = True
    channels[1]["midi_bindings"] = [
        {"cc": 7, "midi_channel": 0},
        {"cc": 11, "midi_channel": 3},
    ]
    profile = make_profile(channel_count=2, channels=channels)
    write_profile(tmp_profiles_dir, profile)
    _write_hybrid_config(tmp_config_path, profile)

    config = ConfigManager(config_path=tmp_config_path, profiles_dir=tmp_profiles_dir)
    config.apply_profile(profile)

    assert config.get_midi_binding_count(1) == 1
    assert config.get_all_midi_mappings() == {(0, 7): 1}


def test_example_led_cc_mapping() -> None:
    assert _example_led_cc_for_mute(5) == 32
    assert _example_led_cc_for_mute(8) == 35
    assert _example_led_cc_for_mute(4) is None
    assert _LED_HUE_MUTED == 0
    assert _LED_HUE_UNMUTED == 42


def test_mute_feedback_queued(tmp_config_path, tmp_profiles_dir) -> None:
    from nativmix.hardware.midi import MidiThread

    thread = MidiThread(device_name="", input_mode="midi_only")
    thread.set_fader_feedback_enabled(True)
    thread.update_mute_mappings({(0, 5): 1})
    thread.request_mute_feedback([(1, True)])
    # Signal is queued async — call slot directly for unit test
    thread._queue_mute_feedback([(1, True)])
    with thread._feedback_lock:
        assert thread._pending_mute_feedback == [(1, True)]
