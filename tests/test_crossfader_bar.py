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


def test_set_control_name_placeholder_and_text(qapp):
    bar = CrossfaderBar()
    assert bar._control_label.text() == "—"
    bar.set_control_name("USB Fader")
    assert bar._control_label.text() == "USB Fader"
    bar.set_control_name("")
    assert bar._control_label.text() == "—"
    bar.set_control_name("   ")
    assert bar._control_label.text() == "—"


def test_midi_edit_mode_gates_main_menu_actions(qapp):
    bar = CrossfaderBar()
    bar.set_midi_edit_mode(False)
    menu = bar._build_main_menu()
    texts = [a.text() for a in menu.actions()]
    assert not any("learn" in t.lower() and "enable edit" not in t.lower() for t in texts)
    assert any(not a.isEnabled() for a in menu.actions())

    bar.set_midi_edit_mode(True)
    menu = bar._build_main_menu()
    texts = [a.text() for a in menu.actions()]
    assert any("learn midi" in t.lower() for t in texts)
    assert any("clear midi" in t.lower() for t in texts)


def test_midi_edit_mode_off_cancels_in_progress_learn(qapp):
    bar = CrossfaderBar()
    bar.set_midi_edit_mode(True)
    bar.set_learning(True)

    seen: list[None] = []
    bar.midi_learn_requested.connect(lambda: seen.append(None))

    bar.set_midi_edit_mode(False)
    assert bar._learning is False
    assert len(seen) == 1  # notifies MainWindow via the existing toggle signal


def test_midi_edit_mode_off_without_learning_does_not_emit(qapp):
    bar = CrossfaderBar()
    bar.set_midi_edit_mode(True)
    seen: list[None] = []
    bar.midi_learn_requested.connect(lambda: seen.append(None))

    bar.set_midi_edit_mode(False)
    assert seen == []


def test_side_menu_excludes_control_and_disables_opposite_side(qapp):
    bar = CrossfaderBar()
    bar.set_control_index(3)
    bar.set_side_menu_model(
        [
            (0, "Firefox", "a"),
            (1, "Discord", "b"),
            (2, "Spotify", "none"),
            (3, "USB Fader", "none"),
        ]
    )

    menu_a = bar._build_side_menu("a")
    by_text = {a.text(): a for a in menu_a.actions()}
    assert "USB Fader" not in by_text  # control channel hidden
    assert by_text["Firefox"].isEnabled()
    assert by_text["Firefox"].isChecked()
    assert not by_text["Discord"].isEnabled()  # on the opposite side
    assert by_text["Spotify"].isEnabled()
    assert not by_text["Spotify"].isChecked()


def test_side_menu_toggle_emits_side_assignment_toggled(qapp):
    bar = CrossfaderBar()
    bar.set_control_index(None)
    bar.set_side_menu_model([(0, "Firefox", "none")])

    seen: list[tuple[str, int, bool]] = []
    bar.side_assignment_toggled.connect(lambda s, i, c: seen.append((s, i, c)))

    menu = bar._build_side_menu("a")
    action = menu.actions()[0]
    action.trigger()

    assert seen == [("a", 0, True)]


def test_side_menu_empty_model_shows_disabled_placeholder(qapp):
    bar = CrossfaderBar()
    bar.set_side_menu_model([])
    menu = bar._build_side_menu("a")
    actions = menu.actions()
    assert len(actions) == 1
    assert not actions[0].isEnabled()
