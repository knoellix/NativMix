"""Unit tests for Windows WASAPI Other Apps catch-all and session cache (#32)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nativmix.audio.wasapi_manager import WasapiManager
from nativmix.utils.config_manager import ConfigManager


def _mgr(tmp_path) -> WasapiManager:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    return WasapiManager(config=cfg)


def test_resolve_other_apps_catch_all(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_app_names(1, ["Other Apps"])

    assert mgr._resolve_target_channel("Spotify") == 0
    assert mgr._resolve_target_channel("Minecraft") == 1
    assert mgr._resolve_target_channel("System Master") is None
    # The catch-all label itself resolves to its channel (same as Linux).
    assert mgr._resolve_target_channel("Other Apps") == 1


def test_resolve_explicit_mapping_beats_other_apps(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    mgr._config.set_app_names(0, ["Minecraft"])
    mgr._config.set_app_names(1, ["Other Apps"])
    assert mgr._resolve_target_channel("Minecraft") == 0


def test_apply_mute_other_apps_skips_mapped(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_app_names(1, ["Other Apps"])

    spotify = MagicMock(name="spotify_session")
    game = MagicMock(name="game_session")
    named = [(spotify, "Spotify"), (game, "Minecraft")]

    with (
        patch.object(mgr, "_iter_named_sessions", return_value=named),
        patch("nativmix.audio.wasapi_manager._set_session_mute") as set_mute,
    ):
        mgr._apply_mute_by_name("Other Apps", True)

    set_mute.assert_called_once_with(game, True)


def test_apply_volume_other_apps_skips_mapped(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_app_names(1, ["Other Apps"])

    spotify = MagicMock()
    game = MagicMock()
    named = [(spotify, "Spotify"), (game, "Chrome")]

    with (
        patch.object(mgr, "_iter_named_sessions", return_value=named),
        patch("nativmix.audio.wasapi_manager._set_session_volume") as set_vol,
    ):
        mgr._apply_volume_by_name("Other Apps", 0.4)

    set_vol.assert_called_once_with(game, 0.4)


def test_toggle_mute_other_apps_channel(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    mgr._config.set_app_names(0, ["Other Apps"])
    game = MagicMock()

    with (
        patch.object(mgr, "_iter_named_sessions", return_value=[(game, "Game")]),
        patch("nativmix.audio.wasapi_manager._set_session_mute") as set_mute,
    ):
        mgr.toggle_mute(0)

    assert mgr.is_channel_muted(0) is True
    set_mute.assert_called_once_with(game, True)


def test_session_cache_reuses_list(tmp_path) -> None:
    mgr = _mgr(tmp_path)
    sessions = [MagicMock(), MagicMock()]
    with patch("nativmix.audio.wasapi_manager._get_sessions", return_value=sessions) as get_sessions:
        first = mgr._cached_sessions()
        second = mgr._cached_sessions()
        assert first is second
        assert get_sessions.call_count == 1
        mgr._invalidate_session_cache()
        third = mgr._cached_sessions()
        assert third is sessions
        assert get_sessions.call_count == 2
