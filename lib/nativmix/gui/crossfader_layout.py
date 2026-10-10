"""Pure layout and A/B assignment eligibility helpers for the crossfader bar."""

from __future__ import annotations


def crossfader_bar_width(num_channels: int, strip_width: int) -> int:
    n = max(0, int(num_channels))
    w = max(1, int(strip_width))
    return max(2, min(6, n if n > 0 else 2)) * w


def channel_eligible_for_side(
    side: str,
    channel_index: int,
    *,
    control_index: int | None,
    sides: dict[int, str],
) -> bool:
    if control_index is not None and channel_index == control_index:
        return False
    other = "b" if side == "a" else "a"
    if (sides.get(channel_index, "none") or "none").lower() == other:
        return False
    return True
