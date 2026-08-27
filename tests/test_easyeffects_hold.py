"""Unit tests for Easy Effects hold / routing destination helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.audio.easyeffects_hold import (  # noqa: E402
    is_easyeffects_sink,
    resolve_auto_route_target,
    volume_apply_mode,
)


def test_detects_classic_easyeffects_sink():
    assert is_easyeffects_sink("easyeffects_sink")
    assert is_easyeffects_sink("easyeffects_sink.2")
    assert is_easyeffects_sink("EasyEffects_Sink")


def test_rejects_normal_and_nativmix_sinks():
    assert not is_easyeffects_sink(None)
    assert not is_easyeffects_sink("")
    assert not is_easyeffects_sink("alsa_output.pci-0000_00_1f.3.analog-stereo")
    assert not is_easyeffects_sink("NativMix_CH_2")
    assert not is_easyeffects_sink("easyeffects_source")  # input side — not playback hold


def test_skip_route_when_manually_paused():
    assert (
        resolve_auto_route_target(
            current_sink="alsa_output.hw",
            vsink_enabled=True,
            vsink_name="NativMix_CH_0",
            default_sink="alsa_output.hw",
            routing_paused=True,
        )
        is None
    )


def test_route_to_vsink_or_default_when_not_held():
    assert (
        resolve_auto_route_target(
            current_sink="alsa_output.hw",
            vsink_enabled=True,
            vsink_name="NativMix_CH_1",
            default_sink="alsa_output.hw",
        )
        == "NativMix_CH_1"
    )
    assert (
        resolve_auto_route_target(
            current_sink="alsa_output.other",
            vsink_enabled=False,
            vsink_name="NativMix_CH_1",
            default_sink="alsa_output.hw",
        )
        == "alsa_output.hw"
    )


def test_no_move_when_already_on_target():
    assert (
        resolve_auto_route_target(
            current_sink="NativMix_CH_1",
            vsink_enabled=True,
            vsink_name="NativMix_CH_1",
            default_sink="alsa_output.hw",
        )
        is None
    )
    assert (
        resolve_auto_route_target(
            current_sink="alsa_output.hw",
            vsink_enabled=False,
            vsink_name="NativMix_CH_1",
            default_sink="alsa_output.hw",
        )
        is None
    )


def test_volume_mode_stream_on_ee_even_with_vsink():
    assert (
        volume_apply_mode(
            current_sink="easyeffects_sink",
            vsink_enabled=True,
            vsink_name="NativMix_CH_0",
        )
        == "stream"
    )


def test_volume_mode_vsink_when_on_channel_sink():
    assert (
        volume_apply_mode(
            current_sink="NativMix_CH_0",
            vsink_enabled=True,
            vsink_name="NativMix_CH_0",
        )
        == "vsink"
    )


def test_volume_mode_stream_without_vsink():
    assert (
        volume_apply_mode(
            current_sink="alsa_output.hw",
            vsink_enabled=False,
            vsink_name="NativMix_CH_0",
        )
        == "stream"
    )
