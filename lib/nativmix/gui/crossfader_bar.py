"""Horizontal A/B crossfader row.

Shown below the channel scroll area and above the MIDI/size-grip bottom bar.
The bar only reports user intent via signals — all state (position, MIDI
binding, enabled flag, A/B assignment) lives in the active profile via
ConfigManager, so this widget stays free of business logic.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSlider, QVBoxLayout, QWidget

from nativmix.gui.crossfader_layout import channel_eligible_for_side
from nativmix.utils.qt_utils import _slot_guard

# Slider resolution: 0 = full A, _XF_RESOLUTION = full B. Mapped to 0.0–1.0.
_XF_RESOLUTION = 1000

# Shown above the slider when no USB/MIDI channel is assigned as the control.
_PLACEHOLDER_CONTROL_NAME = "—"


class CrossfaderBar(QWidget):
    """A horizontal crossfader with a control-name label, A/B menus and MIDI Learn.

    MIDI Learn/Clear is only offered via the main right-click menu while
    ``_midi_edit_mode`` is active (gated by MainWindow's "Edit MIDI Channel"
    toggle). Right-clicking the A/B labels always opens the assignment menu,
    independent of edit mode.
    """

    position_changed = pyqtSignal(float)  # 0.0 (full A) .. 1.0 (full B)
    midi_learn_requested = pyqtSignal()
    midi_clear_requested = pyqtSignal()
    side_assignment_toggled = pyqtSignal(str, int, bool)  # side "a"|"b", channel_index, checked

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._learning = False
        self._cc: int | None = None
        self._midi_channel = 0
        self._midi_edit_mode = False
        self._control_index: int | None = None
        # Rows for the A/B assignment menus: (channel_index, display_name, current_side).
        self._side_entries: list[tuple[int, str, str]] = []
        self._ctx_menu: QMenu | None = None

        outer = QVBoxLayout(self)
        # Tight to the V-Sink row above — only horizontal padding for A/B hit targets.
        outer.setContentsMargins(4, 0, 4, 0)
        outer.setSpacing(0)

        self._control_label = QLabel(_PLACEHOLDER_CONTROL_NAME)
        self._control_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small_font = self._control_label.font()
        small_font.setPointSize(max(7, small_font.pointSize() - 1))
        self._control_label.setFont(small_font)
        # Always visible (placeholder or name) so assigning a control channel
        # does not nudge the A/B track downward.
        outer.addWidget(self._control_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._label_a = QLabel("A")
        self._label_a.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label_a.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._label_a.customContextMenuRequested.connect(
            lambda pos: self._on_side_context_menu("a", self._label_a, pos)
        )

        self._label_b = QLabel("B")
        self._label_b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label_b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._label_b.customContextMenuRequested.connect(
            lambda pos: self._on_side_context_menu("b", self._label_b, pos)
        )

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, _XF_RESOLUTION)
        self._slider.setValue(_XF_RESOLUTION // 2)
        self._slider.valueChanged.connect(self._on_slider_changed)

        row.addWidget(self._label_a)
        row.addWidget(self._slider, stretch=1)
        row.addWidget(self._label_b)
        outer.addLayout(row)

        # Right-click on the slider / control-name label / empty area → Learn / Clear
        # MIDI, gated by `_midi_edit_mode`. Right-click on A/B is handled above and
        # never bubbles here because the labels have their own context menu policy.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._refresh_tooltip()

    # ------------------------------------------------------------------
    # Signals out
    # ------------------------------------------------------------------
    @_slot_guard
    def _on_slider_changed(self, value: int) -> None:
        self.position_changed.emit(value / float(_XF_RESOLUTION))

    def _build_main_menu(self) -> QMenu:
        """Build the Learn/Clear menu (slider / control-name / empty-area right-click)."""
        menu = QMenu(self)
        if self._midi_edit_mode:
            learn = menu.addAction("Cancel MIDI Learn" if self._learning else "Learn MIDI")
            learn.triggered.connect(lambda _checked=False: self.midi_learn_requested.emit())
            clear = menu.addAction("Clear MIDI")
            clear.triggered.connect(lambda _checked=False: self.midi_clear_requested.emit())
        else:
            hint = menu.addAction("Enable Edit MIDI Channel to Learn")
            hint.setEnabled(False)
        return menu

    @_slot_guard
    def _on_context_menu(self, pos) -> None:
        menu = self._build_main_menu()
        # Keep a reference so the non-blocking popup is not garbage-collected.
        self._ctx_menu = menu
        menu.popup(self.mapToGlobal(pos))

    def _build_side_menu(self, side: str) -> QMenu:
        """Build the checkable A/B assignment menu for *side* ("a" or "b")."""
        menu = QMenu(self)
        sides = {idx: s for idx, _name, s in self._side_entries}
        added_any = False
        for idx, name, cur_side in self._side_entries:
            if self._control_index is not None and idx == self._control_index:
                continue  # Control channel: not a valid A/B target, hidden entirely.
            eligible = channel_eligible_for_side(side, idx, control_index=self._control_index, sides=sides)
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(cur_side == side)
            act.setEnabled(eligible)
            if eligible:
                act.triggered.connect(lambda checked, i=idx, s=side: self.side_assignment_toggled.emit(s, i, checked))
            added_any = True
        if not added_any:
            empty = menu.addAction("No channels available")
            empty.setEnabled(False)
        return menu

    @_slot_guard
    def _on_side_context_menu(self, side: str, label: QLabel, pos) -> None:
        menu = self._build_side_menu(side)
        self._ctx_menu = menu
        menu.popup(label.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # State sync (called by MainWindow — never re-emits position_changed)
    # ------------------------------------------------------------------
    def set_position(self, position: float) -> None:
        """Move the slider to *position* (0.0–1.0) without emitting a change."""
        value = int(round(max(0.0, min(1.0, position)) * _XF_RESOLUTION))
        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(False)

    def set_learning(self, active: bool) -> None:
        """Toggle the visual 'waiting for MIDI' hint."""
        self._learning = bool(active)
        self._refresh_tooltip()

    def set_midi_label(self, cc: int | None, midi_channel: int = 0) -> None:
        """Remember the current MIDI binding for the tooltip."""
        self._cc = cc
        self._midi_channel = midi_channel
        self._refresh_tooltip()

    def set_control_name(self, name: str) -> None:
        """Update the label shown above the slider.

        Empty names keep the `—` placeholder so bar height stays stable when
        a control channel is assigned or cleared.
        """
        text = (name or "").strip()
        self._control_label.setText(text if text else _PLACEHOLDER_CONTROL_NAME)
        self._control_label.setVisible(True)

    def set_control_index(self, control_index: int | None) -> None:
        """Remember which channel index is the frozen crossfader control (if any).

        Used to hide that channel from the A/B assignment menus.
        """
        self._control_index = control_index

    def set_side_menu_model(self, entries: list[tuple[int, str, str]]) -> None:
        """Set the rows used to build the A/B assignment menus.

        Each entry is ``(channel_index, display_name, current_side)`` where
        ``current_side`` is ``"none"``, ``"a"`` or ``"b"``.
        """
        self._side_entries = list(entries)

    def set_midi_edit_mode(self, active: bool) -> None:
        """Gate Learn/Clear in the main context menu to MIDI-edit mode only.

        When edit mode turns off while a Learn is in progress, cancel it
        locally and re-emit `midi_learn_requested` (the same toggle signal the
        right-click "Cancel MIDI Learn" action uses) so MainWindow's
        `_crossfader_midi_learning` flag — which it owns — stays in sync.
        """
        active = bool(active)
        turning_off = self._midi_edit_mode and not active
        self._midi_edit_mode = active
        if turning_off and self._learning:
            self.set_learning(False)
            self.midi_learn_requested.emit()

    def _refresh_tooltip(self) -> None:
        if self._learning:
            text = "Move a MIDI control to bind the crossfader… (right-click to cancel)"
        elif self._cc is None:
            text = "A/B crossfader — right-click to Learn/Clear a MIDI CC."
        else:
            text = f"A/B crossfader — MIDI M{self._midi_channel + 1}/CC{self._cc} (right-click to change)."
        self.setToolTip(text)
        self._slider.setToolTip(text)
