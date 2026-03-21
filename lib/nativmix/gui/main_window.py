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

import functools
import logging
import os
import time
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, pyqtSlot, QEvent, QSettings
from PyQt6.QtGui import QGuiApplication, QIcon, QPixmap, QPalette, QColor, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QSizeGrip,
    QMessageBox,
)

from nativmix.gui.settings_panel import SettingsPanel

if TYPE_CHECKING:
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.audio.manager import PipeWireManager

logger = logging.getLogger(__name__)


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


def _slot_guard(func):
    """Catch exceptions in Qt slots, log them, and continue running."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("Unhandled exception in slot %s", func.__qualname__)
    return wrapper


_CHANNEL_MIN_WIDTH = 60


# ---------------------------------------------------------------------------
# Editable channel label (double-click to rename)
# ---------------------------------------------------------------------------

class _EditableChannelLabel(QLabel):
    """QLabel that opens a rename dialog on double-click."""

    rename_requested = pyqtSignal(str)

    def mouseDoubleClickEvent(self, event) -> None:
        text, ok = QInputDialog.getText(
            self, "Rename Channel", "Name:", text=self.text()
        )
        if ok and text.strip():
            self.rename_requested.emit(text.strip())
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------
# Single mapped-app row (remove button + name)
# ---------------------------------------------------------------------------

class _AppRow(QWidget):
    """[×] [name]  – one per assigned app inside a channel."""

    def __init__(self, app_name: str, on_remove, parent=None) -> None:
        super().__init__(parent)
        self.app_name = app_name
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._remove_btn = QToolButton()
        self._remove_btn.setIcon(QIcon.fromTheme('list-remove'))
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
        
        elided = self._name_label.fontMetrics().elidedText(
            app_name, Qt.TextElideMode.ElideRight, 60
        )
        self._name_label.setText(elided)

        layout.addWidget(self._remove_btn)
        layout.addWidget(self._name_label)
        
        self.update_dynamic_styles()

    def update_dynamic_styles(self) -> None:
        """Tint the X button to match the system Highlight color and apply custom hover state."""
        palette = QApplication.palette()
        accent_color = palette.color(QPalette.ColorRole.Highlight)
        accent_hex = accent_color.name()
        
        base_icon = QIcon.fromTheme('list-remove').pixmap(18, 18)
        
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
        
        # Also color the app name label
        # Use QPalette instead of setStyleSheet to avoid breaking native tooltips on Wayland
        pal = self._name_label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, accent_color)
        self._name_label.setPalette(pal)


# ---------------------------------------------------------------------------
# VU Slider — native QSlider + lightweight transparent overlay for animation
# ---------------------------------------------------------------------------

class _VuOverlay(QWidget):
    """
    Transparent child widget painted on top of _VuSlider.

    Separating VU animation from the native slider is the key performance fix:
    the expensive Kvantum/Breeze style render in QSlider.paintEvent() only
    fires when the fader VALUE changes.  The overlay repaints independently
    (only 2 fillRect calls per frame) so VU animation never triggers a full
    native style re-render.
    """

    _DECAY     = 0.82   # per-frame (~175 ms half-life at 12 fps)
    _PEAK_HOLD = 1.2
    _PEAK_FALL = 0.88
    _GAMMA     = 0.45   # perceptual power curve: raw=0.5 → 0.76 visible

    def __init__(self, parent: "QSlider") -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._level  = 0.0
        self._peak   = 0.0
        self._peak_t = 0.0

    def set_level(self, raw: float) -> None:
        scaled = raw ** _VuOverlay._GAMMA if raw > 0.0 else 0.0
        new_level = max(scaled, self._level * _VuOverlay._DECAY)

        now = time.monotonic()
        if scaled >= self._peak:
            new_peak, new_peak_t = scaled, now
        elif now - self._peak_t > _VuOverlay._PEAK_HOLD:
            new_peak, new_peak_t = self._peak * _VuOverlay._PEAK_FALL, self._peak_t
        else:
            new_peak, new_peak_t = self._peak, self._peak_t

        # Only queue a repaint when the change is visually meaningful (≥ 0.5 % bar)
        if abs(new_level - self._level) > 0.005 or abs(new_peak - self._peak) > 0.005:
            self._level  = new_level
            self._peak   = new_peak
            self._peak_t = new_peak_t
            self.update()
        else:
            self._level  = new_level
            self._peak   = new_peak
            self._peak_t = new_peak_t

    def paintEvent(self, event) -> None:
        if self._level < 0.01 and self._peak < 0.01:
            return

        slider = self.parent()
        opt = QStyleOptionSlider()
        slider.initStyleOption(opt)
        groove = slider.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, slider,
        )
        if not groove.isValid() or groove.height() < 2:
            return

        gx, gy, gw, gh = groove.x(), groove.y(), groove.width(), groove.height()
        accent = slider.palette().color(QPalette.ColorRole.Highlight)

        p = QPainter(self)

        vu_h = int(gh * self._level)
        if vu_h > 0:
            c = QColor(accent.lighter(140))
            c.setAlpha(110)
            p.fillRect(gx, gy + gh - vu_h, gw, vu_h, c)

        if self._peak > 0.01:
            pk_y = gy + gh - int(gh * self._peak) - 1
            c = QColor(accent.lighter(180))
            c.setAlpha(220)
            p.fillRect(gx, max(gy, pk_y), gw, 2, c)

        p.end()


class _VuSlider(QSlider):
    """Vertical QSlider with a _VuOverlay child for non-invasive VU animation."""

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setRange(0, 100)
        self.setFixedHeight(180)
        self._overlay = _VuOverlay(self)
        self._overlay.setGeometry(self.rect())

    def initStyleOption(self, option) -> None:
        super().initStyleOption(option)
        option.state |= QStyle.StateFlag.State_Active

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())

    def set_level(self, raw: float) -> None:
        self._overlay.set_level(raw)


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

    def __init__(
        self,
        channel_index: int,
        config: ConfigManager,
        backend: PipeWireManager,
        is_midi: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ch     = channel_index
        self._config = config
        self._backend = backend
        self.is_midi_channel = is_midi
        logger.debug("Creating ChannelWidget: index=%d, is_midi=%s", channel_index, is_midi)

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(_CHANNEL_MIN_WIDTH)
        # Prevent the whole column from stretching infinitely if long text is loaded
        self.setMaximumWidth(85)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # ── Mute Button ────────────────────────────────────────────────
        self._mute_btn = QToolButton()
        self._mute_btn.setIcon(QIcon.fromTheme("audio-volume-high"))
        self._mute_btn.setToolTip("Toggle mute.")
        self._mute_btn.clicked.connect(lambda: self._backend.toggle_mute(self._ch))

        # ── Level label ────────────────────────────────────────────────
        self._level_label = QLabel("—")
        self._level_label.setObjectName("pct_label")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        small = self._level_label.font()
        small.setPointSize(9)
        self._level_label.setFont(small)
        
        # Reduced opacity applied later during update_accent_colors

        # ── Slider (integrated VU fader) ───────────────────────────────
        self._slider = _VuSlider()
        init_vol = self._config.get_channel_volume(self._ch)
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

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        
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
        is_hw = (self._config.get_channel_mode(self._ch) == "hardware")
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
        layout.addWidget(sep)
        
        layout.addWidget(self._app_list_scroll)
        layout.addWidget(self._add_btn)
        layout.addLayout(self._toggles_layout)
        
        # ── MIDI UI Elements (Bottom) ──────────────────────────────────
        if self.is_midi_channel:
            self._learn_btn = QToolButton()
            self._learn_btn.setIcon(QIcon.fromTheme('media-record'))
            self._learn_btn.setText("Learn")
            self._learn_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._learn_btn.setCheckable(True)
            self._learn_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._learn_btn.setMinimumHeight(24)
            
            # Initial text: show current CC if assigned
            current_cc = self._config.get_midi_cc(self._ch)
            btn_text = f"CC: {current_cc}" if current_cc is not None else "Learn"
            self._learn_btn.setText(btn_text)
            
            self._learn_btn.setToolTip("Click to learn a MIDI CC mapping.")
            self._learn_btn.clicked.connect(self._on_learn_clicked)
            
            self._remove_midi_btn = QToolButton()
            self._remove_midi_btn.setIcon(QIcon.fromTheme('list-remove'))
            self._remove_midi_btn.setText("Delete")
            self._remove_midi_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._remove_midi_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._remove_midi_btn.setMinimumHeight(24)
            self._remove_midi_btn.setToolTip("Remove this MIDI channel.")
            self._remove_midi_btn.clicked.connect(self._on_remove_midi_clicked)
            
            midi_controls_layout = QVBoxLayout()
            midi_controls_layout.setContentsMargins(0, 4, 0, 0)
            midi_controls_layout.setSpacing(4)
            midi_controls_layout.addWidget(self._learn_btn)
            midi_controls_layout.addWidget(self._remove_midi_btn)
            layout.addLayout(midi_controls_layout)
            
        layout.addStretch()

        self.refresh_theme()
        self._refresh_app_list()
        
    def _on_learn_clicked(self, checked: bool) -> None:
        if checked:
            self._learn_btn.setText("Waiting...")
            # Visual feedback that we're listening
            pal = self._learn_btn.palette()
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("red"))
            self._learn_btn.setPalette(pal)
            logger.debug("Channel %d entering MIDI Learn mode", self._ch)
        else:
            current_cc = self._config.get_midi_cc(self._ch)
            btn_text = f"CC: {current_cc}" if current_cc is not None else "Learn"
            self._learn_btn.setText(btn_text)
            self._learn_btn.setPalette(QApplication.palette())

    def update_midi_cc(self, cc_number: int) -> None:
        """Update the button text to show the newly assigned CC and uncheck."""
        self._learn_btn.setChecked(False)
        self._learn_btn.setText(f"CC: {cc_number}")
        self._learn_btn.setPalette(QApplication.palette())
        logger.debug("Channel %d MIDI CC updated to %d", self._ch, cc_number)

    def is_waiting_for_midi(self) -> bool:
        """Return True if the Learn button is currently toggled on."""
        return self.is_midi_channel and self._learn_btn.isChecked()
            
    def _on_remove_midi_clicked(self) -> None:
        reply = QMessageBox.question(
            self, "Remove MIDI Channel",
            f"Are you sure you want to remove {self._ch_label.text()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.deleteLater()  # Destroy widget to ensure clean layout clearing
            self._config.remove_midi_channel(self._ch)
            # Rebuild is triggered via settings_changed in config_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_volume(self, volume: float) -> None:
        pct = int(volume * 100)
        self._slider.blockSignals(True)
        self._slider.setValue(pct)
        self._level_label.setText(f"{pct} %")
        self._slider.blockSignals(False)

    def set_peak(self, level: float) -> None:
        """Update the integrated VU wave with a new level [0.0–1.0]."""
        self._slider.set_level(level)

    @pyqtSlot(int, int)
    def handle_midi_input(self, cc: int, value: int) -> None:
        """Slot for direct connection from MidiThread.midi_cc_received."""
        mapped_cc = self._config.get_midi_cc(self._ch)
        if mapped_cc is not None and cc == mapped_cc:
            vol = value / 127.0
            self.set_volume(vol)
            # Notify config for persistence
            self._config.set_channel_volume(self._ch, vol)

    @pyqtSlot(int)
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
        for role in (QPalette.ColorRole.Highlight, QPalette.ColorRole.HighlightedText, QPalette.ColorRole.WindowText, QPalette.ColorRole.Button):
            palette.setColor(QPalette.ColorGroup.Inactive, role, palette.color(QPalette.ColorGroup.Active, role))
        
        # _VuSlider reads palette directly in paintEvent — just push the updated palette
        self._slider.setPalette(palette)
        self._slider.update()

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
                parts = hw_id.split(':', 1)
                display_name = parts[1] if len(parts) == 2 else hw_id
                self._app_list_layout.addWidget(
                    _AppRow(display_name, on_remove=self._remove_hw)
                )
        else:
            for name in self._config.get_app_names(self._ch):
                self._app_list_layout.addWidget(
                    _AppRow(name, on_remove=lambda _=False, n=name: self._remove_app(n))
                )
            
        # Hide V-Sink for special pseudo-apps (System Master / Other Apps),
        # hardware mode, or when running on Windows (no PipeWire null-sinks).
        from nativmix.utils.paths import is_windows
        _SPECIAL = ("system master", "other apps")
        app_names_lower = [n.lower() for n in self._config.get_app_names(self._ch)]
        has_special = any(n in _SPECIAL for n in app_names_lower)
        is_hw = self._config.get_channel_mode(self._ch) == "hardware"
        self._vsink_cb.setVisible(not has_special and not is_hw and not is_windows())

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
    def _on_mode_toggled(self, checked: bool) -> None:
        mode = "hardware" if checked else "app"
        self._config.set_channel_mode(self._ch, mode)
        
        # When switching, flush the old assignments to prevent background routing
        if mode == "hardware":
            self._config.set_app_names(self._ch, [])
            if self._config.is_v_sink_enabled(self._ch):
                self._vsink_cb.setChecked(False) # Disables the V-Sink
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

    def _open_picker(self) -> None:
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
                    action.triggered.connect(
                        lambda _=False, i=hw_id: self._on_hw_picked(i)
                    )
                    
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
                    action.triggered.connect(
                        lambda _=False, i=hw_id: self._on_hw_picked(i)
                    )
                    
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

        # Build list of candidate app names from active streams
        candidates: set[str] = set()
        for s in streams:
            name = s.app_name
            # Global filter: ignore internal pulse/speech-dispatcher streams
            if "speech-dispatcher" in name.lower() or "dummy" in name.lower():
                continue
            candidates.add(name)

        # Always offer the special pseudo-apps
        candidates.add("System Master")
        candidates.add("Other Apps")

        # Sort: Special apps first, then alphabetically
        def sort_key(name: str) -> tuple[int, str]:
            if name == "System Master": return (0, name)
            if name == "Other Apps":    return (1, name)
            return (2, name.lower())

        added_actions = 0
        for name in sorted(candidates, key=sort_key):
            # Exclusivity: skip if assigned to another channel
            if name in assigned_elsewhere:
                continue

            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name in already_here)
            if name in ("System Master", "Other Apps"):
                font = action.font()
                font.setBold(True)
                action.setFont(font)

            action.triggered.connect(
                lambda _=False, n=name: self._on_stream_picked(n)
            )
            added_actions += 1

        if added_actions == 0:
            a = menu.addAction("No available streams")
            a.setEnabled(False)

        menu.addSeparator()
        type_action = menu.addAction("✏  Enter app name…")
        type_action.triggered.connect(self._open_manual_app_input)

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _open_manual_app_input(self) -> None:
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
    def _on_invert_toggled(self, checked: bool) -> None:
        self._config.set_inverted(self._ch, checked)
        self._config.save()
        logger.debug("Channel %d inversion: %s", self._ch, checked)

    @pyqtSlot(bool)
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
                    widget._name_label.setToolTip(text)
                    self._slider.setToolTip(text)
                    break

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

    def __init__(self, config: ConfigManager, backend: PipeWireManager, arduino_thread: Optional[ArduinoThread] = None, midi_thread: Optional[MidiThread] = None, parent=None) -> None:
        super().__init__(parent)
        self._config  = config
        self._backend = backend
        self._arduino = arduino_thread
        self._midi    = midi_thread
        self._channels: list[ChannelWidget] = []
        self._last_mode = self._config.input_mode
        self.settings = QSettings('nativmix', 'GUI')
        


        # Guard: set True while a show() is in flight to suppress spurious hide.
        self._show_requested: bool = False

        from nativmix.metadata import __app_name__, __version__
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        # ── Window Flags ──
        # Tool is the correct type for accessory windows on all compositors
        # (KDE Wayland, COSMIC, X11).  Window|SkipTaskbarHint breaks mapping
        # on some Wayland compositors without a valid activation token.
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint
        )

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
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Collapsible Settings Area & Pin ────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        self._toggle_settings_btn = QToolButton()
        self._toggle_settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle_settings_btn.setText("Show Settings")
        self._toggle_settings_btn.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle_settings_btn.setCheckable(True)
        self._toggle_settings_btn.setChecked(False)
        self._toggle_settings_btn.toggled.connect(self._on_settings_toggled)

        top_bar.addWidget(self._toggle_settings_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        top_bar.addStretch()

        self._pin_btn = QToolButton()
        self._pin_btn.setIcon(QIcon.fromTheme('window-pin'))
        self._pin_btn.setText("Don't Close")
        self._pin_btn.setToolTip("Keep the window open instead of hiding to tray on close.")
        self._pin_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(self._config.stay_open)
        self._pin_btn.toggled.connect(self._on_pin_toggled)

        top_bar.addWidget(self._pin_btn, alignment=Qt.AlignmentFlag.AlignRight)

        root.addLayout(top_bar)

        self.settings_panel = SettingsPanel(self._config)
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
        self._ch_layout = QHBoxLayout(container)
        self._ch_layout.setContentsMargins(0, 0, 0, 0)
        self._ch_layout.setSpacing(6)
        self._ch_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        scroll.setWidget(container)
        root.addWidget(scroll)

        # ── Add MIDI Channel Button ──
        self._add_midi_btn = QPushButton("+ Add MIDI Channel")
        self._add_midi_btn.clicked.connect(self._on_add_midi_clicked)
        # Visible only in hybrid/midi_only modes. Set visibility initially:
        self._add_midi_btn.setVisible(self._config.input_mode in ("hybrid", "midi_only"))

        # ── Size Grip (for frameless resizing) ─────────────────────────
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self._add_midi_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addStretch()
        grip = QSizeGrip(self)
        bottom_layout.addWidget(grip, alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        root.addLayout(bottom_layout)

        # ── Build initial channels ─────────────────────────────────────
        self._rebuild_channels()
        self.refresh_layout()
        self._update_window_width()

        # ── Restore geometry ───────────────────────────────────────────
        geom = self.settings.value('geometry')
        self._has_saved_geometry = bool(geom)
        if geom:
            self.restoreGeometry(geom)
            # Guard: if the restored position is off every screen (e.g. after a
            # resolution change or panel resize) move the window to the primary
            # screen's available area so it stays visible.
            win_rect = self.frameGeometry()
            on_screen = any(
                s.availableGeometry().intersects(win_rect)
                for s in QApplication.screens()
            )
            if not on_screen:
                logger.debug("Restored geometry is off-screen – resetting to primary screen")
                self.settings.remove('geometry')
                primary = QApplication.primaryScreen()
                if primary:
                    ag = primary.availableGeometry()
                    self.move(ag.x(), ag.y())

        # ── Signal connections ─────────────────────────────────────────
        self._config.mapping_changed.connect(self._on_mapping_changed)
        self._config.settings_changed.connect(self._apply_transparency)
        self._config.settings_changed.connect(self._on_settings_updated)
        self._backend.other_apps_changed.connect(self._on_other_apps_changed)

        # Qt emits paletteChanged when the system theme switches – no CSS needed
        QApplication.instance().paletteChanged.connect(self._on_palette_changed)
        
        self.settings_panel.panic_triggered.connect(self._on_panic_triggered)
        self.settings_panel.master_refresh_requested.connect(self._on_master_refresh)
        self.settings_panel.master_output_changed.connect(self._on_master_changed)
        if self._midi:
            self.settings_panel.midi_device_changed.connect(self._midi.set_device)
            self.settings_panel.midi_panic_triggered.connect(self._midi.trigger_panic)
        # ── Initial Population ──
        self._on_master_refresh()

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def _rebuild_channels(self) -> None:
        # Layout Batching: Disable layout updates during population
        self._ch_layout.setEnabled(False)
        try:
            while self._ch_layout.count():
                item = self._ch_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._channels.clear()

            for ch_dict in self._config.all_channels():
                i = ch_dict["index"]
                is_midi = ch_dict.get("is_midi", False)
                w = ChannelWidget(i, self._config, self._backend, is_midi=is_midi)
                self._channels.append(w)
                # Ensure MIDI-relevant signals are connected even after rebuild
                if w.is_midi_channel and self._midi:
                    self._midi.midi_cc_received.connect(w.handle_midi_input)
                
                self._ch_layout.addWidget(w)
                
            self._ch_layout.addStretch()
        finally:
            self._ch_layout.setEnabled(True)
            self._ch_layout.update()

    def finalize_ui(self) -> None:
        """Called once hardware/audio audit is complete to enable rendering."""
        if not self.updatesEnabled():
            logger.debug("MainWindow: Hardware audit complete. Enabling UI updates.")
            self.setUpdatesEnabled(True)
            self.update()

    def _update_window_width(self) -> None:
        pass  # Width is now dynamically handled by layouts and user resizing

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    @_slot_guard
    def on_volumes_changed(self, volumes: list[float]) -> None:
        for i, vol in enumerate(volumes):
            # Update persistent in-memory state
            self._config.set_channel_volume(i, vol)
            
            if i < len(self._channels):
                widget = self._channels[i]
                # Only update if visible or if it's a MIDI channel (which are handled separately usually, but for safety)
                if widget.isVisible():
                    widget.set_volume(vol)

    @pyqtSlot(int, float)
    @_slot_guard
    def on_channel_volume_changed(self, channel_index: int, volume: float) -> None:
        if 0 <= channel_index < len(self._channels):
            self._channels[channel_index].set_volume(volume)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_midi_connection_changed(self, connected: bool) -> None:
        """Reset Learn mode for all channels if connection is lost."""
        if not connected:
            logger.debug("MainWindow: MIDI connection lost, resetting Learn state.")
            for widget in self._channels:
                if widget.is_midi_channel and widget.is_waiting_for_midi():
                    widget._learn_btn.setChecked(False)
                    widget._on_learn_clicked(False)

    @pyqtSlot(int, int)
    @_slot_guard
    def on_midi_cc_received(self, control_number: int, value: int) -> None:
        """
        Slot: handles incoming MIDI CC messages for the Learn handshake.
        If a channel is in 'Learn' mode, it adopts this control_number.
        """
        for i, widget in enumerate(self._channels):
            if widget.is_waiting_for_midi() and widget.isVisible():
                # Adopt the CC
                self._config.set_midi_cc(widget._ch, control_number)
                widget.update_midi_cc(control_number)
                # Success - break so one CC doesn't assign to multiple channels
                logger.debug("MIDI Learn successful: CC %d assigned to channel %d", control_number, widget._ch)
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
        self._update_window_width()

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
        if 0 <= channel_index < len(self._channels):
            self._channels[channel_index].set_mute_state(is_muted)

    @pyqtSlot(list)
    @_slot_guard
    def on_peaks_updated(self, levels: list) -> None:
        for i, w in enumerate(self._channels):
            if i < len(levels):
                w.set_peak(levels[i])

    def _open_settings(self) -> None:
        """Open the settings panel (called from tray icon)."""
        self._toggle_settings_btn.setChecked(True)

    @pyqtSlot(bool)
    @_slot_guard
    def _on_settings_toggled(self, checked: bool) -> None:
        self.settings_panel.setVisible(checked)
        self._toggle_settings_btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self._toggle_settings_btn.setText("Hide Settings" if checked else "Show Settings")

    @pyqtSlot(bool)
    @_slot_guard
    def _on_pin_toggled(self, checked: bool) -> None:
        self._config.stay_open = checked
        self._config.save()
        logger.debug("Stay Open (Pin) toggled: %s", checked)

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
            
        self.update()

    @pyqtSlot()
    @_slot_guard
    def _on_settings_updated(self) -> None:
        # 1. Rebuild channels if mode or count changed
        mode_changed = (self._last_mode != self._config.input_mode)
        count_changed = (len(self._channels) != self._config.num_channels)
        
        if mode_changed or count_changed:
            logger.debug("Mode or count changed (%s -> %s) – rebuilding GUI", 
                        self._last_mode, self._config.input_mode)
            self._last_mode = self._config.input_mode
            self._rebuild_channels()
            self._update_window_width()
            
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
                    logger.debug("Purging MIDI widget: index=%d", widget._ch)
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
        if hasattr(self, '_add_midi_btn'):
            self._add_midi_btn.setVisible(mode in ("hybrid", "midi_only"))
            
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
        
        # Avoid global adjustSize on every refresh if possible to prevent jumping
        # Only call it on mode switch or channel count change
        # self.adjustSize() # Removed for stabilization

    def sync_ui_to_hardware(self) -> None:
        """
        Pull latest volumes from Arduino and MIDI threads and push to Backend + UI.
        Crucial for startup and mode transitions to prevent jumps.
        """
        logger.debug("Universal Volume Sync triggered")
        mode = self._config.input_mode
        
        # 1. Arduino Sync
        # Only if we are in a mode that uses hardware
        if mode in ("usb", "hybrid") and self._arduino:
            try:
                hw_vols = self._arduino.get_last_volumes()
                logger.debug("Syncing Arduino volumes: %s", hw_vols)
                self.on_volumes_changed(hw_vols)
                # Only push to backend when real hardware data is available.
                # get_last_volumes() returns 1.0 as a fallback before the first
                # serial reading arrives.  Pushing 1.0 to the backend would
                # immediately set an existing V-Sink to full volume, causing an
                # audible spike on restart.
                if self._arduino.has_real_data:
                    self._backend.apply_poti_volumes(hw_vols)
                else:
                    logger.debug("Arduino sync: no real data yet – skipping backend update")
            except Exception as exc:
                logger.error("Arduino sync failed: %s", exc)

        # 2. MIDI Sync
        if mode in ("hybrid", "midi_only") and self._midi:
            try:
                mapped = self._midi.get_mapped_volumes()
                if mapped:
                    logger.debug("Syncing MIDI volumes: %s", mapped)
                    self._backend.apply_midi_volumes(mapped)
                    self.on_midi_volumes_changed(mapped)
            except Exception as exc:
                logger.error("MIDI sync failed: %s", exc)

    @pyqtSlot()
    def _on_add_midi_clicked(self) -> None:
        self._config.add_midi_channel()
        # The add_midi_channel method emits settings_changed, which triggers _on_settings_updated,
        # which detects the length difference and rebuilds.

    def _apply_transparency(self) -> None:
        """
        Applies a semi-transparent background to the main window.
        """
        transparent = bool(self._config.transparency)
        # WA_TranslucentBackground stays always-on (set at init); only alpha changes.

        sys_color = self.palette().color(QPalette.ColorRole.Window)
        if transparent:
            alpha = 200  # Transparency (semi-transparent, but readable)
        else:
            alpha = 255  # Solid (Standard System-Theme)

        rgba_string = f"rgba({sys_color.red()}, {sys_color.green()}, {sys_color.blue()}, {alpha})"
        self.setStyleSheet(f"#MainFrame {{ background-color: {rgba_string}; border-radius: 12px; }}")
        
        self.update()
        


    def _apply_dynamic_app_styles(self) -> None:
        """
        Generates and applies a comprehensive dynamic stylesheet to the MainWindow.
        Overrides stubborn system defaults for QComboBox and standard hover states.
        """
        palette = QApplication.palette()
        
        # Theme colors
        accent = palette.color(QPalette.ColorRole.Highlight).name()
        accent_text = palette.color(QPalette.ColorRole.HighlightedText).name()
        window_bg = palette.color(QPalette.ColorRole.Window).name()
        window_text = palette.color(QPalette.ColorRole.WindowText).name()
        base_bg = palette.color(QPalette.ColorRole.Base).name()
        
        # Calculate a border color (semi-transparent version of text color)
        text_color = palette.color(QPalette.ColorRole.WindowText)
        border_color = f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 0.2)"

        # 1. Global App Stylesheet (Inherited by children)
        global_qss = f"""
        /* QComboBox Main Controls */
        QComboBox {{
            background-color: {base_bg};
            border: 1px solid {border_color};
            border-radius: 4px;
            padding: 2px 8px;
            color: {window_text};
        }}
        QComboBox:hover {{
            border: 1px solid {accent};
        }}
        QComboBox::drop-down {{
            border: none;
            background: transparent;
            width: 20px;
        }}
        QComboBox::down-arrow {{
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 5px solid {window_text};
            width: 0;
            height: 0;
            margin-right: 8px;
        }}

        /* QComboBox Dropdown List */
        QComboBox QAbstractItemView {{
            background-color: {window_bg};
            border: 1px solid {border_color};
            selection-background-color: {accent};
            outline: none;
        }}
        QComboBox QAbstractItemView::item {{
            padding: 4px 8px;
            min-height: 24px;
        }}
        QComboBox QAbstractItemView::item:selected,
        QComboBox QAbstractItemView::item:hover {{
            background-color: {accent};
            color: {accent_text};
        }}

        /* Global Hover Effects for Standard Buttons */
        QPushButton:hover, QToolButton:hover {{
            background-color: {accent};
            color: {accent_text};
            border-radius: 4px;
        }}
        """
        self.setStyleSheet(global_qss)

        # 2. Specific styles for top-bar buttons (inherits from global above)
        settings_btn_style = """
        QPushButton, QToolButton {
            border: none;
            font-weight: bold;
            padding: 4px;
        }
        """
        self._toggle_settings_btn.setStyleSheet(settings_btn_style)
        self._pin_btn.setStyleSheet(settings_btn_style)

    @pyqtSlot(list)
    def _on_other_apps_changed(self, names: list[str]) -> None:
        """Dynamically updates the tooltip for the 'Other Apps' channel."""
        for ch_widget in self._channels:
            ch_widget.set_other_apps_tooltip(names)

    @pyqtSlot()
    def _on_panic_triggered(self) -> None:
        """Reset all apps to default sink, destroy V-Sinks, clear mappings."""
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "Panic Reset",
            "This will destroy all virtual cables and move all apps back to the system default output.\n\nAre you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
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
            self._on_debug_refresh()
            logger.debug("Panic Reset completed from GUI.")



    @pyqtSlot()
    def _on_master_refresh(self) -> None:
        """Fetch real sinks and update the settings panel dropdown."""
        sinks = self._backend.get_real_sinks()
        default = self._backend.get_default_sink_name()
        self.settings_panel.populate_master_outputs(sinks, default)

    @pyqtSlot(str)
    def _on_master_changed(self, sink_name: str) -> None:
        """Set the new default sink and route loopbacks."""
        self._backend.set_default_sink_and_move_loopbacks(sink_name)

    def refresh_stream_list(self) -> None:
        """No-op: stream picker fetches data on-demand."""

    # ------------------------------------------------------------------
    # Close → conditionally hide to tray or actually close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.settings.setValue('geometry', self.saveGeometry())
        
        # If the Tray Icon called "Quit NativMix", we must accept the event 
        # so QApplication.quit() can actually terminate the application.
        if getattr(self, "_force_quit", False):
            logger.debug("MainWindow force-closing, stopping background threads")
            if self._arduino:
                self._arduino.stop()
            if self._midi:
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
        if event.button() == Qt.MouseButton.LeftButton:
            if self.windowHandle():
                self.windowHandle().startSystemMove()
        super().mousePressEvent(event)

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
            g.x(), g.y(), g.width(), g.height(),
            self.isActiveWindow(),
            getattr(self, "_show_requested", False),
        )
        super().showEvent(event)
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
        """Helper to safely save the window geometry to QSettings."""
        if self.isVisible():
            self.settings.setValue('geometry', self.saveGeometry())
            logger.debug("Window geometry saved")
