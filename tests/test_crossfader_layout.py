"""Unit tests for crossfader bar width and A/B menu eligibility helpers."""

from nativmix.gui.crossfader_layout import (
    channel_eligible_for_side,
    crossfader_bar_width,
)


def test_bar_width_clamps_2_to_6() -> None:
    assert crossfader_bar_width(1, 70) == 140  # min 2
    assert crossfader_bar_width(3, 70) == 210
    assert crossfader_bar_width(6, 70) == 420
    assert crossfader_bar_width(10, 70) == 420  # max 6


def test_bar_width_empty_rebuild_uses_two_strips() -> None:
    assert crossfader_bar_width(0, 70) == 140


def test_eligibility_blocks_control_and_opposite() -> None:
    sides = {0: "a", 1: "b", 2: "none"}
    assert channel_eligible_for_side("a", 2, control_index=3, sides=sides) is True
    assert channel_eligible_for_side("a", 3, control_index=3, sides=sides) is False
    assert channel_eligible_for_side("a", 1, control_index=None, sides=sides) is False  # on B
    assert channel_eligible_for_side("b", 0, control_index=None, sides=sides) is False  # on A
    assert channel_eligible_for_side("a", 0, control_index=None, sides=sides) is True  # already A
