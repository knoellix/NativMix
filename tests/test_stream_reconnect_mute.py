"""Unit tests for channel mute restore on stream appear (issue #29)."""

from __future__ import annotations

from unittest.mock import MagicMock

from nativmix.audio.manager import _AudioListenerThread
from nativmix.utils.config_manager import ConfigManager


def test_desired_channel_mute_unmapped_is_false(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    thread = _AudioListenerThread(config=cfg)
    assert thread._desired_channel_mute("UnmappedApp") is False


def test_desired_channel_mute_reads_channel_state(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Minecraft"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {0: {"vol": 0.4, "muted": True}}
    assert thread._desired_channel_mute("Minecraft") is True
    with thread._states_lock:
        thread.channel_states[0]["muted"] = False
    assert thread._desired_channel_mute("Minecraft") is False


def test_dedupe_state_includes_app_name() -> None:
    """Late identity resolve must change the dedupe key (volume/mute alone is not enough)."""
    a = (0.5, False, "Unknown")
    b = (0.5, False, "Minecraft")
    assert a != b


def test_apply_post_reflex_mute_honors_channel(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Spotify"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {0: {"muted": True, "apps": ["Spotify"]}}

    pulse = MagicMock()
    info = MagicMock()
    info.index = 42
    info.app_name = "Spotify"

    thread._apply_post_reflex_mute(pulse, info)
    pulse.sink_input_mute.assert_called_once_with(42, mute=True)


def test_other_apps_catch_all_resolves_unassigned(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Spotify"])
    cfg.set_app_names(1, ["Other Apps"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {
            0: {"vol": 0.8, "muted": False, "apps": ["Spotify"]},
            1: {"vol": 0.3, "muted": True, "apps": ["Other Apps"]},
        }

    assert thread._resolve_target_channel("Spotify") == 0
    assert thread._resolve_target_channel("Minecraft") == 1
    assert thread._desired_channel_mute("Minecraft") is True
    assert thread._resolve_target_channel("System Master") is None


def test_explicit_mapping_beats_other_apps(tmp_path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_app_names(0, ["Minecraft"])
    cfg.set_app_names(1, ["Other Apps"])
    thread = _AudioListenerThread(config=cfg)
    with thread._states_lock:
        thread.channel_states = {
            0: {"apps": ["Minecraft"], "muted": False},
            1: {"apps": ["Other Apps"], "muted": True},
        }
    assert thread._resolve_target_channel("Minecraft") == 0
    assert thread._desired_channel_mute("Minecraft") is False
