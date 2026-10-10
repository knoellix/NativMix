"""
CC volume throttling helper for the MIDI backend.

Coalesces fast bursts of Control Change messages per binding to at most
one emit every ``min_interval`` seconds, while guaranteeing the final
("trailing") value of a burst is always emitted via ``flush_due()`` even
if it arrives inside the throttle window.
"""

from __future__ import annotations


class CcVolumeThrottler:
    """Emit at most every min_interval; always flush the latest pending value."""

    def __init__(self, min_interval: float = 0.02) -> None:
        self.min_interval = min_interval
        self._last_emit: dict[tuple[int, int], float] = {}
        self._pending: dict[tuple[int, int], tuple[int, float]] = {}  # key -> (ch_idx, vol)

    def note(self, key: tuple[int, int], ch_idx: int, vol: float, now: float) -> list[tuple[int, float]] | None:
        """Record a new CC sample for *key*.

        Returns a one-element mapping list to emit immediately when outside
        the throttle window, or None if the sample is only queued as
        pending (to be delivered later by ``flush_due()``).
        """
        self._pending[key] = (ch_idx, vol)
        # No prior emit for this binding -> always pass the first sample
        # through immediately (there is nothing to throttle against yet).
        last = self._last_emit.get(key)
        if last is None or now - last >= self.min_interval:
            self._last_emit[key] = now
            self._pending.pop(key, None)
            return [(ch_idx, vol)]
        return None

    def flush_due(self, now: float) -> list[tuple[int, float]]:
        """Return mappings for all pending bindings whose throttle window has elapsed."""
        out: list[tuple[int, float]] = []
        for key, (ch_idx, vol) in list(self._pending.items()):
            last = self._last_emit.get(key)
            if last is None or now - last >= self.min_interval:
                self._last_emit[key] = now
                self._pending.pop(key, None)
                out.append((ch_idx, vol))
        return out
