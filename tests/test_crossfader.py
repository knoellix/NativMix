"""Unit tests for nativmix.audio.crossfader pure math helpers."""

from nativmix.audio.crossfader import (
    crossfader_gains,
    effective_volume,
    side_gain,
)


def test_gains_left_center_right() -> None:
    assert crossfader_gains(0.0) == (1.0, 0.0)
    ga, gb = crossfader_gains(0.5)
    assert ga == 1.0
    assert gb == 1.0
    assert crossfader_gains(1.0) == (0.0, 1.0)


def test_gains_clamps_position() -> None:
    assert crossfader_gains(-0.5) == (1.0, 0.0)
    assert crossfader_gains(1.5) == (0.0, 1.0)


def test_side_gain_disabled_or_none() -> None:
    assert side_gain("a", 1.0, enabled=False) == 1.0
    assert side_gain(None, 1.0, enabled=True) == 1.0
    assert side_gain("none", 1.0, enabled=True) == 1.0
    assert side_gain("", 1.0, enabled=True) == 1.0


def test_side_gain_a_and_b() -> None:
    assert side_gain("a", 0.0, enabled=True) == 1.0
    assert side_gain("b", 0.0, enabled=True) == 0.0
    assert side_gain("a", 1.0, enabled=True) == 0.0
    assert side_gain("b", 1.0, enabled=True) == 1.0


def test_effective_keeps_base_when_disabled() -> None:
    assert effective_volume(0.8, "a", 1.0, enabled=False) == 0.8


def test_effective_silences_a_when_full_b() -> None:
    assert effective_volume(0.8, "a", 1.0, enabled=True) == 0.0
    assert effective_volume(0.8, "b", 1.0, enabled=True) == 0.8


def test_effective_clamps_base() -> None:
    assert effective_volume(1.5, "b", 0.5, enabled=True) == 1.0
    assert effective_volume(-0.1, "a", 0.5, enabled=True) == 0.0
