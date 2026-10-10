import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.hardware.midi_throttle import CcVolumeThrottler


def test_throttler_emits_trailing_value():
    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((0, 7), 1, 0.5, 0.005) is None
    assert t.note((0, 7), 1, 0.9, 0.010) is None
    assert t.flush_due(0.025) == [(1, 0.9)]
    assert t.flush_due(0.030) == []


def test_throttler_emits_immediately_outside_window():
    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((0, 7), 1, 0.2, 0.025) == [(1, 0.2)]


def test_throttler_tracks_bindings_independently():
    t = CcVolumeThrottler(0.02)
    assert t.note((0, 7), 1, 0.1, 0.0) == [(1, 0.1)]
    assert t.note((1, 8), 2, 0.3, 0.0) == [(2, 0.3)]
    # Second binding's burst is still pending while the first is not due again.
    assert t.note((0, 7), 1, 0.4, 0.005) is None
    assert t.note((1, 8), 2, 0.6, 0.005) is None
    assert t.flush_due(0.02) == [(1, 0.4), (2, 0.6)]


def test_flush_due_without_pending_returns_empty():
    t = CcVolumeThrottler(0.02)
    assert t.flush_due(1.0) == []
