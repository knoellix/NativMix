"""Pure crossfader gain math (no GUI or backend dependencies)."""

from __future__ import annotations


def crossfader_gains(position: float) -> tuple[float, float]:
    """Return (gain_a, gain_b) for crossfader position in [0, 1] (clamped)."""
    p = max(0.0, min(1.0, float(position)))
    if p <= 0.5:
        return 1.0, 2.0 * p
    return 2.0 * (1.0 - p), 1.0


def side_gain(side: str | None, position: float, enabled: bool) -> float:
    """Per-channel crossfader gain; 1.0 when disabled or side is none/empty."""
    if not enabled:
        return 1.0
    key = (side or "none").lower()
    if key in ("", "none"):
        return 1.0
    ga, gb = crossfader_gains(position)
    if key == "a":
        return ga
    if key == "b":
        return gb
    return 1.0


def effective_volume(
    base: float,
    side: str | None,
    position: float,
    enabled: bool,
) -> float:
    """Apply crossfader to base volume; result clamped to [0, 1]."""
    return max(0.0, min(1.0, float(base) * side_gain(side, position, enabled)))
