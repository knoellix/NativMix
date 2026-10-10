"""Widget tests for the horizontal A/B crossfader bar.

Runs headless via the offscreen Qt platform set in conftest.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from nativmix.gui.crossfader_bar import CrossfaderBar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def test_slider_move_emits_position(qapp):
    bar = CrossfaderBar()
    seen: list[float] = []
    bar.position_changed.connect(seen.append)

    bar._slider.setValue(1000)
    assert len(seen) == 1
    assert abs(seen[-1] - 1.0) < 1e-6
    bar._slider.setValue(0)
    assert abs(seen[-1] - 0.0) < 1e-6
    bar._slider.setValue(500)
    assert abs(seen[-1] - 0.5) < 1e-6


def test_set_position_does_not_echo(qapp):
    bar = CrossfaderBar()
    seen: list[float] = []
    bar.position_changed.connect(seen.append)

    bar.set_position(0.25)
    assert seen == []  # programmatic sync must not re-emit
    # Clamped to [0, 1]
    bar.set_position(5.0)
    bar.set_position(-5.0)
    assert seen == []


def test_midi_label_tooltip(qapp):
    bar = CrossfaderBar()
    bar.set_midi_label(None)
    assert "right-click" in bar.toolTip().lower()
    bar.set_midi_label(42, 2)
    assert "CC42" in bar.toolTip()
    assert "M3" in bar.toolTip()  # midi channel is 0-based → displayed +1


def test_learning_hint(qapp):
    bar = CrossfaderBar()
    bar.set_learning(True)
    assert "move a midi control" in bar.toolTip().lower()
    bar.set_learning(False)
    assert "move a midi control" not in bar.toolTip().lower()
