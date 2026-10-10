"""Unit tests for Task 5: effective (crossfader-adjusted) volume applied at the
backend volume-apply entry points, while stored base volume / GUI feedback
stay untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nativmix.audio.manager import PipeWireManager
from nativmix.audio.wasapi_manager import WasapiManager
from nativmix.utils.config_manager import ConfigManager


def _pw_mgr(tmp_path) -> PipeWireManager:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    return PipeWireManager(config=cfg)


def _wasapi_mgr(tmp_path) -> WasapiManager:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    return WasapiManager(config=cfg)


# ---------------------------------------------------------------------------
# _effective_for_channel (pure gain math wiring)
# ---------------------------------------------------------------------------


def test_effective_for_channel_disabled_passthrough(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.set_cross_side(0, "a")
    mgr._config.set_crossfader_position(0.0)
    mgr._config.set_crossfader_enabled(False)
    assert mgr._effective_for_channel(0, 0.8) == 0.8


def test_effective_for_channel_side_a_full_at_center_and_fades_right(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.set_cross_side(0, "a")
    mgr._config.set_crossfader_enabled(True)

    mgr._config.set_crossfader_position(0.0)
    assert mgr._effective_for_channel(0, 0.8) == 0.8

    mgr._config.set_crossfader_position(1.0)
    assert mgr._effective_for_channel(0, 0.8) == 0.0


def test_effective_for_channel_none_side_unaffected_by_crossfader(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.set_cross_side(0, "none")
    mgr._config.set_crossfader_enabled(True)
    mgr._config.set_crossfader_position(1.0)
    assert mgr._effective_for_channel(0, 0.8) == 0.8


def test_effective_for_channel_skips_usb_control_channel(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.set_crossfader_usb_channel_index(2)
    mgr._config.set_crossfader_enabled(True)
    mgr._config.set_crossfader_position(1.0)
    # Control channel never gets A/B gain applied, regardless of position.
    assert mgr._effective_for_channel(2, 0.8) == 0.8


# ---------------------------------------------------------------------------
# PipeWireManager.set_channel_volume: base stored + emitted, effective applied
# ---------------------------------------------------------------------------


def test_pipewire_set_channel_volume_applies_effective_but_emits_base(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_cross_side(0, "a")
    mgr._config.set_crossfader_enabled(True)
    mgr._config.set_crossfader_position(1.0)  # side A fully faded out -> gain 0.0

    emitted: list[tuple[int, float]] = []
    mgr.channel_volume_changed.connect(lambda ch, vol: emitted.append((ch, vol)))

    with patch.object(mgr, "_apply_volume_by_name") as apply_by_name:
        mgr.set_channel_volume(0, 0.8)

    apply_by_name.assert_called_once()
    args, _kwargs = apply_by_name.call_args
    assert args[0] == "Spotify"
    assert args[1] == 0.0  # effective volume, attenuated to silence

    assert mgr._config.get_channel_volume(0) == 0.8  # base persisted unchanged
    assert emitted == [(0, 0.8)]  # GUI/base feedback, not effective


# ---------------------------------------------------------------------------
# PipeWireManager.reapply_all_channel_volumes
# ---------------------------------------------------------------------------


def test_pipewire_reapply_all_channel_volumes_uses_effective_and_keeps_base(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.num_channels = 2
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_cross_side(0, "a")
    mgr._config.set_app_names(1, ["Discord"])
    mgr._config.set_cross_side(1, "b")
    mgr._config.set_crossfader_enabled(True)
    mgr._config.set_crossfader_position(1.0)  # A -> 0.0 gain, B -> 1.0 gain
    mgr._config.set_channel_volume(0, 0.8)
    mgr._config.set_channel_volume(1, 0.8)

    emitted: list[tuple[int, float]] = []
    mgr.channel_volume_changed.connect(lambda ch, vol: emitted.append((ch, vol)))

    with (
        patch.object(mgr, "_get_vol_pulse", return_value=MagicMock()),
        patch.object(mgr, "_apply_volume_by_name") as apply_by_name,
    ):
        mgr.reapply_all_channel_volumes()

    calls = {c.args[0]: c.args[1] for c in apply_by_name.call_args_list}
    assert calls["Spotify"] == 0.0
    assert calls["Discord"] == 0.8

    # Stored base volumes must stay untouched.
    assert mgr._config.get_channel_volume(0) == 0.8
    assert mgr._config.get_channel_volume(1) == 0.8
    # No GUI/base feedback signal — faders must not visibly move.
    assert emitted == []


# ---------------------------------------------------------------------------
# WasapiManager parity
# ---------------------------------------------------------------------------


def test_wasapi_apply_channel_volume_applies_effective_but_emits_base(tmp_path) -> None:
    mgr = _wasapi_mgr(tmp_path)
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_cross_side(0, "a")
    mgr._config.set_crossfader_enabled(True)
    mgr._config.set_crossfader_position(1.0)  # side A fully faded out -> gain 0.0

    emitted: list[tuple[int, float]] = []
    mgr.channel_volume_changed.connect(lambda ch, vol: emitted.append((ch, vol)))

    with patch.object(mgr, "_apply_volume_by_name") as apply_by_name:
        mgr._apply_channel_volume(0, 0.8)

    apply_by_name.assert_called_once_with("Spotify", 0.0)
    assert emitted == [(0, 0.8)]


def test_wasapi_reapply_all_channel_volumes_uses_effective_and_keeps_base(tmp_path) -> None:
    mgr = _wasapi_mgr(tmp_path)
    mgr._config.num_channels = 2
    mgr._config.set_app_names(0, ["Spotify"])
    mgr._config.set_cross_side(0, "a")
    mgr._config.set_app_names(1, ["Discord"])
    mgr._config.set_cross_side(1, "b")
    mgr._config.set_crossfader_enabled(True)
    mgr._config.set_crossfader_position(1.0)
    mgr._config.set_channel_volume(0, 0.8)
    mgr._config.set_channel_volume(1, 0.8)

    with patch.object(mgr, "_apply_volume_by_name") as apply_by_name:
        mgr.reapply_all_channel_volumes()

    calls = {c.args[0]: c.args[1] for c in apply_by_name.call_args_list}
    assert calls["Spotify"] == 0.0
    assert calls["Discord"] == 0.8
    assert mgr._config.get_channel_volume(0) == 0.8
    assert mgr._config.get_channel_volume(1) == 0.8


def test_wasapi_reapply_all_channel_volumes_skips_muted_channel(tmp_path) -> None:
    mgr = _wasapi_mgr(tmp_path)
    mgr._config.set_app_names(0, ["Spotify"])
    with mgr._state_lock:
        mgr._channel_muted[0] = True

    with patch.object(mgr, "_apply_volume_by_name") as apply_by_name:
        mgr.reapply_all_channel_volumes()

    apply_by_name.assert_not_called()


# ---------------------------------------------------------------------------
# PipeWireManager.apply_poti_volumes / apply_midi_volumes: skip the USB
# crossfader control channel entirely (its poti/CC value drives the bar
# position via main.py, not a mix volume -- nothing should reach
# hardware/apps/V-Sink for that index while it is the control channel).
# ---------------------------------------------------------------------------


def test_pipewire_apply_poti_volumes_skips_usb_control_channel(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.num_channels = 2
    mgr._config.set_crossfader_usb_channel_index(0)
    mgr._config.set_crossfader_enabled(True)
    # Simulate stale hardware assignment that predates the control assignment
    # (e.g. a profile saved by an older build) to prove the volume-apply path
    # itself skips the channel, independent of config-level clearing.
    ch = mgr._config._channel(0)
    ch["mode"] = "hardware"
    ch["hardware_id"] = "sink:stale_target"

    with (
        patch.object(mgr, "_get_vol_pulse", return_value=MagicMock()),
        patch.object(mgr, "_apply_hardware_volume") as apply_hw,
        patch.object(mgr, "_apply_volume_by_name") as apply_by_name,
    ):
        mgr.apply_poti_volumes([0.9, 0.5])

    apply_hw.assert_not_called()
    # Non-control channel (1) is unaffected by the skip.
    assert mgr._poti_volumes[0] == 0.9
    apply_by_name.assert_not_called()


def test_pipewire_apply_midi_volumes_skips_usb_control_channel(tmp_path) -> None:
    mgr = _pw_mgr(tmp_path)
    mgr._config.num_channels = 2
    mgr._config.set_crossfader_usb_channel_index(0)
    mgr._config.set_crossfader_enabled(True)
    ch = mgr._config._channel(0)
    ch["mode"] = "hardware"
    ch["hardware_id"] = "sink:stale_target"

    with (
        patch.object(mgr, "_get_vol_pulse", return_value=MagicMock()),
        patch.object(mgr, "_apply_hardware_volume") as apply_hw,
    ):
        mgr.apply_midi_volumes([(0, 0.9)])

    apply_hw.assert_not_called()
    # Base is still persisted (needed for restart-seeding), just not applied.
    assert mgr._config.get_channel_volume(0) == 0.9


def test_pipewire_apply_poti_volumes_still_applies_when_crossfader_disabled(tmp_path) -> None:
    """Skip only kicks in while the crossfader feature itself is enabled."""
    mgr = _pw_mgr(tmp_path)
    mgr._config.num_channels = 1
    mgr._config.set_crossfader_usb_channel_index(0)
    mgr._config.set_crossfader_enabled(False)
    ch = mgr._config._channel(0)
    ch["mode"] = "hardware"
    ch["hardware_id"] = "sink:target"

    with (
        patch.object(mgr, "_get_vol_pulse", return_value=MagicMock()),
        patch.object(mgr, "_apply_hardware_volume") as apply_hw,
    ):
        mgr.apply_poti_volumes([0.7])

    apply_hw.assert_called_once()
