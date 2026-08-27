"""
Main window for NativMix.

Design philosophy: ZERO manual colors, ZERO QSS.
100% native Qt style via QApplication.style() and QPalette.
Theme adapts automatically when KDE switches dark ↔ light
via QApplication.paletteChanged (emitted by Qt itself).

Layout:
    ┌────────────────────────────────────────────────────┐
    │  SettingsPanel (port combo, autostart toggle)      │
    ├──────┬──────┬──────┬──────┐                        │
    │ CH 1 │ CH 2 │ CH 3 │ CH 4 │  …  (QScrollArea)     │
    │slider│slider│slider│slider│                        │
    │  ↕   │  ↕   │  ↕   │  ↕   │                        │
    │[apps]│[apps]│[apps]│[apps]│                        │
    │[inv] │[inv] │[inv] │[inv] │                        │
    └──────┴──────┴──────┴──────┘                        │
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from nativmix.audio.easyeffects_hold import is_easyeffects_sink
from nativmix.gui.settings_panel import SettingsPanel
from nativmix.utils.paths import is_windows
from nativmix.utils.proc_resolver import GENERIC_PA_NAMES
from nativmix.utils.qt_utils import _slot_guard

if TYPE_CHECKING:
    from nativmix.audio.base import AudioBackendBase
    from nativmix.audio.manager import PipeWireManager
    from nativmix.hardware.arduino import ArduinoThread
    from nativmix.hardware.midi import MidiThread
    from nativmix.utils.config_manager import ConfigManager

logger = logging.getLogger(__name__)

_STRIP_DROP_ANIM_MS = 220
_STRIP_LIVE_ANIM_MS = 140


def _format_midi_binding_label(prefix: str, midi_ch: int, cc: int | None, empty: str) -> str:
    """Human-readable binding for narrow strips: MIDI ch as 1–16, short CC form."""
    if cc is None:
        return empty
    return f"{prefix}M{midi_ch + 1}/{cc}"


def _midi_edit_btn_text(label: str) -> str:
    """Pad label so icon and text are not glued together on narrow strips."""
    return f"\u2009{label}"  # thin space — icon slot already provides most gap


def _midi_edit_icon(theme_name: str, glyph: int = 12, slot: int = 16) -> QIcon:
    """
    Draw a theme icon into a fixed transparent slot so Learn/Mute/Delete
    icons share the same left edge (themes pack list-remove tighter than others).
    """
    src = QIcon.fromTheme(theme_name).pixmap(glyph, glyph)
    if src.isNull():
        return QIcon.fromTheme(theme_name)
    canvas = QPixmap(slot, slot)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    # Bias slightly toward the text so the glyph clears the left border.
    x = max(0, (slot - glyph) // 2 + 1)
    y = max(0, (slot - glyph) // 2)
    painter.drawPixmap(x, y, src)
    painter.end()
    return QIcon(canvas)


def _style_midi_edit_button(btn: QToolButton, *, with_menu: bool = True) -> None:
    """Smaller type + icon so Learn/Mute/Delete fit and match each other."""
    font = btn.font()
    if font.pointSize() > 0:
        font.setPointSize(max(8, font.pointSize() - 2))
    else:
        font.setPointSize(8)
    btn.setFont(font)
    btn.setIconSize(QSize(16, 16))
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    # Keep the previous control height — padding must not shrink the strip buttons.
    btn.setMinimumHeight(28)
    if with_menu:
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    else:
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
    # Horizontal padding only for icon clearance; vertical padding keeps height stable.
    btn.setStyleSheet("QToolButton { padding-left: 6px; padding-right: 2px; padding-top: 4px; padding-bottom: 4px; }")


def _is_gnome_x11() -> bool:
    """True if running on GNOME under X11 (xcb platform)."""
    if QGuiApplication.platformName() != "xcb":
        return False
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def _is_kde_x11() -> bool:
    """True if running on KDE Plasma under X11 (xcb platform)."""
    if QGuiApplication.platformName() != "xcb":
        return False
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


_CHANNEL_MIN_WIDTH = 60
_CHANNEL_MAX_WIDTH = 85


# ---------------------------------------------------------------------------
# Editable channel label (double-click to rename)
# ---------------------------------------------------------------------------


# Left/right only — ClosedHand often falls back to SizeAll (4-way) on Linux themes.
_GRIP_CURSOR = Qt.CursorShape.SizeHorCursor


def _push_grip_cursor() -> None:
    QApplication.setOverrideCursor(QCursor(_GRIP_CURSOR))


def _force_clear_cursor_overrides() -> None:
    """Ensure no stuck override (e.g. theme fallback) hides the grip hover cursor."""
    while QApplication.overrideCursor() is not None:
        QApplication.restoreOverrideCursor()


class _EditableChannelLabel(QLabel):
    """Channel name: double-click to rename (reorder uses the separator below)."""

    rename_requested = pyqtSignal(str)

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def mouseDoubleClickEvent(self, event) -> None:
        text, ok = QInputDialog.getText(self, "Rename Channel", "Name:", text=self.text())
        if ok and text.strip():
            self.rename_requested.emit(text.strip())
        super().mouseDoubleClickEvent(event)


class _StripDragSeparator(QFrame):
    """Horizontal rule that also acts as a strip reorder grip."""

    reorder_finished = pyqtSignal(object)  # global QPoint
    reorder_active_changed = pyqtSignal(bool)
    reorder_tracking = pyqtSignal(object)  # global QPoint while dragging

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._press_global: QPoint | None = None
        self._dragging = False
        self._reorder_enabled = False
        self._grab_cursor_pushed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # HLine alone is ~1px; give a usable hit target without changing look much.
        self.setMinimumHeight(8)

    def set_reorder_enabled(self, enabled: bool) -> None:
        self._reorder_enabled = enabled
        self.setProperty("nativmix_strip_drag", enabled)
        if not enabled:
            self._press_global = None
            self._dragging = False
            self._release_grab_cursor()
            self.reorder_active_changed.emit(False)
            self.unsetCursor()
        else:
            self.setCursor(QCursor(_GRIP_CURSOR))

    def _release_grab_cursor(self) -> None:
        if self._grab_cursor_pushed:
            self._grab_cursor_pushed = False
            _force_clear_cursor_overrides()

    def enterEvent(self, event) -> None:
        if self._reorder_enabled and not self._grab_cursor_pushed:
            self.setCursor(QCursor(_GRIP_CURSOR))
        super().enterEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._reorder_enabled:
            self._press_global = event.globalPosition().toPoint()
            self._dragging = False
            _push_grip_cursor()
            self._grab_cursor_pushed = True
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            pos = event.globalPosition().toPoint()
            if not self._dragging:
                delta = pos - self._press_global
                if delta.manhattanLength() >= QApplication.startDragDistance():
                    self._dragging = True
                    self.reorder_active_changed.emit(True)
            if self._dragging:
                self.reorder_tracking.emit(pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        was_dragging = self._dragging
        if event.button() == Qt.MouseButton.LeftButton and was_dragging:
            self.reorder_finished.emit(event.globalPosition().toPoint())
        self._press_global = None
        self._dragging = False
        self._release_grab_cursor()
        if was_dragging:
            self.reorder_active_changed.emit(False)
        if self._reorder_enabled:
            self.setCursor(QCursor(_GRIP_CURSOR))
        else:
            self.unsetCursor()
        if event.button() == Qt.MouseButton.LeftButton and was_dragging:
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Single mapped-app row (remove button + name)
# ---------------------------------------------------------------------------


class _AppRow(QWidget):
    """[×] [name]  – one per assigned app inside a channel."""

    routing_pause_toggled = pyqtSignal(str, bool)  # app_name, paused

    def __init__(self, app_name: str, on_remove, parent=None) -> None:
        super().__init__(parent)
        self.app_name = app_name
        self._routing_paused = False
        self._nm_routed = True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._remove_btn = QToolButton()
        self._remove_btn.setIcon(QIcon.fromTheme("list-remove"))
        self._remove_btn.setFixedSize(QSize(18, 18))
        self._remove_btn.setAutoRaise(True)
        self._remove_btn.setToolTip("Remove app.")
        self._remove_btn.clicked.connect(on_remove)

        self._name_label = QLabel()
        self._name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name_label.setToolTip(f"App: {app_name}")
        if app_name in ("System Master", "Other Apps"):
            font = self._name_label.font()
            font.setBold(True)
            self._name_label.setFont(font)

        elided = self._name_label.fontMetrics().elidedText(app_name, Qt.TextElideMode.ElideRight, 60)
        self._name_label.setText(elided)

        layout.addWidget(self._remove_btn)
        layout.addWidget(self._name_label)

        if app_name.lower() != "system master":
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_context_menu)

        self.update_dynamic_styles()

    def set_name_tooltip(self, text: str) -> None:
        """Set the tooltip on the app name label."""
        self._name_label.setToolTip(text)

    def set_routing_state(self, *, paused: bool, nm_routed: bool) -> None:
        """Update pause flag and whether NativMix currently owns the destination."""
        self._routing_paused = paused
        self._nm_routed = nm_routed
        tip = f"App: {self.app_name}"
        if paused:
            tip += "\nNativMix routing paused (volume/mute still apply)."
        elif not nm_routed:
            tip += "\nRouted outside NativMix (e.g. Easy Effects)."
        self._name_label.setToolTip(tip)
        self.update_dynamic_styles()

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        if self._routing_paused:
            action = QAction("Resume NativMix routing", self)
            action.triggered.connect(lambda _=False: self.routing_pause_toggled.emit(self.app_name, False))
        else:
            action = QAction("Pause NativMix routing", self)
            action.setToolTip(
                "Do not move this app to a V-Sink or the default sink. "
                "Use for Easy Effects or other external routing. Volume and mute still work."
            )
            action.triggered.connect(lambda _=False: self.routing_pause_toggled.emit(self.app_name, True))
        menu.addAction(action)
        menu.exec(self.mapToGlobal(pos))

    def update_dynamic_styles(self) -> None:
        """Tint the X button to match the system Highlight color and apply custom hover state."""
        palette = QApplication.palette()
        accent_color = palette.color(QPalette.ColorRole.Highlight)
        accent_hex = accent_color.name()
        muted_color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)

        base_icon = QIcon.fromTheme("list-remove").pixmap(18, 18)

        if not base_icon.isNull():
            tinted = QPixmap(base_icon.size())
            tinted.fill(Qt.GlobalColor.transparent)
            painter = QPainter(tinted)
            painter.drawPixmap(0, 0, base_icon)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(tinted.rect(), accent_color)
            painter.end()
            self._remove_btn.setIcon(QIcon(tinted))

        btn_style = f"""
        QToolButton:hover {{
            background-color: {accent_hex};
            border-radius: 4px;
        }}
        """
        self._remove_btn.setStyleSheet(btn_style)

        # Accent when NativMix routes this app; disabled/muted text when not.
        text_color = accent_color if self._nm_routed and not self._routing_paused else muted_color
        pal = self._name_label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, text_color)
        self._name_label.setPalette(pal)


# ---------------------------------------------------------------------------
# Per-channel column
# ---------------------------------------------------------------------------


class ChannelWidget(QFrame):
    """
    One vertical mixer channel column.

    Contains (top → bottom):
      level label → slider → CH number → separator →
      mode switch → app list (with × buttons)/hw display →
      + App / + Gerät button → Toggles (Invert/VSink)
    """

    strip_drop = pyqtSignal(int, object)  # source_id, global QPoint
    reorder_tracking = pyqtSignal(object)  # global QPoint while dragging
    reorder_active_changed = pyqtSignal(bool)

    def __init__(
        self,
        channel_index: int,
        config: ConfigManager,
        backend: PipeWireManager,
        is_midi: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ch = channel_index
        self._config = config
        self._backend = backend
        self.is_midi_channel = is_midi
        self._compact = False
        self._drag_blocked = False
        logger.debug("Creating ChannelWidget: index=%d, is_midi=%s", channel_index, is_midi)

        self.setFrameShape(QFrame.Shape.NoFrame)
        # Avoid opaque Fusion fills so window transparency shows around faders.
        self.setAutoFillBackground(False)
        self.setMinimumWidth(_CHANNEL_MIN_WIDTH)
        # Prevent the whole column from stretching infinitely if long text is loaded
        self.setMaximumWidth(_CHANNEL_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # ── Mute Button ────────────────────────────────────────────────
        self._mute_btn = QToolButton()
        self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-high"))
        self._mute_btn.setToolTip("Toggle mute.")
        self._mute_btn.clicked.connect(lambda checked=False: self._backend.toggle_mute(self._ch))

        # ── Level label ────────────────────────────────────────────────
        self._level_label = QLabel("—")
        self._level_label.setObjectName("pct_label")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        small = self._level_label.font()
        small.setPointSize(9)
        self._level_label.setFont(small)

        # Reduced opacity applied later during update_accent_colors

        # ── Slider ─────────────────────────────────────────────────────
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)

        # Initial volume sync from config
        init_vol = self._config.get_channel_volume(self._ch)
        self._slider.setFixedHeight(180)
        self._slider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._slider.valueChanged.connect(self._on_slider_changed)

        # Explicitly set initial volume to update label AND slider
        self.set_volume(init_vol)

        default_label = f"MIDI {channel_index + 1}" if self.is_midi_channel else f"CH {channel_index + 1}"
        label_text = self._config.get_channel_label(channel_index) or default_label
        self._ch_label = _EditableChannelLabel(label_text)
        self._ch_label.setObjectName("ch_label")
        self._ch_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._ch_label.setToolTip("Double-click to rename")
        self._ch_label.rename_requested.connect(self._on_rename)
        tiny = self._ch_label.font()
        tiny.setPointSize(8)
        self._ch_label.setFont(tiny)

        # Accent palette applied later during update_accent_colors

        self._sep = _StripDragSeparator()
        self._sep.setFrameShape(QFrame.Shape.HLine)
        self._sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._sep.setToolTip("Drag to reorder channel strips")
        self._sep.reorder_finished.connect(self._on_reorder_gesture_finished)
        self._sep.reorder_active_changed.connect(self._on_reorder_active_changed)
        self._sep.reorder_tracking.connect(self.reorder_tracking.emit)
        self._sep.reorder_active_changed.connect(self.reorder_active_changed.emit)
        self._update_drag_handle_cursor()

        # ── Mode Switch ────────────────────────────────────────────────
        self._mode_cb = QCheckBox("Device")
        self._mode_cb.setToolTip("Toggle between App Mode and Hardware Mode.")
        self._mode_cb.clicked.connect(self._on_mode_toggled)

        # ── App list / HW Selection display ────────────────────────────
        self._app_list_widget = QWidget()
        self._app_list_widget.setObjectName("app_list_widget")
        self._app_list_layout = QVBoxLayout(self._app_list_widget)
        self._app_list_layout.setContentsMargins(0, 0, 0, 0)
        self._app_list_layout.setSpacing(2)

        self._app_list_scroll = QScrollArea()
        self._app_list_scroll.setWidgetResizable(True)
        self._app_list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._app_list_scroll.viewport().setAutoFillBackground(False)
        self._app_list_scroll.setStyleSheet("QScrollArea, #app_list_widget { background: transparent; }")
        self._app_list_scroll.setFixedHeight(90)
        self._app_list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._app_list_scroll.setWidget(self._app_list_widget)

        # ── Add-stream / Add-HW button ─────────────────────────────────
        self._add_btn = QPushButton()
        self._add_btn.clicked.connect(self._open_picker)

        # ── Toggle Controls ────────────────────────────────────────────
        self._toggles_layout = QVBoxLayout()
        self._toggles_layout.setContentsMargins(0, 4, 0, 0)
        self._toggles_layout.setSpacing(4)

        # Invert checkbox
        self._invert_cb = QCheckBox("Inv")
        self._invert_cb.setToolTip("Invert slider direction.")
        self._invert_cb.setChecked(self._config.get_effective_inversion(channel_index))
        sp_inv = self._invert_cb.sizePolicy()
        sp_inv.setRetainSizeWhenHidden(True)
        self._invert_cb.setSizePolicy(sp_inv)
        self._invert_cb.toggled.connect(self._on_invert_toggled)
        self._invert_cb.setVisible(self._config.show_invert_option)

        # V-Sink checkbox
        self._vsink_cb = QCheckBox("V-Sink")
        self._vsink_cb.setToolTip("Route audio through a virtual sink.")
        self._vsink_cb.setChecked(self._config.is_v_sink_enabled(channel_index))
        sp_vsink = self._vsink_cb.sizePolicy()
        sp_vsink.setRetainSizeWhenHidden(True)
        self._vsink_cb.setSizePolicy(sp_vsink)
        self._vsink_cb.toggled.connect(self._on_vsink_toggled)

        self._toggles_layout.addWidget(self._mode_cb)
        self._toggles_layout.addWidget(self._vsink_cb)
        self._toggles_layout.addWidget(self._invert_cb)

        # Initialize Mode UI State
        is_hw = self._config.get_channel_mode(self._ch) == "hardware"
        self._mode_cb.setChecked(is_hw)
        self._apply_mode_ui(is_hw)

        # ── Setup size policies for consistency ───────────────────────
        # We always want the app list and toggles to exist so columns align.
        # Use setRetainSizeWhenHidden(True) if they ever get hidden.
        sp_scroll = self._app_list_scroll.sizePolicy()
        sp_scroll.setRetainSizeWhenHidden(True)
        self._app_list_scroll.setSizePolicy(sp_scroll)

        sp_add = self._add_btn.sizePolicy()
        sp_add.setRetainSizeWhenHidden(True)
        self._add_btn.setSizePolicy(sp_add)

        # ── Root layout ────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)

        layout.addWidget(self._mute_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._level_label)
        layout.addWidget(self._slider, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._ch_label)
        layout.addWidget(self._sep)

        layout.addWidget(self._app_list_scroll)
        layout.addWidget(self._add_btn)
        layout.addLayout(self._toggles_layout)

        # ── MIDI UI Elements (Bottom) ──────────────────────────────────
        # Channel strips are only ~60–85 px wide: full-width buttons, MIDI
        # channel via toolbutton ▼ menu (no side-by-side spinbox).
        if self.is_midi_channel:
            self._learn_btn = QToolButton()
            self._learn_btn.setIcon(_midi_edit_icon("media-record"))
            _style_midi_edit_button(self._learn_btn)
            self._learn_btn.setCheckable(True)
            self._learn_btn.setToolTip(
                "Learn volume CC (captures MIDI channel + CC).\nUse the ▼ menu to change MIDI channel (1–16) manually."
            )
            self._learn_btn.clicked.connect(self._on_learn_clicked)
            self._vol_midi_menu = QMenu(self._learn_btn)
            self._vol_midi_menu.aboutToShow.connect(self._rebuild_vol_midi_menu)
            self._learn_btn.setMenu(self._vol_midi_menu)
            self._refresh_vol_learn_label()

            self._mute_learn_btn = QToolButton()
            self._mute_learn_btn.setIcon(_midi_edit_icon("audio-volume-muted"))
            _style_midi_edit_button(self._mute_learn_btn)
            self._mute_learn_btn.setCheckable(True)
            self._mute_learn_btn.setToolTip(
                "Learn mute CC (captures MIDI channel + CC).\nUse the ▼ menu to change MIDI channel (1–16) manually."
            )
            self._mute_learn_btn.clicked.connect(self._on_mute_learn_clicked)
            self._mute_midi_menu = QMenu(self._mute_learn_btn)
            self._mute_midi_menu.aboutToShow.connect(self._rebuild_mute_midi_menu)
            self._mute_learn_btn.setMenu(self._mute_midi_menu)
            self._refresh_mute_learn_label()

            self._remove_midi_btn = QToolButton()
            self._remove_midi_btn.setIcon(_midi_edit_icon("list-remove"))
            _style_midi_edit_button(self._remove_midi_btn, with_menu=False)
            self._remove_midi_btn.setText(_midi_edit_btn_text("Delete"))
            self._remove_midi_btn.setToolTip("Remove this MIDI channel.")
            self._remove_midi_btn.clicked.connect(self._on_remove_midi_clicked)

            midi_controls_layout = QVBoxLayout()
            midi_controls_layout.setContentsMargins(0, 4, 0, 0)
            midi_controls_layout.setSpacing(4)
            midi_controls_layout.addWidget(self._learn_btn)
            midi_controls_layout.addWidget(self._mute_learn_btn)
            midi_controls_layout.addWidget(self._remove_midi_btn)
            layout.addLayout(midi_controls_layout)

            # Hidden by default — shown when "Edit MIDI Channel" is active
            self._learn_btn.setVisible(False)
            self._mute_learn_btn.setVisible(False)
            self._remove_midi_btn.setVisible(False)

        layout.addStretch()

        self.refresh_theme()
        self._refresh_app_list()

    @_slot_guard
    def _rebuild_vol_midi_menu(self) -> None:
        self._vol_midi_menu.clear()
        current = self._config.get_midi_channel(self._ch)
        for display in range(1, 17):
            act = self._vol_midi_menu.addAction(f"MIDI channel {display}")
            act.setCheckable(True)
            act.setChecked((display - 1) == current)
            act.triggered.connect(lambda _checked=False, d=display: self._set_vol_midi_channel(d - 1))

    @_slot_guard
    def _rebuild_mute_midi_menu(self) -> None:
        self._mute_midi_menu.clear()
        current = self._config.get_midi_mute_channel(self._ch)
        for display in range(1, 17):
            act = self._mute_midi_menu.addAction(f"MIDI channel {display}")
            act.setCheckable(True)
            act.setChecked((display - 1) == current)
            act.triggered.connect(lambda _checked=False, d=display: self._set_mute_midi_channel(d - 1))

    @_slot_guard
    def _set_vol_midi_channel(self, midi_ch: int) -> None:
        self._config.set_midi_channel(self._ch, midi_ch)
        self._refresh_vol_learn_label()

    @_slot_guard
    def _set_mute_midi_channel(self, midi_ch: int) -> None:
        self._config.set_midi_mute_channel(self._ch, midi_ch)
        self._refresh_mute_learn_label()

    @_slot_guard
    def _on_learn_clicked(self, checked: bool) -> None:
        if checked:
            if self._mute_learn_btn.isChecked():
                self._mute_learn_btn.setChecked(False)
                self._on_mute_learn_clicked(False)
            self._learn_btn.setText(_midi_edit_btn_text("Cancel"))
            pal = self._learn_btn.palette()
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("red"))
            self._learn_btn.setPalette(pal)
            logger.debug("Channel %d entering MIDI Learn mode", self._ch)
        else:
            self._refresh_vol_learn_label()
            self._learn_btn.setPalette(QApplication.palette())

    def _refresh_vol_learn_label(self) -> None:
        cc = self._config.get_midi_cc(self._ch)
        midi_ch = self._config.get_midi_channel(self._ch)
        self._learn_btn.setText(_midi_edit_btn_text(_format_midi_binding_label("", midi_ch, cc, "Learn")))

    def _refresh_mute_learn_label(self) -> None:
        cc = self._config.get_midi_mute_cc(self._ch)
        midi_ch = self._config.get_midi_mute_channel(self._ch)
        self._mute_learn_btn.setText(_midi_edit_btn_text(_format_midi_binding_label("", midi_ch, cc, "Mute")))

    def update_midi_cc(self, cc_number: int, midi_channel: int = 0, slot: int = 0) -> None:
        """Update the Learn button after a successful volume learn."""
        del slot  # single Learn slot; kept for call-site compatibility
        self._learn_btn.setChecked(False)
        self._learn_btn.setText(_midi_edit_btn_text(_format_midi_binding_label("", midi_channel, cc_number, "Learn")))
        self._learn_btn.setPalette(QApplication.palette())
        logger.debug(
            "Channel %d MIDI volume binding updated to M%d/CC%d",
            self._ch,
            midi_channel + 1,
            cc_number,
        )

    @_slot_guard
    def _on_mute_learn_clicked(self, checked: bool) -> None:
        if checked:
            if self._learn_btn.isChecked():
                self._learn_btn.setChecked(False)
                self._on_learn_clicked(False)
            self._mute_learn_btn.setText(_midi_edit_btn_text("Cancel"))
            pal = self._mute_learn_btn.palette()
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("red"))
            self._mute_learn_btn.setPalette(pal)
            logger.debug("Channel %d entering Mute CC Learn mode", self._ch)
        else:
            self._refresh_mute_learn_label()
            self._mute_learn_btn.setPalette(QApplication.palette())

    def update_midi_mute_cc(self, cc_number: int, midi_channel: int = 0) -> None:
        """Update the mute-CC button text after a successful learn."""
        self._mute_learn_btn.setChecked(False)
        self._mute_learn_btn.setText(
            _midi_edit_btn_text(_format_midi_binding_label("", midi_channel, cc_number, "Mute"))
        )
        self._mute_learn_btn.setPalette(QApplication.palette())
        logger.debug(
            "Channel %d MIDI mute binding updated to M%d/CC%d",
            self._ch,
            midi_channel + 1,
            cc_number,
        )

    def set_edit_mode(self, visible: bool) -> None:
        """Show or hide the Learn, Mute-CC, and Delete buttons."""
        if not self.is_midi_channel:
            return
        self._learn_btn.setVisible(visible)
        self._mute_learn_btn.setVisible(visible)
        self._remove_midi_btn.setVisible(visible)

    def set_compact_mode(self, compact: bool) -> None:
        """Hide app list and controls below the separator; separator stays visible."""
        self._compact = compact
        self._update_drag_handle_cursor()
        # Freeze width so fader spacing doesn't change when app list is hidden
        if compact:
            self.setFixedWidth(self.width())
        else:
            self.setMinimumWidth(_CHANNEL_MIN_WIDTH)
            self.setMaximumWidth(_CHANNEL_MAX_WIDTH)

        # Tighten bottom margin in compact mode to remove empty space below separator
        self.layout().setContentsMargins(2, 4, 2, 1 if compact else 4)

        # Toggle RetainSizeWhenHidden so hidden widgets release their space
        for widget in (self._app_list_scroll, self._add_btn):
            sp = widget.sizePolicy()
            sp.setRetainSizeWhenHidden(not compact)
            widget.setSizePolicy(sp)

        # _invert_cb has RetainSizeWhenHidden=True by default; toggle it so
        # compact mode can actually shrink the layout.
        sp_inv = self._invert_cb.sizePolicy()
        sp_inv.setRetainSizeWhenHidden(not compact)
        self._invert_cb.setSizePolicy(sp_inv)

        self._sep.setVisible(True)
        self._app_list_scroll.setVisible(not compact)
        self._add_btn.setVisible(not compact)
        if compact:
            for i in range(self._toggles_layout.count()):
                item = self._toggles_layout.itemAt(i)
                if item and item.widget():
                    item.widget().setVisible(False)
        else:
            # Restore proper visibility — invert respects its setting
            self._mode_cb.setVisible(True)
            self._vsink_cb.setVisible(True)
            self._invert_cb.setVisible(self._config.show_invert_option)
        if self.is_midi_channel and compact:
            self._learn_btn.setVisible(False)
            self._mute_learn_btn.setVisible(False)
            self._remove_midi_btn.setVisible(False)

    def set_drag_blocked(self, blocked: bool) -> None:
        """Temporarily disable strip reorder grips (e.g. during drop animation)."""
        self._drag_blocked = blocked
        self._update_drag_handle_cursor()

    def _update_drag_handle_cursor(self) -> None:
        enabled = not self._compact and not self._drag_blocked
        self._sep.set_reorder_enabled(enabled)

    def _on_reorder_active_changed(self, active: bool) -> None:
        """Subtle dim while dragging so the moving strip is easier to spot."""
        if active:
            fx = QGraphicsOpacityEffect(self)
            fx.setOpacity(0.78)
            self.setGraphicsEffect(fx)
        else:
            self.setGraphicsEffect(None)

    def _on_reorder_gesture_finished(self, global_pos: QPoint) -> None:
        """Finish strip reorder; MainWindow resolves the insert gap from the pointer."""
        if self._compact:
            return
        self.strip_drop.emit(self._ch, global_pos)

    @property
    def channel_index(self) -> int:
        """Zero-based index of this channel."""
        return self._ch

    def volume_learn_slot(self) -> int | None:
        """Return 0 if volume Learn is armed, else None."""
        if self.is_midi_channel and self._learn_btn.isChecked():
            return 0
        return None

    def is_waiting_for_volume_learn(self) -> bool:
        """Return True if the volume Learn button is active."""
        return self.volume_learn_slot() is not None

    def is_waiting_for_mute_learn(self) -> bool:
        """Return True if the Mute-CC Learn button is active."""
        return self.is_midi_channel and self._mute_learn_btn.isChecked()

    def is_waiting_for_midi(self) -> bool:
        """Return True if any Learn button is active (used for connection-reset)."""
        return self.is_waiting_for_volume_learn() or self.is_waiting_for_mute_learn()

    def cancel_learn(self) -> None:
        """Cancel any active MIDI learn without assigning a CC."""
        if not self.is_midi_channel:
            return
        if self._learn_btn.isChecked():
            self._learn_btn.setChecked(False)
            self._on_learn_clicked(False)
        if self._mute_learn_btn.isChecked():
            self._mute_learn_btn.setChecked(False)
            self._on_mute_learn_clicked(False)

    @_slot_guard
    def _on_remove_midi_clicked(self, checked: bool = False) -> None:
        reply = QMessageBox.question(
            self,
            "Remove MIDI Channel",
            f"Are you sure you want to remove {self._ch_label.text()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._config.remove_midi_channel(self._ch)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_volume(self, volume: float) -> None:
        pct = int(volume * 100)
        self._slider.blockSignals(True)
        self._slider.setValue(pct)
        self._level_label.setText(f"{pct} %")
        self._slider.blockSignals(False)

    @pyqtSlot(int, int, int)
    @_slot_guard
    def handle_midi_input(self, midi_channel: int, cc: int, value: int) -> None:
        """Real-time slider sync from MidiThread.midi_cc_received.
        Learn logic lives in MainWindow.on_midi_cc_received so there is one
        central break-on-first-match gate for both volume and mute-CC learn.
        """
        if self.is_waiting_for_midi():
            return
        mapped_cc = self._config.get_midi_cc(self._ch)
        mapped_midi_ch = self._config.get_midi_channel(self._ch)
        if mapped_cc is not None and cc == mapped_cc and midi_channel == mapped_midi_ch:
            vol = value / 127.0
            self.set_volume(vol)
            self._config.set_channel_volume(self._ch, vol)

    @pyqtSlot(int)
    @_slot_guard
    def _on_slider_changed(self, value: int) -> None:
        """Called when the user drags the GUI slider."""
        vol_float = value / 100.0
        self._level_label.setText(f"{value} %")
        self._backend.set_channel_volume(self._ch, vol_float)

    def set_mute_state(self, is_muted: bool) -> None:
        if is_muted:
            self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-muted"))
            self._slider.setEnabled(False)
        else:
            self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-high"))
            self._slider.setEnabled(True)

    def refresh(self) -> None:
        self._refresh_app_list()

    def update_settings(self) -> None:
        self._invert_cb.setVisible(self._config.show_invert_option)

    def refresh_theme(self) -> None:
        """Tell the channel to redraw components for the new theme."""
        self.update_dynamic_styles()
        # _app_list_layout contains _AppRow widgets
        for i in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), _AppRow):
                item.widget().update_dynamic_styles()

    def update_dynamic_styles(self) -> None:
        """Apply dynamic stylesheets directly to the components to override stubborn Qt defaults."""
        palette = QApplication.palette()

        # Prevent KDE from fading the accent color when the window loses focus
        for role in (
            QPalette.ColorRole.Highlight,
            QPalette.ColorRole.HighlightedText,
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Button,
        ):
            palette.setColor(QPalette.ColorGroup.Inactive, role, palette.color(QPalette.ColorGroup.Active, role))

        self._slider.setPalette(palette)

        accent_hex = palette.color(QPalette.ColorRole.Highlight).name()
        # Use Button instead of Dark because Dark is not parsed by our KDE theme parser,
        # causing it to stay stuck on the previous theme's color!
        bg_hex = palette.color(QPalette.ColorRole.Button).name()
        text_color = palette.color(QPalette.ColorRole.WindowText)
        border_hex = f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 50)"

        # 1. Sliders (Dynamic Theme Variables)
        # Use theme-compliant colors to prevent reverting to default blue when inactive.
        # Make the border slightly darker than the main accent color for better contrast
        slider_border_hex = palette.color(QPalette.ColorRole.Highlight).darker(150).name()

        slider_qss = f"""
        QSlider::groove:vertical {{
            background: {bg_hex};
            border: 1px solid {border_hex};
            width: 6px;
            border-radius: 3px;
        }}
        QSlider::add-page:vertical {{
            background: {accent_hex};
            border: 1px solid {border_hex};
            border-radius: 3px;
        }}
        QSlider::sub-page:vertical {{
            background: transparent;
        }}
        QSlider::handle:vertical {{
            background: {bg_hex};
            border: 1px solid {slider_border_hex};
            height: 12px;
            margin: 0 -4px;
            border-radius: 7px;
        }}
        """
        self._slider.setStyleSheet(slider_qss)

        # Color the labels using QPalette instead of stylesheets to avoid breaking Wayland native tooltips
        pal_ch = self._ch_label.palette()
        pal_ch.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.Highlight))
        self._ch_label.setPalette(pal_ch)

        pal_lvl = self._level_label.palette()
        pal_lvl.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.Highlight))
        self._level_label.setPalette(pal_lvl)

        # 3. ToolButtons (Mute, Add) Inherit Global Hover
        # We only set specific properties here if needed.
        btn_qss = "QToolButton, QPushButton { border: none; border-radius: 4px; }"
        self._mute_btn.setStyleSheet(btn_qss)
        self._add_btn.setStyleSheet(btn_qss)

    # ------------------------------------------------------------------
    # App list
    # ------------------------------------------------------------------

    def _refresh_app_list(self) -> None:
        while self._app_list_layout.count():
            item = self._app_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._config.get_channel_mode(self._ch) == "hardware":
            hw_id = self._config.get_hardware_id(self._ch)
            if hw_id:
                parts = hw_id.split(":", 1)
                display_name = parts[1] if len(parts) == 2 else hw_id
                self._app_list_layout.addWidget(_AppRow(display_name, on_remove=self._remove_hw))
        else:
            sink_by_app = {}
            if hasattr(self._backend, "get_app_sink_names"):
                try:
                    sink_by_app = self._backend.get_app_sink_names()
                except Exception as exc:
                    logger.debug("get_app_sink_names failed: %s", exc)
            vsink_name = f"NativMix_CH_{self._ch}"
            vsink_on = self._config.is_v_sink_enabled(self._ch)
            for name in self._config.get_app_names(self._ch):
                row = _AppRow(name, on_remove=lambda _=False, n=name: self._remove_app(n))
                row.routing_pause_toggled.connect(self._on_app_routing_pause_toggled)
                paused = self._config.is_app_routing_paused(self._ch, name)
                sink = sink_by_app.get(name.lower())
                if paused:
                    nm_routed = False
                elif sink is None:
                    # No live stream — treat as OK (not externally held).
                    nm_routed = True
                elif vsink_on:
                    nm_routed = sink == vsink_name
                else:
                    nm_routed = not is_easyeffects_sink(sink) and not sink.startswith("NativMix_")
                row.set_routing_state(paused=paused, nm_routed=nm_routed)
                self._app_list_layout.addWidget(row)

        # Hide V-Sink for special pseudo-apps (System Master / Other Apps),
        # hardware mode, or when running on Windows (no PipeWire null-sinks).
        _SPECIAL = ("system master", "other apps")
        app_names_lower = [n.lower() for n in self._config.get_app_names(self._ch)]
        has_special = any(n in _SPECIAL for n in app_names_lower)
        is_hw = self._config.get_channel_mode(self._ch) == "hardware"
        self._vsink_cb.setVisible(not has_special and not is_hw and not is_windows())

    @pyqtSlot(str, bool)
    @_slot_guard
    def _on_app_routing_pause_toggled(self, app_name: str, paused: bool) -> None:
        self._config.set_app_routing_paused(self._ch, app_name, paused)
        self._config.save()
        self._refresh_app_list()

    def refresh_app_routing_styles(self) -> None:
        """Update pause/NM-routed colors without rebuilding the whole list."""
        if self._config.get_channel_mode(self._ch) == "hardware":
            return
        sink_by_app: dict[str, str] = {}
        if hasattr(self._backend, "get_app_sink_names"):
            try:
                sink_by_app = self._backend.get_app_sink_names()
            except Exception as exc:
                logger.debug("get_app_sink_names failed: %s", exc)
        vsink_name = f"NativMix_CH_{self._ch}"
        vsink_on = self._config.is_v_sink_enabled(self._ch)
        for i in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(i)
            row = item.widget() if item else None
            if not isinstance(row, _AppRow):
                continue
            paused = self._config.is_app_routing_paused(self._ch, row.app_name)
            sink = sink_by_app.get(row.app_name.lower())
            if paused:
                nm_routed = False
            elif sink is None:
                nm_routed = True
            elif vsink_on:
                nm_routed = sink == vsink_name
            else:
                nm_routed = not is_easyeffects_sink(sink) and not sink.startswith("NativMix_")
            row.set_routing_state(paused=paused, nm_routed=nm_routed)

    def _remove_app(self, app_name: str) -> None:
        self._config.remove_app_name(self._ch, app_name)
        self._config.save()
        self._refresh_app_list()

    def _remove_hw(self, _=False) -> None:
        self._config.set_hardware_id(self._ch, None)
        self._config.save()
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Mode Switching
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    @_slot_guard
    def _on_mode_toggled(self, checked: bool) -> None:
        mode = "hardware" if checked else "app"
        self._config.set_channel_mode(self._ch, mode)

        # When switching, flush the old assignments to prevent background routing
        if mode == "hardware":
            self._config.set_app_names(self._ch, [])
            if self._config.is_v_sink_enabled(self._ch):
                self._vsink_cb.setChecked(False)  # Disables the V-Sink
        else:
            self._config.set_hardware_id(self._ch, None)

        self._config.save()
        self._apply_mode_ui(checked)
        self._refresh_app_list()

    def _apply_mode_ui(self, is_hw: bool) -> None:
        if is_hw:
            self._add_btn.setText("+ Device")
            self._add_btn.setToolTip("Assign hardware input/output.")
        else:
            self._add_btn.setText("+ App")
            self._add_btn.setToolTip("Assign audio stream.")
        # V-Sink visibility is handled by _refresh_app_list called after this

    # ------------------------------------------------------------------
    # Stream / Hardware picker
    # ------------------------------------------------------------------

    def _open_picker(self, checked: bool = False) -> None:
        if self._config.get_channel_mode(self._ch) == "hardware":
            self._open_hw_picker()
        else:
            self._open_stream_picker()

    def _open_hw_picker(self) -> None:
        sinks = self._backend.get_real_sinks()
        sources = self._backend.get_real_sources()

        current_hw = self._config.get_hardware_id(self._ch)

        assigned_elsewhere = set()
        for i in range(self._config.num_channels):
            if i != self._ch and self._config.get_channel_mode(i) == "hardware":
                val = self._config.get_hardware_id(i)
                if val:
                    assigned_elsewhere.add(val)

        menu = QMenu(self)

        # Outputs
        if sinks:
            out_action = menu.addAction("── Outputs ──")
            out_action.setEnabled(False)
            for desc, name in sorted(sinks, key=lambda x: x[0].lower()):
                hw_id = f"sink:{name}"
                is_vsink = name.startswith("NativMix_")

                action = menu.addAction(desc)
                action.setCheckable(True)
                action.setChecked(hw_id == current_hw)

                if is_vsink or hw_id in assigned_elsewhere:
                    action.setEnabled(False)
                else:
                    action.triggered.connect(lambda _=False, i=hw_id: self._on_hw_picked(i))

        # Inputs
        if sources:
            if sinks:
                menu.addSeparator()
            in_action = menu.addAction("── Inputs ──")
            in_action.setEnabled(False)
            for desc, name in sorted(sources, key=lambda x: x[0].lower()):
                hw_id = f"source:{name}"
                action = menu.addAction(desc)
                action.setCheckable(True)
                action.setChecked(hw_id == current_hw)

                if hw_id in assigned_elsewhere:
                    action.setEnabled(False)
                else:
                    action.triggered.connect(lambda _=False, i=hw_id: self._on_hw_picked(i))

        if not sinks and not sources:
            a = menu.addAction("No hardware found")
            a.setEnabled(False)

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _on_hw_picked(self, hw_id: str) -> None:
        current = self._config.get_hardware_id(self._ch)
        if hw_id == current:
            self._config.set_hardware_id(self._ch, None)
        else:
            self._config.set_hardware_id(self._ch, hw_id)
        self._config.save()
        self._refresh_app_list()

    def _open_stream_picker(self) -> None:
        streams = self._backend.get_active_streams()

        # Determine which apps are assigned elsewhere, and which are here
        already_here = set(self._config.get_app_names(self._ch))
        assigned_elsewhere = set()
        for i in range(self._config.num_channels):
            if i != self._ch:
                assigned_elsewhere.update(self._config.get_app_names(i))

        menu = QMenu(self)

        # Build list of candidate app names from active streams.
        # Track which names come from anonymous streams (pid=0, generic name)
        # so we can show a hint in the menu — the real name is still used for mapping.
        candidates: set[str] = set()
        anonymous_names: set[str] = set()
        for s in streams:
            name = s.app_name
            # Global filter: ignore internal pulse/speech-dispatcher streams
            if "speech-dispatcher" in name.lower() or "dummy" in name.lower():
                continue
            candidates.add(name)
            if s.pid == 0 and name.lower() in GENERIC_PA_NAMES:
                anonymous_names.add(name)

        # Always offer the special pseudo-apps
        candidates.add("System Master")
        candidates.add("Other Apps")

        # Sort: Special apps first, then alphabetically
        def sort_key(name: str) -> tuple[int, str]:
            if name == "System Master":
                return (0, name)
            if name == "Other Apps":
                return (1, name)
            return (2, name.lower())

        added_actions = 0
        for name in sorted(candidates, key=sort_key):
            # Exclusivity: skip if assigned to another channel
            if name in assigned_elsewhere:
                continue

            if name in anonymous_names:
                label = f"{name}  [no process — map by name]"
            else:
                label = name
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(name in already_here)
            if name in ("System Master", "Other Apps"):
                font = action.font()
                font.setBold(True)
                action.setFont(font)

            action.triggered.connect(lambda _=False, n=name: self._on_stream_picked(n))
            added_actions += 1

        if added_actions == 0:
            a = menu.addAction("No available streams")
            a.setEnabled(False)

        menu.addSeparator()
        type_action = menu.addAction("✏  Enter app name…")
        type_action.triggered.connect(self._open_manual_app_input)

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _open_manual_app_input(self, checked: bool = False) -> None:
        name, ok = QInputDialog.getText(self, "Pin App", "App name:")
        if ok and name.strip():
            self._on_stream_picked(name.strip())

    def _on_stream_picked(self, app_name: str) -> None:
        current = self._config.get_app_names(self._ch)
        try:
            if app_name in current:
                self._config.remove_app_name(self._ch, app_name)
            else:
                self._config.update_mapping(app_name, self._ch)
        except ValueError as e:
            _msg = QMessageBox(self)
            _msg.setIcon(QMessageBox.Icon.NoIcon)
            _msg.setWindowTitle("NativMix")
            _msg.setText(f"⚠  {e}")
            _msg.exec()
            # Re-open the picker so the user can choose a different app
            self._open_stream_picker()
            return

        self._config.save()
        self._refresh_app_list()

    def _on_rename(self, new_name: str) -> None:
        self._config.set_channel_label(self._ch, new_name)
        self._config.save()
        self._ch_label.setText(new_name)

    # ------------------------------------------------------------------
    # Inversion
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    @_slot_guard
    def _on_invert_toggled(self, checked: bool) -> None:
        self._config.set_inverted(self._ch, checked)
        self._config.save()
        logger.debug("Channel %d inversion: %s", self._ch, checked)

    def set_other_apps_tooltip(self, names: list[str]) -> None:
        """Dynamically update the tooltip for the 'Other Apps' label."""
        app_names = [n.lower() for n in self._config.get_app_names(self._ch)]
        if "other apps" not in app_names:
            return

        text = "Contains:\n• " + "\n• ".join(names) if names else "No other apps active"

        for i in range(self._app_list_layout.count()):
            item = self._app_list_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, _AppRow) and widget.app_name.lower() == "other apps":
                    widget.set_name_tooltip(text)
                    self._slider.setToolTip(text)
                    break

    @pyqtSlot(bool)
    @_slot_guard
    def _on_vsink_toggled(self, checked: bool) -> None:
        self._config.set_v_sink_enabled(self._ch, checked)
        self._config.save()
        logger.debug("Channel %d V-Sink enabled: %s", self._ch, checked)
        # Inform the backend
        if checked:
            self._backend.enable_v_sink(self._ch)
        else:
            self._backend.disable_v_sink(self._ch)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """
    NativMix main mixer window.

    Pure native Qt style – no QSS, no manual palette colors.
    Responds to KDE dark/light theme switches via QApplication.paletteChanged.
    """

    profile_switch_requested = pyqtSignal(str)  # profile_id
    fader_display_synced = pyqtSignal()

    def __init__(
        self,
        config: ConfigManager,
        backend: AudioBackendBase,
        arduino_thread: ArduinoThread | None = None,
        midi_thread: MidiThread | None = None,
        profile_manager: object | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._backend = backend
        self._arduino = arduino_thread
        self._midi = midi_thread
        self._profile_manager = profile_manager
        self._channels: list[ChannelWidget] = []
        self._last_mode = self._config.input_mode
        self.settings = QSettings("nativmix", "GUI")
        self._reorder_anim: QParallelAnimationGroup | None = None
        self._reorder_animating = False
        self._live_anim: QParallelAnimationGroup | None = None
        self._drag_home: dict[ChannelWidget, QRect] = {}
        self._drag_home_order: list[ChannelWidget] = []
        self._drag_source: ChannelWidget | None = None
        self._live_insert_at: int | None = None
        self._layout_detached = False

        # Guard: set True while a show() is in flight to suppress spurious hide.
        self._show_requested: bool = False

        from nativmix.metadata import __app_name__, __version__

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        # ── Window Flags ──
        # Tool is the correct type for accessory windows on all compositors
        # (KDE Wayland, COSMIC, X11).  Window|SkipTaskbarHint breaks mapping
        # on some Wayland compositors without a valid activation token.
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)

        from nativmix.utils.paths import get_icon_path

        icon_path = get_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        else:
            self.setWindowIcon(QIcon.fromTheme("nativmix", QIcon.fromTheme("audio-volume-high")))

        # UI Stabilization: Fix size to prevent jumping for tiling engines
        self.setMinimumSize(400, 420)
        self.resize(400, 420)

        # Flicker Protection: Disable updates until audit is finished
        self.setUpdatesEnabled(False)

        # ARGB surface is required for border-radius to clip corners correctly.
        # Wayland always supports ARGB; alpha=255 keeps the window opaque when
        # transparency is disabled, but the compositor still sees transparent corners.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._setup_ui()

        # ── Universal Volume Sync ──
        # Delay startup sync slightly to allow background threads to connect.
        QTimer.singleShot(250, self.sync_ui_to_hardware)

    def _setup_ui(self) -> None:
        # ── Central widget ─────────────────────────────────────────────
        central = QFrame()
        central.setObjectName("MainFrame")
        self.setCentralWidget(central)

        self._apply_transparency()
        self._root_layout = QVBoxLayout(central)
        self._root_layout.setContentsMargins(8, 8, 8, 8)
        self._root_layout.setSpacing(6)
        root = self._root_layout

        # ── Collapsible Settings Area & Pin ────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        self._toggle_settings_btn = QRadioButton("Settings")
        self._toggle_settings_btn.setToolTip("Show or hide the settings panel.")
        self._toggle_settings_btn.setAutoExclusive(False)
        self._toggle_settings_btn.setChecked(False)
        self._toggle_settings_btn.toggled.connect(self._on_settings_toggled)

        top_bar.addWidget(self._toggle_settings_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # ── Profile selector ────────────────────────────────────────────
        if self._profile_manager is not None:
            self._profile_combo = QComboBox()
            self._profile_combo.setEditable(True)
            self._profile_combo.setMinimumWidth(120)
            self._profile_combo.setToolTip("Active profile — click to switch, type to rename")
            self._populate_profile_combo()

            self._profile_add_btn = QPushButton("+")
            self._profile_add_btn.setFixedSize(QSize(26, 26))
            self._profile_add_btn.setToolTip("Create new profile")
            self._profile_add_btn.clicked.connect(self._on_add_profile_clicked)

            top_bar.addWidget(self._profile_combo, alignment=Qt.AlignmentFlag.AlignLeft)
            top_bar.addWidget(self._profile_add_btn, alignment=Qt.AlignmentFlag.AlignLeft)

            # Debounce rename: only save after 500 ms of no typing
            self._profile_rename_timer = QTimer(self)
            self._profile_rename_timer.setSingleShot(True)
            self._profile_rename_timer.setInterval(500)
            self._profile_rename_timer.timeout.connect(self._apply_profile_rename)

            self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
            self._profile_combo.editTextChanged.connect(lambda _: self._profile_rename_timer.start())

            self._profile_manager.profile_list_changed.connect(self._populate_profile_combo)
            self._profile_manager.profile_changed.connect(self._on_profile_changed_externally)

        top_bar.addStretch()

        self._pin_btn = QRadioButton("Don't Close")
        self._pin_btn.setToolTip("Keep the window open instead of hiding to tray on close.")
        self._pin_btn.setAutoExclusive(False)
        self._pin_btn.setChecked(self._config.stay_open)
        self._pin_btn.toggled.connect(self._on_pin_toggled)

        self._compact_btn = QRadioButton("Compact")
        self._compact_btn.setToolTip("Hide app assignments and controls — show faders only.")
        self._compact_btn.setAutoExclusive(False)
        self._compact_btn.setChecked(self._config.compact_mode)
        self._compact_btn.toggled.connect(self._on_compact_toggled)

        top_bar.addWidget(self._compact_btn, alignment=Qt.AlignmentFlag.AlignRight)
        top_bar.addWidget(self._pin_btn, alignment=Qt.AlignmentFlag.AlignRight)

        root.addLayout(top_bar)

        self.settings_panel = SettingsPanel(self._config, profile_manager=self._profile_manager)
        self.settings_panel.setVisible(False)
        root.addWidget(self.settings_panel)

        # ── Scrollable channel area ────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        container.setObjectName("channels_container")
        container.setAutoFillBackground(False)
        self._ch_container = container
        self._ch_layout = QHBoxLayout(container)
        self._ch_layout.setContentsMargins(0, 0, 0, 0)
        self._ch_layout.setSpacing(6)
        self._ch_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        # Insert marker drawn in the gap between strips (not on card edges).
        self._drop_gap = QFrame(container)
        self._drop_gap.setFixedWidth(3)
        self._drop_gap.hide()
        self._drop_gap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        scroll.setWidget(container)
        root.addWidget(scroll)

        # ── Add MIDI Channel Button ──
        self._add_midi_btn = QPushButton("+ Add MIDI Channel")
        self._add_midi_btn.clicked.connect(self._on_add_midi_clicked)
        # Visible only in hybrid/midi_only modes. Set visibility initially:
        _midi_mode = self._config.input_mode in ("hybrid", "midi_only")
        _compact = self._config.compact_mode
        self._add_midi_btn.setVisible(_midi_mode and not _compact)

        # ── Edit MIDI Channel Toggle Button ──
        self._edit_midi_btn = QPushButton("✏ Edit MIDI Channel")
        self._edit_midi_btn.setCheckable(True)
        self._edit_midi_btn.setVisible(_midi_mode and not _compact)
        self._edit_midi_btn.toggled.connect(self._on_edit_midi_toggled)

        # ── Size Grip (for frameless resizing) ─────────────────────────
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self._add_midi_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addWidget(self._edit_midi_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addStretch()
        self._size_grip = QSizeGrip(self)
        bottom_layout.addWidget(self._size_grip, alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        root.addLayout(bottom_layout)

        # ── Build initial channels ─────────────────────────────────────
        self._rebuild_channels()
        self.refresh_layout()

        # ── Restore geometry ───────────────────────────────────────────
        geom = self.settings.value("geometry")
        self._has_saved_geometry = bool(geom)
        # Debounce geometry writes: moveEvent/resizeEvent fire on every pixel.
        # The timer is restarted on each call; the actual write happens once,
        # 500 ms after the last movement/resize ends.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(500)
        self._geometry_save_timer.timeout.connect(self._flush_geometry)
        if geom:
            self.restoreGeometry(geom)
            # Guard: if the restored position is off every screen (e.g. after a
            # resolution change or panel resize) move the window to the primary
            # screen's available area so it stays visible.
            win_rect = self.frameGeometry()
            on_screen = any(s.availableGeometry().intersects(win_rect) for s in QApplication.screens())
            if not on_screen:
                logger.debug("Restored geometry is off-screen – resetting to primary screen")
                self.settings.remove("geometry")
                primary = QApplication.primaryScreen()
                if primary:
                    ag = primary.availableGeometry()
                    self.move(ag.x(), ag.y())

        # ── Signal connections ─────────────────────────────────────────
        self._config.mapping_changed.connect(self._on_mapping_changed)
        self._config.settings_changed.connect(self._apply_transparency)
        self._config.settings_changed.connect(self._on_settings_updated)
        self._backend.other_apps_changed.connect(self._on_other_apps_changed)
        if hasattr(self._backend, "routing_status_changed"):
            self._backend.routing_status_changed.connect(self._on_routing_status_changed)

        # Qt emits paletteChanged when the system theme switches – no CSS needed
        QApplication.instance().paletteChanged.connect(self._on_palette_changed)

        self.settings_panel.panic_triggered.connect(self._on_panic_triggered)
        self.settings_panel.master_refresh_requested.connect(self._on_master_refresh)
        self.settings_panel.master_output_changed.connect(self._on_master_changed)
        if self._midi:
            self.settings_panel.midi_panic_triggered.connect(self._midi.restart_midi)
        # ── Initial Population ──
        self._on_master_refresh()

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def _rebuild_channels(self) -> None:
        self._stop_live_anim()
        if self._reorder_anim is not None:
            self._reorder_anim.stop()
            self._reorder_anim.deleteLater()
            self._reorder_anim = None
        self._reorder_animating = False
        self._clear_live_reorder_state()
        self._layout_detached = False
        # Layout Batching: Disable layout updates during population
        self._ch_layout.setEnabled(False)
        try:
            while self._ch_layout.count():
                item = self._ch_layout.takeAt(0)
                widget = item.widget()
                if widget:
                    if isinstance(widget, ChannelWidget) and widget.is_midi_channel and self._midi:
                        try:
                            self._midi.midi_cc_received.disconnect(widget.handle_midi_input)
                        except RuntimeError:
                            pass
                    widget.deleteLater()
            self._channels.clear()

            by_id = {int(ch_dict["index"]): ch_dict for ch_dict in self._config.all_channels()}
            for i in self._config.get_channel_order():
                ch_dict = by_id.get(i)
                if ch_dict is None:
                    continue
                is_midi = ch_dict.get("is_midi", False)
                # In USB mode MIDI widgets are purged by refresh_layout anyway;
                # skip creating them to avoid the wasted create-then-destroy cycle.
                if is_midi and self._config.input_mode == "usb":
                    continue
                w = ChannelWidget(i, self._config, self._backend, is_midi=is_midi)
                w.strip_drop.connect(self._on_strip_drop)
                w.reorder_tracking.connect(self._on_reorder_tracking)
                w.reorder_active_changed.connect(lambda active, ww=w: self._on_reorder_session(active, ww))
                self._channels.append(w)
                # Ensure MIDI-relevant signals are connected even after rebuild
                if w.is_midi_channel and self._midi:
                    self._midi.midi_cc_received.connect(w.handle_midi_input)
                # Apply current edit mode so buttons show/hide correctly —
                # kept outside the self._midi guard so it fires on every rebuild.
                if w.is_midi_channel and hasattr(self, "_edit_midi_btn"):
                    w.set_edit_mode(self._edit_midi_btn.isChecked())

                # Apply compact mode
                if hasattr(self, "_compact_btn"):
                    w.set_compact_mode(self._compact_btn.isChecked())

                self._ch_layout.addWidget(w)

            self._ch_layout.addStretch()
        finally:
            self._ch_layout.setEnabled(True)
            self._ch_layout.update()

    def _channel_widget(self, channel_index: int) -> ChannelWidget | None:
        """Return the strip widget for a stable channel id (not display slot)."""
        for widget in self._channels:
            if widget.channel_index == channel_index:
                return widget
        return None

    def _clear_drop_hints(self) -> None:
        self._drop_gap.hide()

    def _detach_channel_layout(self) -> None:
        """Take strips out of QHBoxLayout so geometries can be animated."""
        if self._layout_detached:
            return
        while self._ch_layout.count():
            item = self._ch_layout.takeAt(0)
            del item
        for w in self._channels:
            w.setParent(self._ch_container)
            w.show()
        self._layout_detached = True

    def _reattach_channel_layout(self, widgets: list[ChannelWidget] | None = None) -> None:
        """Put strips back into left-to-right layout order."""
        if widgets is None:
            widgets = list(self._channels)
        while self._ch_layout.count():
            item = self._ch_layout.takeAt(0)
            del item
        for w in widgets:
            self._ch_layout.addWidget(w)
            w.set_drag_blocked(False)
        self._ch_layout.addStretch()
        self._layout_detached = False
        _force_clear_cursor_overrides()

    def _stop_live_anim(self) -> None:
        if self._live_anim is not None:
            self._live_anim.stop()
            self._live_anim.deleteLater()
            self._live_anim = None

    def _clear_live_reorder_state(self) -> None:
        self._drag_home = {}
        self._drag_home_order = []
        self._drag_source = None
        self._live_insert_at = None

    def _insert_index_at(self, global_pos: QPoint) -> int | None:
        """Visual insert slot 0..n from pointer x (uses home rects while live-dragging)."""
        widgets = self._drag_home_order or self._channels
        if not widgets:
            return None
        local_x = self._ch_container.mapFromGlobal(global_pos).x()
        for i, widget in enumerate(widgets):
            geo = self._drag_home.get(widget, widget.geometry())
            if local_x < geo.center().x():
                return i
        return len(widgets)

    def _live_preview_targets(self, insert_at: int) -> dict[ChannelWidget, QRect]:
        """Packed row with a source-sized hole at *insert_at* (home-order coordinates)."""
        source = self._drag_source
        if source is None or not self._drag_home_order:
            return {}
        others = [w for w in self._drag_home_order if w is not source]
        src_i = self._drag_home_order.index(source)
        adj = insert_at
        if src_i < adj:
            adj -= 1
        adj = max(0, min(adj, len(others)))
        spacing = self._ch_layout.spacing()
        src_home = self._drag_home[source]
        gap_w = src_home.width() + spacing
        x = 0
        targets: dict[ChannelWidget, QRect] = {}
        placed_source = False
        for i, w in enumerate(others):
            if i == adj:
                targets[source] = QRect(x, src_home.y(), src_home.width(), src_home.height())
                x += gap_w
                placed_source = True
            home = self._drag_home[w]
            targets[w] = QRect(x, home.y(), home.width(), home.height())
            x += home.width() + spacing
        if not placed_source:
            targets[source] = QRect(x, src_home.y(), src_home.width(), src_home.height())
        return targets

    def _animate_geometries(
        self,
        targets: dict[ChannelWidget, QRect],
        duration: int,
        on_finished: object | None = None,
        *,
        live: bool = False,
    ) -> None:
        """Animate widgets to *targets*. *live* uses the live-gap animation slot."""
        if live:
            self._stop_live_anim()
        else:
            if self._reorder_anim is not None:
                self._reorder_anim.stop()
                self._reorder_anim.deleteLater()
                self._reorder_anim = None

        group = QParallelAnimationGroup(self)
        for w, end in targets.items():
            start = QRect(w.geometry())
            if start == end:
                continue
            anim = QPropertyAnimation(w, b"geometry", group)
            anim.setDuration(duration)
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(anim)

        if group.animationCount() == 0:
            if on_finished is not None:
                on_finished()
            return

        if on_finished is not None:
            group.finished.connect(on_finished)
        if live:
            self._live_anim = group
        else:
            self._reorder_anim = group
        group.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def _on_reorder_session(self, active: bool, source: ChannelWidget) -> None:
        """Begin/end a live-gap reorder; drop animation takes over if already running."""
        if active:
            if self._reorder_animating or self._drag_source is not None:
                return
            self._drag_source = source
            self._drag_home_order = list(self._channels)
            self._drag_home = {w: QRect(w.geometry()) for w in self._channels}
            self._live_insert_at = None
            self._detach_channel_layout()
            source.raise_()
            return
        self._clear_drop_hints()
        if self._reorder_animating:
            return
        # Drag cancelled or dropped in the original slot — slide home, then reattach.
        if self._drag_home:
            homes = dict(self._drag_home)
            order = list(self._drag_home_order)

            def _restore() -> None:
                self._channels = order
                self._reattach_channel_layout(order)
                self._clear_live_reorder_state()

            self._animate_geometries(homes, _STRIP_LIVE_ANIM_MS, _restore, live=True)
        elif self._layout_detached:
            self._reattach_channel_layout()
            self._clear_live_reorder_state()

    def _on_reorder_tracking(self, global_pos: QPoint) -> None:
        """Open a live insert gap by sliding neighbours as the pointer moves."""
        if self._drag_source is None or self._reorder_animating:
            return
        insert_at = self._insert_index_at(global_pos)
        if insert_at is None:
            return
        if insert_at == self._live_insert_at:
            return
        self._live_insert_at = insert_at
        targets = self._live_preview_targets(insert_at)
        if targets:
            self._animate_geometries(targets, _STRIP_LIVE_ANIM_MS, live=True)

    def _on_strip_drop(self, source_id: int, global_pos: QPoint) -> None:
        self._clear_drop_hints()
        if self._reorder_animating:
            return
        visual = [w.channel_index for w in (self._drag_home_order or self._channels)]
        if source_id not in visual:
            return
        insert_at = self._insert_index_at(global_pos)
        if insert_at is None:
            return
        src_visual = visual.index(source_id)
        if src_visual < insert_at:
            insert_at -= 1
        visual = [cid for cid in visual if cid != source_id]
        insert_at = max(0, min(insert_at, len(visual)))
        visual.insert(insert_at, source_id)

        full = self._config.get_channel_order()
        hidden = [cid for cid in full if cid not in visual]
        new_order = visual + hidden
        if new_order == full:
            return
        self._config.set_channel_order(new_order)
        if self._profile_manager is not None:
            self._profile_manager.save_current(self._config.all_channels(), self._config.get_channel_order())
        self._stop_live_anim()
        self._animate_channel_reorder(visual)

    def _animate_channel_reorder(self, visual_ids: list[int]) -> None:
        """Slide existing strips to their new row positions, then reattach to the layout."""
        by_id = {w.channel_index: w for w in (self._drag_home_order or self._channels)}
        widgets = [by_id[cid] for cid in visual_ids if cid in by_id]
        if len(widgets) < 2:
            self._clear_live_reorder_state()
            self._rebuild_channels()
            return

        for w in widgets:
            w.setGraphicsEffect(None)

        self._detach_channel_layout()
        starts = {w: QRect(w.geometry()) for w in widgets}
        spacing = self._ch_layout.spacing()
        x = 0
        targets: dict[ChannelWidget, QRect] = {}
        for w in widgets:
            start = starts[w]
            targets[w] = QRect(x, start.y(), start.width(), start.height())
            x += start.width() + spacing
            w.setParent(self._ch_container)
            w.show()

        self._channels = widgets
        self._reorder_animating = True
        for w in widgets:
            w.set_drag_blocked(True)

        def _finish() -> None:
            self._reorder_animating = False
            self._reorder_anim = None
            self._reattach_channel_layout(widgets)
            self._clear_live_reorder_state()

        self._animate_geometries(targets, _STRIP_DROP_ANIM_MS, _finish, live=False)

    def finalize_ui(self) -> None:
        """Called once hardware/audio audit is complete to enable rendering."""
        if not self.updatesEnabled():
            logger.debug("MainWindow: Hardware audit complete. Enabling UI updates.")
            self.setUpdatesEnabled(True)
            self.update()

    def set_show_requested(self, value: bool) -> None:
        """Set the show-in-flight guard flag (used by tray and IPC show handlers)."""
        self._show_requested = value

    def set_force_quit(self) -> None:
        """Mark the window for a real quit so closeEvent does not intercept it."""
        self._force_quit = True

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    @_slot_guard
    def on_volumes_changed(self, volumes: list[float]) -> None:
        for i, vol in enumerate(volumes):
            # Update persistent in-memory state
            self._config.set_channel_volume(i, vol)

            widget = self._channel_widget(i)
            if widget is not None:
                widget.set_volume(vol)

    def sync_sliders_from_config(self) -> None:
        """Refresh on-screen fader positions from persisted profile/config volumes."""
        for i in range(self._config.num_channels):
            widget = self._channel_widget(i)
            if widget is None:
                continue
            widget.set_volume(self._config.get_channel_volume(i))
        logger.debug("Slider positions synced from config/profile")
        self.fader_display_synced.emit()

    @pyqtSlot(int, float)
    @_slot_guard
    def on_channel_volume_changed(self, channel_index: int, volume: float) -> None:
        widget = self._channel_widget(channel_index)
        if widget is not None:
            widget.set_volume(volume)

    @pyqtSlot(bool)
    @_slot_guard
    def on_midi_connection_changed(self, connected: bool) -> None:
        """Reset Learn mode for all channels if connection is lost."""
        if not connected:
            logger.debug("MainWindow: MIDI connection lost, resetting Learn state.")
            for widget in self._channels:
                widget.cancel_learn()

    @pyqtSlot(int, int, int)
    @_slot_guard
    def on_midi_cc_received(self, midi_channel: int, control_number: int, value: int) -> None:
        """
        Central Learn handshake for both volume-CC and mute-CC.
        Iterates all channels and acts on the first one that is in learn mode.
        A single break ensures one CC event never assigns to multiple channels.
        Mute-CC learn only captures on value==127 (button press) so fader
        movements cannot accidentally complete the learn.
        """
        for widget in self._channels:
            if not widget.isVisible():
                continue
            if widget.is_waiting_for_volume_learn():
                self._config.set_midi_cc(widget.channel_index, control_number, midi_channel=midi_channel)
                widget.update_midi_cc(control_number, midi_channel=midi_channel)
                logger.debug(
                    "Volume Learn: M%d/CC%d → channel %d",
                    midi_channel + 1,
                    control_number,
                    widget.channel_index,
                )
                break
            if widget.is_waiting_for_mute_learn() and value == 127:
                self._config.set_midi_mute_cc(widget.channel_index, control_number, midi_channel=midi_channel)
                widget.update_midi_mute_cc(control_number, midi_channel=midi_channel)
                logger.debug(
                    "Mute-CC Learn: M%d/CC%d → channel %d",
                    midi_channel + 1,
                    control_number,
                    widget.channel_index,
                )
                break

    @pyqtSlot(int)
    @_slot_guard
    def on_channel_count_changed(self, n: int) -> None:
        if n == self._config.hw_channel_count:
            return
        logger.debug("Channel count changed to %d – rebuilding GUI", n)
        self._config.num_channels = n
        self._config.save()
        self._rebuild_channels()
        self.refresh_layout()

    @pyqtSlot(int, list)
    @_slot_guard
    def _on_mapping_changed(self, channel_index: int, _names: list[str]) -> None:
        """
        Refresh ALL channels when a mapping changes, so the + App menus
        immediately reflect the new exclusivity rules.
        """
        for ch in self._channels:
            ch.refresh()

    @pyqtSlot(int, bool)
    @_slot_guard
    def on_mute_state_changed(self, channel_index: int, is_muted: bool) -> None:
        widget = self._channel_widget(channel_index)
        if widget is not None:
            widget.set_mute_state(is_muted)

    def open_settings(self) -> None:
        """Open the settings panel (called from tray icon)."""
        self._toggle_settings_btn.setChecked(True)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_settings_toggled(self, checked: bool) -> None:
        self.settings_panel.setVisible(checked)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_pin_toggled(self, checked: bool) -> None:
        self._config.stay_open = checked
        self._config.save()
        logger.debug("Stay Open (Pin) toggled: %s", checked)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_compact_toggled(self, checked: bool) -> None:
        if checked:
            # Remember current height before hiding content
            self._pre_compact_height = self.height()
        self._config.compact_mode = checked
        for w in self._channels:
            w.set_compact_mode(checked)
        if hasattr(self, "_add_midi_btn"):
            _midi_mode = self._config.input_mode in ("hybrid", "midi_only")
            self._add_midi_btn.setVisible(_midi_mode and not checked)
            self._edit_midi_btn.setVisible(_midi_mode and not checked)
        if checked:
            # Tighten margins and spacing in compact mode; hide grip to save space
            self._root_layout.setContentsMargins(8, 8, 8, 2)
            self._root_layout.setSpacing(4)
            self._size_grip.setVisible(False)
            # Allow window to shrink below normal minimum temporarily
            self.setMinimumHeight(0)

            # Shrink to fit; then lock minimum to compact height so user can't go smaller
            def _do_compact_resize():
                QApplication.processEvents()
                m = self._root_layout.contentsMargins()
                sp = self._root_layout.spacing()
                top_h = self._toggle_settings_btn.height()
                ch_h = self._channels[0].sizeHint().height() if self._channels else 200
                h = m.top() + top_h + sp + ch_h + m.bottom()
                logger.debug("Compact resize: top=%d ch=%d → h=%d", top_h, ch_h, h)
                # setFixedHeight forces the resize even if the WM ignores resize()
                self.setFixedHeight(h)

                # Immediately release fixed constraint so user can still resize larger
                def _release_height_constraint() -> None:
                    self.setMinimumHeight(h)
                    self.setMaximumHeight(16777215)

                QTimer.singleShot(0, _release_height_constraint)

            QTimer.singleShot(0, _do_compact_resize)
        else:
            # Restore normal margins, spacing, grip and minimum height
            self._root_layout.setContentsMargins(8, 8, 8, 8)
            self._root_layout.setSpacing(6)
            self._size_grip.setVisible(True)
            self.setMinimumHeight(420)
            # Restore saved height
            saved = getattr(self, "_pre_compact_height", None)
            if saved:
                QTimer.singleShot(0, lambda: self.resize(self.width(), saved))
        logger.debug("Compact mode toggled: %s", checked)

    @_slot_guard
    def _on_palette_changed(self, _palette=None) -> None:
        """
        Called by Qt when the system theme changes (dark ↔ light or accent changes).
        Re-apply the glass look and our dynamic styling hooks.
        """
        logger.debug("System palette changed – repainting and syncing theme")

        # 2. Update window background (transparency)
        self._apply_transparency()

        # 3. Cascade redraws to all channels (Labels, etc.)
        for ch in self._channels:
            ch.refresh_theme()

        self.repaint()

    @pyqtSlot()
    @_slot_guard
    def _on_settings_updated(self) -> None:
        # 1. Rebuild channels if mode or count changed
        mode_changed = self._last_mode != self._config.input_mode
        # In USB mode MIDI widgets are not built, so compare against hw count only.
        expected_widgets = (
            self._config.hw_channel_count if self._config.input_mode == "usb" else self._config.num_channels
        )
        count_changed = len(self._channels) != expected_widgets

        if mode_changed or count_changed:
            logger.debug("Mode or count changed (%s -> %s) – rebuilding GUI", self._last_mode, self._config.input_mode)
            self._last_mode = self._config.input_mode
            self._rebuild_channels()

        # 2. Centralized UI refresh and mode-specific state
        self.refresh_layout()

        # 3. Update existing widgets
        for ch in self._channels:
            ch.update_settings()

    def refresh_layout(self) -> None:
        """
        Centralized UI refresh logic for input modes (usb, hybrid, midi_only).
        """
        mode = self._config.input_mode
        logger.debug("Centralized UI refresh for mode: %s", mode)

        # 1. Thread Management & App Cleanup
        if mode == "usb":
            # MidiThread handles USB-idle internally via set_mode() (called via
            # config.settings_changed signal in main.py).  We do NOT stop/start
            # the thread here so the ALSA virtual port stays alive across mode
            # switches and doesn't accumulate duplicate ports.

            # CLEAR app assignments from MIDI channels so they don't block apps
            self._config.clear_midi_channel_mappings()

            # FULL PURGE of MIDI widgets from memory/UI
            remaining_channels = []
            for widget in self._channels:
                if widget.is_midi_channel:
                    logger.debug("Purging MIDI widget: index=%d", widget.channel_index)
                    self._ch_layout.removeWidget(widget)
                    # Disconnect signal before deleteLater() to prevent a
                    # midi_cc_received firing on a half-destroyed widget.
                    if self._midi:
                        try:
                            self._midi.midi_cc_received.disconnect(widget.handle_midi_input)
                        except RuntimeError:
                            pass  # Already disconnected
                    widget.deleteLater()
                else:
                    remaining_channels.append(widget)
            self._channels = remaining_channels
        else:
            # Hybrid or MIDI Only: ensure thread is running (first launch only –
            # subsequent mode switches are handled by MidiThread internally).
            if self._midi and not self._midi.isRunning():
                logger.debug("Starting MIDI thread for %s mode", mode)
                self._midi.start()

        # 2. USB specific logic
        if mode == "midi_only":
            self._config.clear_usb_channel_mappings()
            if self._arduino and self._arduino.isRunning():
                # We don't stop the arduino thread (discovery), but backend blocks it.
                pass
        elif mode in ("usb", "hybrid") and self._arduino:
            if not self._arduino.isRunning():
                try:
                    logger.debug("Attempting to restart Arduino thread for %s mode", mode)
                    self._arduino.start()
                except Exception as exc:
                    logger.error("Failed to start Arduino thread: %s", exc)

        # 3. Universal Synchronization
        # Push ANY change to Backend + UI immediately
        self.sync_ui_to_hardware()

        # 4. Visibility logic (Clean Hide/Show)
        if hasattr(self, "_add_midi_btn"):
            _midi_mode = mode in ("hybrid", "midi_only")
            _compact = self._config.compact_mode
            self._add_midi_btn.setVisible(_midi_mode and not _compact)
            self._edit_midi_btn.setVisible(_midi_mode and not _compact)

        for widget in self._channels:
            is_midi = widget.is_midi_channel
            if mode == "usb":
                # Hide MIDI, show USB
                widget.setVisible(not is_midi)
            elif mode == "midi_only":
                # Hide USB, show MIDI
                widget.setVisible(is_midi)
            else:
                # Hybrid: show all
                widget.setVisible(True)

        # 4. Layout Stabilization
        if self.layout():
            self.layout().activate()

    def sync_ui_to_hardware(self) -> None:
        """
        Pull latest volumes from Arduino and MIDI threads and push to Backend + UI.
        Crucial for startup and mode transitions to prevent jumps.
        """
        logger.debug("Universal Volume Sync triggered")
        mode = self._config.input_mode
        hardware_synced = False

        # 1. Arduino Sync
        # Only if we are in a mode that uses hardware
        if mode in ("usb", "hybrid") and self._arduino:
            try:
                if self._arduino.has_real_data:
                    hw_vols = self._arduino.get_last_volumes()
                    logger.debug("Syncing Arduino volumes: %s", hw_vols)
                    self.on_volumes_changed(hw_vols)
                    self._backend.apply_poti_volumes(hw_vols)
                    hardware_synced = True
                else:
                    logger.debug("Arduino sync: no real data yet – keeping profile/config volumes for UI")
            except Exception as exc:
                logger.error("Arduino sync failed: %s", exc)

        # 2. MIDI Sync
        if mode in ("hybrid", "midi_only") and self._midi:
            try:
                mapped = self._midi.get_mapped_volumes()
                if mapped:
                    logger.debug("Syncing MIDI volumes: %s", mapped)
                    self._backend.apply_midi_volumes(mapped)
                    for ch, vol in mapped:
                        self._config.set_channel_volume(ch, vol)
                        self.on_channel_volume_changed(ch, vol)
                    hardware_synced = True
            except Exception as exc:
                logger.error("MIDI sync failed: %s", exc)

        if not hardware_synced:
            self.sync_sliders_from_config()

    @pyqtSlot(bool)
    @_slot_guard
    def _on_add_midi_clicked(self, checked: bool = False) -> None:
        self._config.add_midi_channel()
        # The add_midi_channel method emits settings_changed, which triggers _on_settings_updated,
        # which detects the length difference and rebuilds.

    @pyqtSlot(bool)
    @_slot_guard
    def _on_edit_midi_toggled(self, checked: bool) -> None:
        for w in self._channels:
            if w.is_midi_channel:
                w.set_edit_mode(checked)

    # ------------------------------------------------------------------
    # Profile selector helpers
    # ------------------------------------------------------------------

    def _populate_profile_combo(self) -> None:
        """Rebuild the profile combo from ProfileManager (blocks signals to avoid loops)."""
        if not hasattr(self, "_profile_combo") or self._profile_manager is None:
            return
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for p in self._profile_manager.list_profiles():
            self._profile_combo.addItem(p["name"], userData=p["id"])
        active_id = self._profile_manager.active_profile_id
        for i in range(self._profile_combo.count()):
            if self._profile_combo.itemData(i) == active_id:
                self._profile_combo.setCurrentIndex(i)
                break
        self._profile_combo.blockSignals(False)

    @pyqtSlot(int)
    @_slot_guard
    def _on_profile_selected(self, index: int) -> None:
        if self._profile_manager is None or index < 0:
            return
        profile_id = self._profile_combo.itemData(index)
        if profile_id and profile_id != self._profile_manager.active_profile_id:
            self.profile_switch_requested.emit(profile_id)

    @pyqtSlot(str)
    @_slot_guard
    def _on_profile_changed_externally(self, profile_id: str) -> None:
        """Update combo when profile changes from IPC or MIDI (not from the combo itself)."""
        if not hasattr(self, "_profile_combo"):
            return
        self._profile_combo.blockSignals(True)
        for i in range(self._profile_combo.count()):
            if self._profile_combo.itemData(i) == profile_id:
                self._profile_combo.setCurrentIndex(i)
                break
        self._profile_combo.blockSignals(False)

    @_slot_guard
    def _apply_profile_rename(self) -> None:
        """Debounced rename: save the text currently in the combo as the active profile name."""
        if self._profile_manager is None or not hasattr(self, "_profile_combo"):
            return
        new_name = self._profile_combo.currentText().strip()
        active_id = self._profile_manager.active_profile_id
        if new_name and active_id:
            try:
                current_name = self._profile_manager.load(active_id).get("name", "")
                if new_name != current_name:
                    self._profile_manager.rename(active_id, new_name)
            except Exception:
                logger.exception("Error renaming profile")

    @pyqtSlot(bool)
    @_slot_guard
    def _on_add_profile_clicked(self, checked: bool = False) -> None:
        if self._profile_manager is None:
            return
        # Flush any pending changes to the current profile before copying
        self._profile_manager.save_current(self._config.all_channels(), self._config.get_channel_order())
        names = {p["name"] for p in self._profile_manager.list_profiles()}
        n = len(names) + 1
        candidate = f"Profile {n}"
        while candidate in names:
            n += 1
            candidate = f"Profile {n}"
        new_id = self._profile_manager.create(
            candidate,
            channel_count=self._config.hw_channel_count,
            channels=self._config.all_channels(),
            channel_order=self._config.get_channel_order(),
        )
        self.profile_switch_requested.emit(new_id)
        # Defer focus/select until after the event loop processes the switch signal
        if hasattr(self, "_profile_combo"):
            QTimer.singleShot(
                0,
                lambda: (self._profile_combo.setFocus(), self._profile_combo.lineEdit().selectAll())
                if hasattr(self, "_profile_combo")
                else None,
            )

    def _apply_transparency(self) -> None:
        """
        Applies a semi-transparent background to the main window.

        Under Fusion (Flatpak), child frames/group boxes otherwise paint opaque
        Window fills — so when transparency is on we force container backgrounds
        transparent. Faders keep their own opaque groove/handle stylesheets.
        """
        transparent = bool(self._config.transparency)
        # WA_TranslucentBackground stays always-on (set at init); only alpha changes.

        sys_color = self.palette().color(QPalette.ColorRole.Window)
        if transparent:
            alpha = 200  # Transparency (semi-transparent, but readable)
        else:
            alpha = 255  # Solid (Standard System-Theme)

        rgba_string = f"rgba({sys_color.red()}, {sys_color.green()}, {sys_color.blue()}, {alpha})"
        if transparent:
            # Keep containers glass-clear; interactive controls (combo/slider via
            # their own styles) stay readable. Do not blanket-clear every QWidget
            # — that would wash out combo popups and buttons.
            self.setStyleSheet(
                f"#MainFrame {{ background-color: {rgba_string}; border-radius: 12px; }}"
                "#MainFrame QFrame,"
                "#MainFrame QGroupBox,"
                "#MainFrame QScrollArea,"
                "#MainFrame QAbstractScrollArea::viewport,"
                "#MainFrame #channels_container,"
                "#MainFrame #app_list_widget {"
                " background-color: transparent;"
                "}"
                "#MainFrame QSlider {"
                " background-color: transparent;"
                "}"
            )
        else:
            self.setStyleSheet(f"#MainFrame {{ background-color: {rgba_string}; border-radius: 12px; }}")

        # Force a repaint to safely apply KWin compositor changes on-the-fly
        self.repaint()

    @pyqtSlot(list)
    @_slot_guard
    def _on_other_apps_changed(self, names: list[str]) -> None:
        """Dynamically updates the tooltip for the 'Other Apps' channel."""
        for ch_widget in self._channels:
            ch_widget.set_other_apps_tooltip(names)

    @pyqtSlot()
    @_slot_guard
    def _on_routing_status_changed(self) -> None:
        """Refresh app-row colors when live sink destinations change."""
        for ch_widget in self._channels:
            if not ch_widget._compact:
                ch_widget.refresh_app_routing_styles()

    @pyqtSlot()
    @_slot_guard
    def _on_panic_triggered(self) -> None:
        """Reset all apps to default sink, destroy V-Sinks, clear mappings."""
        reply = QMessageBox.question(
            self,
            "Panic Reset",
            "This will destroy all virtual cables and move all apps back to the system default output."
            "\n\nAre you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # 1. Backend reset
            self._backend.panic_reset()
            # 2. Config purge
            for i in range(self._config.num_channels):
                self._config.set_app_names(i, [])
                self._config.set_v_sink_enabled(i, False)
            self._config.save()
            # 3. GUI refresh
            self._rebuild_channels()
            self._on_master_refresh()
            logger.debug("Panic Reset completed from GUI.")

    @pyqtSlot()
    @_slot_guard
    def _on_master_refresh(self) -> None:
        """Fetch real sinks and update the settings panel dropdown."""
        sinks = self._backend.get_real_sinks()
        default = self._backend.get_default_sink_name()
        self.settings_panel.populate_master_outputs(sinks, default)

    @pyqtSlot(str)
    @_slot_guard
    def _on_master_changed(self, sink_name: str) -> None:
        """Set the new default sink and route loopbacks."""
        self._backend.set_default_sink_and_move_loopbacks(sink_name)

    # ------------------------------------------------------------------
    # Close → conditionally hide to tray or actually close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.settings.setValue("geometry", self.saveGeometry())

        # If the Tray Icon called "Quit NativMix", we must accept the event
        # so QApplication.quit() can actually terminate the application.
        if getattr(self, "_force_quit", False):
            logger.debug("MainWindow force-closing, stopping background threads")
            # Block signals before stop() so in-flight emissions during the
            # 2-second graceful-wait window cannot reach already-torn-down slots.
            if self._arduino:
                self._arduino.blockSignals(True)
                self._arduino.stop()
            if self._midi:
                self._midi.blockSignals(True)
                self._midi.stop()
            event.accept()
            return

        # Always accept so the Wayland compositor can proceed (e.g. system shutdown).
        # WA_DeleteOnClose is not set → Qt hides the window, app stays alive via tray.
        event.accept()
        if self._config.stay_open:
            # "Don't Close": re-show in the next event-loop tick so the window
            # stays visible for the user. During system shutdown the event loop
            # exits before the timer fires → window stays hidden → shutdown proceeds.
            QTimer.singleShot(0, self.show)
            logger.debug("Close event accepted, re-showing (Stay Open is ON)")
        else:
            logger.debug("Window closed/hidden to tray (Stay Open is OFF)")

    # ------------------------------------------------------------------
    # Drag & Auto-Hide on Focus Loss (Applet Behavior)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        """Native Wayland Window Move. No manual coordinate math needed."""
        if event.button() == Qt.MouseButton.LeftButton and not self._hit_strip_drag_handle(event):
            if self.windowHandle():
                self.windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def _hit_strip_drag_handle(self, event) -> bool:
        """True when the press landed on a channel strip reorder handle."""
        hit = self.childAt(event.position().toPoint())
        w = hit
        while w is not None and w is not self:
            if bool(w.property("nativmix_strip_drag")):
                # Compact mode: handles are inactive (property still set).
                parent = w.parentWidget()
                while parent is not None and parent is not self:
                    if isinstance(parent, ChannelWidget) and parent._compact:
                        return False
                    parent = parent.parentWidget()
                return True
            w = w.parentWidget()
        return False

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange:
            active = self.isActiveWindow()
            show_req = getattr(self, "_show_requested", False)
            active_widget = QApplication.activeWindow()
            logger.debug(
                "changeEvent ActivationChange: isActiveWindow=%s _show_requested=%s "
                "isVisible=%s activeWindow=%s stay_open=%s",
                active,
                show_req,
                self.isVisible(),
                type(active_widget).__name__ if active_widget else None,
                self._config.stay_open,
            )
            if not active:
                # Suppress auto-hide while a show request is in flight.
                if show_req:
                    logger.debug("changeEvent: _show_requested active – skipping auto-hide")
                    super().changeEvent(event)
                    return
                # Don't hide if a child dialog (e.g. QMessageBox) is currently active
                if active_widget is self or (active_widget is not None and active_widget.parent() is not None):
                    logger.debug("changeEvent: child dialog or self active – keeping visible")
                elif not self._config.stay_open:
                    self._save_geometry()
                    self.hide()
                    logger.debug("Window auto-hidden on focus loss")
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            for ch in self._channels:
                ch.cancel_learn()
        super().keyPressEvent(event)

    def moveEvent(self, event) -> None:
        self._save_geometry()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._save_geometry()
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        g = self.geometry()
        logger.debug(
            "showEvent: geometry=(%d,%d %dx%d) isActiveWindow=%s _show_requested=%s",
            g.x(),
            g.y(),
            g.width(),
            g.height(),
            self.isActiveWindow(),
            getattr(self, "_show_requested", False),
        )
        super().showEvent(event)
        self.sync_sliders_from_config()
        # Dirty X11 trick for GNOME: Mutter's smart placement overrides the
        # position set by restoreGeometry(). Capture pos before the compositor
        # moves it and reapply after the placement round-trip (~80 ms).
        if self._has_saved_geometry and (_is_gnome_x11() or _is_kde_x11()):
            target = self.pos()
            QTimer.singleShot(80, lambda: self.move(target))

    def hideEvent(self, event) -> None:
        logger.debug("hideEvent fired (caller will be in traceback if needed)")
        super().hideEvent(event)

    def _save_geometry(self) -> None:
        """Schedule a debounced geometry write (500 ms after the last call)."""
        self._geometry_save_timer.start()  # restarts if already running

    def _flush_geometry(self) -> None:
        """Write the current geometry to QSettings (called by debounce timer)."""
        if self.isVisible():
            self.settings.setValue("geometry", self.saveGeometry())
            logger.debug("Window geometry saved")
