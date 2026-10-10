"""Horizontal A/B crossfader row.

Shown below the channel scroll area and above the MIDI/size-grip bottom bar.
The bar only reports user intent via signals — all state (position, MIDI
binding, enabled flag) lives in the active profile via ConfigManager, so this
widget stays free of business logic.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSlider, QWidget

from nativmix.utils.qt_utils import _slot_guard

logger = logging.getLogger(__name__)

# Slider resolution: 0 = full A, _XF_RESOLUTION = full B. Mapped to 0.0–1.0.
_XF_RESOLUTION = 1000


class CrossfaderBar(QWidget):
    """A horizontal crossfader with 'A' / 'B' labels and right-click MIDI Learn."""

    position_changed = pyqtSignal(float)  # 0.0 (full A) .. 1.0 (full B)
    midi_learn_requested = pyqtSignal()
    midi_clear_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._learning = False
        self._cc: int | None = None
        self._midi_channel = 0
        self._ctx_menu: QMenu | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)

        self._label_a = QLabel("A")
        self._label_a.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label_b = QLabel("B")
        self._label_b.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, _XF_RESOLUTION)
        self._slider.setValue(_XF_RESOLUTION // 2)
        self._slider.valueChanged.connect(self._on_slider_changed)

        layout.addWidget(self._label_a)
        layout.addWidget(self._slider, stretch=1)
        layout.addWidget(self._label_b)

        # Right-click anywhere on the row → Learn / Clear MIDI.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._refresh_tooltip()

    # ------------------------------------------------------------------
    # Signals out
    # ------------------------------------------------------------------
    @_slot_guard
    def _on_slider_changed(self, value: int) -> None:
        self.position_changed.emit(value / float(_XF_RESOLUTION))

    @_slot_guard
    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        learn = menu.addAction("Cancel MIDI Learn" if self._learning else "Learn MIDI")
        learn.triggered.connect(lambda _checked=False: self.midi_learn_requested.emit())
        clear = menu.addAction("Clear MIDI")
        clear.triggered.connect(lambda _checked=False: self.midi_clear_requested.emit())
        # Keep a reference so the non-blocking popup is not garbage-collected.
        self._ctx_menu = menu
        menu.popup(self.mapToGlobal(pos))

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

    def _refresh_tooltip(self) -> None:
        if self._learning:
            text = "Move a MIDI control to bind the crossfader… (right-click to cancel)"
        elif self._cc is None:
            text = "A/B crossfader — right-click to Learn/Clear a MIDI CC."
        else:
            text = f"A/B crossfader — MIDI M{self._midi_channel + 1}/CC{self._cc} (right-click to change)."
        self.setToolTip(text)
        self._slider.setToolTip(text)
