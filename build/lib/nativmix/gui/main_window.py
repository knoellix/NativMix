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
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QSize, pyqtSlot, QEvent, QSettings
from PyQt6.QtGui import QIcon, QPixmap, QPalette, QColor, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QSizeGrip,
)

from nativmix.gui.settings_panel import SettingsPanel
from nativmix.gui.theme_parser import parse_kde_scheme

if TYPE_CHECKING:
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.audio.manager import PipeWireManager

logger = logging.getLogger(__name__)

_CHANNEL_MIN_WIDTH = 60


# ---------------------------------------------------------------------------
# Single mapped-app row (remove button + name)
# ---------------------------------------------------------------------------

class _AppRow(QWidget):
    """[×] [name]  – one per assigned app inside a channel."""

    def __init__(self, app_name: str, on_remove, parent=None) -> None:
        super().__init__(parent)
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
        self._name_label.setStyleSheet(f"QLabel {{ color: {accent_hex}; }}")


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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ch     = channel_index
        self._config = config
        self._backend = backend

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(_CHANNEL_MIN_WIDTH)
        self.setMaximumWidth(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

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

        # ── Slider ─────────────────────────────────────────────────────
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)
        self._slider.setValue(50)
        self._slider.setMinimumHeight(140)
        self._slider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._slider.valueChanged.connect(self._on_slider_changed)

        self._ch_label = QLabel(f"CH {channel_index + 1}")
        self._ch_label.setObjectName("ch_label")
        self._ch_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
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
        self._app_list_layout = QVBoxLayout(self._app_list_widget)
        self._app_list_layout.setContentsMargins(0, 0, 0, 0)
        self._app_list_layout.setSpacing(2)

        self._app_list_scroll = QScrollArea()
        self._app_list_scroll.setWidgetResizable(True)
        self._app_list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._app_list_scroll.viewport().setAutoFillBackground(False)
        self._app_list_scroll.setStyleSheet("background: transparent;")
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
        self._invert_cb.toggled.connect(self._on_invert_toggled)

        # V-Sink checkbox
        self._vsink_cb = QCheckBox("V-Sink")
        self._vsink_cb.setToolTip("Route audio through a virtual sink.")
        self._vsink_cb.setChecked(self._config.is_v_sink_enabled(channel_index))
        self._vsink_cb.toggled.connect(self._on_vsink_toggled)

        self._toggles_layout.addWidget(self._vsink_cb)
        self._toggles_layout.addWidget(self._invert_cb)

        # Initialize Mode UI State
        is_hw = (self._config.get_channel_mode(self._ch) == "hardware")
        self._mode_cb.setChecked(is_hw)
        self._apply_mode_ui(is_hw)
        
        # ── Root layout ────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)
        layout.addWidget(self._mute_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._level_label)
        layout.addWidget(self._slider, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._ch_label)
        layout.addWidget(sep)
        layout.addWidget(self._mode_cb)
        layout.addWidget(self._app_list_scroll)
        layout.addWidget(self._add_btn)
        layout.addLayout(self._toggles_layout)
        layout.addStretch()

        self.refresh_theme()
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_volume(self, volume: float) -> None:
        pct = int(volume * 100)
        self._slider.blockSignals(True)
        self._slider.setValue(pct)
        self._level_label.setText(f"{pct} %")
        self._slider.blockSignals(False)

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
        accent_hex = palette.color(QPalette.ColorRole.Highlight).name()
        # Use Button instead of Dark because Dark is not parsed by our KDE theme parser, 
        # causing it to stay stuck on the previous theme's color!
        bg_hex = palette.color(QPalette.ColorRole.Button).name()
        text_color = palette.color(QPalette.ColorRole.WindowText)
        border_hex = f"rgba({text_color.red()}, {text_color.green()}, {text_color.blue()}, 50)"
        text_on_accent = palette.color(QPalette.ColorRole.HighlightedText).name()
        
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
        
        label_qss = f"QLabel {{ color: {accent_hex}; }}"
        self._ch_label.setStyleSheet(label_qss)
        self._level_label.setStyleSheet(label_qss)
        
        # 3. ToolButtons (Mute, Add) Hover States
        btn_qss = f"""
        QToolButton, QPushButton {{
            border: none;
            border-radius: 4px;
        }}
        QToolButton:hover, QPushButton:hover, QToolButton:checked {{
            background-color: {accent_hex};
            color: {text_on_accent};
        }}
        """
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
            self._vsink_cb.setVisible(False)
        else:
            self._add_btn.setText("+ App")
            self._add_btn.setToolTip("Assign audio stream.")
            self._vsink_cb.setVisible(True)

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

        menu.exec(self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft()))

    def _on_stream_picked(self, app_name: str) -> None:
        current = self._config.get_app_names(self._ch)
        if app_name in current:
            self._config.remove_app_name(self._ch, app_name)
        else:
            self._config.update_mapping(app_name, self._ch)
        self._config.save()
        self._refresh_app_list()

    # ------------------------------------------------------------------
    # Inversion
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    def _on_invert_toggled(self, checked: bool) -> None:
        self._config.set_inverted(self._ch, checked)
        self._config.save()
        logger.info("Channel %d inversion: %s", self._ch, checked)

    @pyqtSlot(bool)
    def _on_vsink_toggled(self, checked: bool) -> None:
        self._config.set_v_sink_enabled(self._ch, checked)
        self._config.save()
        logger.info("Channel %d V-Sink enabled: %s", self._ch, checked)
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

    def __init__(self, config: ConfigManager, backend: PipeWireManager, parent=None) -> None:
        super().__init__(parent)
        self._config  = config
        self._backend = backend
        self._channels: list[ChannelWidget] = []
        self.settings = QSettings('NativMix', 'GUI')
        self.is_pinned = False
        
        # Save the System Default Palette for when "System (Default)" is re-selected
        self.default_palette = QApplication.instance().palette()

        self.setWindowTitle("NativMix")
        # ── Window Flags (KDE Applet Style) ──
        # Basis-Flags für permanentes Frameless- und Tool-Verhalten
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint
        )

        from nativmix.utils.paths import get_icon_path
        icon_path = get_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        else:
            self.setWindowIcon(QIcon.fromTheme("nativmix", QIcon.fromTheme("audio-volume-high")))
            
        self.setMinimumHeight(380)
        self.setMinimumWidth(350)
        self.resize(400, 400)
        
        # Must be set permanently before show() for Wayland blur
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # ── Central widget ─────────────────────────────────────────────
        central = QFrame()
        central.setObjectName("MainFrame")
        self.setCentralWidget(central)
        
        self._apply_glass_look()
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
        self._pin_btn.setToolTip("Verhindert das automatische Ausblenden der App. Die App läuft im Hintergrund weiter, um das Audio-Routing aufrechtzuerhalten.")
        self._pin_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(False)
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        
        top_bar.addWidget(self._pin_btn, alignment=Qt.AlignmentFlag.AlignRight)

        root.addLayout(top_bar)

        self.settings_panel = SettingsPanel(config)
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

        # ── Size Grip (for frameless resizing) ─────────────────────────
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addStretch()
        grip = QSizeGrip(self)
        bottom_layout.addWidget(grip, alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        root.addLayout(bottom_layout)

        # ── Build initial channels ─────────────────────────────────────
        self._rebuild_channels()
        self._update_window_width()

        # ── Restore geometry ───────────────────────────────────────────
        geom = self.settings.value('geometry')
        if geom:
            self.restoreGeometry(geom)

        # ── Signal connections ─────────────────────────────────────────
        self._config.mapping_changed.connect(self._on_mapping_changed)
        self._config.settings_changed.connect(self._apply_glass_look)

        # Qt emits paletteChanged when the system theme switches – no CSS needed
        QApplication.instance().paletteChanged.connect(self._on_palette_changed)
        
        # ── Settings UI signals ─────────────────────────────────────────
        self.settings_panel.panic_triggered.connect(self._on_panic_triggered)
        self.settings_panel.debug_refresh_requested.connect(self._on_debug_refresh)
        self.settings_panel.master_refresh_requested.connect(self._on_master_refresh)
        self.settings_panel.master_output_changed.connect(self._on_master_changed)
        self.settings_panel.theme_changed.connect(self._on_theme_changed)

        # ── Initial Population ──
        # Load the saved active theme immediately on startup
        active_theme = self._config.active_theme
        if active_theme and active_theme != "System (Default)":
            self._on_theme_changed(active_theme)
            
        self._on_master_refresh()

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def _rebuild_channels(self) -> None:
        while self._ch_layout.count():
            item = self._ch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._channels.clear()

        for i in range(self._config.num_channels):
            w = ChannelWidget(i, self._config, self._backend)
            self._channels.append(w)
            self._ch_layout.addWidget(w)
            
        self._ch_layout.addStretch()

    def _update_window_width(self) -> None:
        pass  # Width is now dynamically handled by layouts and user resizing

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    def on_volumes_changed(self, volumes: list[float]) -> None:
        for i, vol in enumerate(volumes):
            if i < len(self._channels):
                self._channels[i].set_volume(vol)

    @pyqtSlot(int, float)
    def on_channel_volume_changed(self, channel_index: int, volume: float) -> None:
        if 0 <= channel_index < len(self._channels):
            self._channels[channel_index].set_volume(volume)

    @pyqtSlot(int)
    def on_channel_count_changed(self, n: int) -> None:
        if n == len(self._channels):
            return
        logger.info("Channel count changed to %d – rebuilding GUI", n)
        self._config.num_channels = n
        self._config.save()
        self._rebuild_channels()
        self._update_window_width()

    @pyqtSlot(int, list)
    def _on_mapping_changed(self, channel_index: int, _names: list[str]) -> None:
        """
        Refresh ALL channels when a mapping changes, so the + App menus
        immediately reflect the new exclusivity rules.
        """
        for ch in self._channels:
            ch.refresh()

    @pyqtSlot(int, bool)
    def on_mute_state_changed(self, channel_index: int, is_muted: bool) -> None:
        if 0 <= channel_index < len(self._channels):
            self._channels[channel_index].set_mute_state(is_muted)

    @pyqtSlot(bool)
    def _on_settings_toggled(self, checked: bool) -> None:
        self.settings_panel.setVisible(checked)
        self._toggle_settings_btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self._toggle_settings_btn.setText("Hide Settings" if checked else "Show Settings")

    @pyqtSlot(bool)
    def _on_pin_toggled(self, checked: bool) -> None:
        self.is_pinned = checked

    def _on_palette_changed(self, _palette=None) -> None:
        """
        Called by Qt when the system theme changes (dark ↔ light or accent changes).
        Re-apply the glass look and our dynamic styling hooks.
        """
        logger.debug("System palette changed – repainting and syncing theme")
        
        # 1. Update window background (glass look)
        self._apply_glass_look()
        
        # 2. Update top bar buttons (Settings, Pin)
        self._update_top_bar_styles()
        
        # 3. Cascade redraws to all channels (Labels, etc.)
        for ch in self._channels:
            ch.refresh_theme()
            
        self.repaint()
        
    def _apply_glass_look(self) -> None:
        """Apply transparent or solid background based on native system theme."""
        sys_color = self.palette().color(QPalette.ColorRole.Window)
        
        if self._config.glass_look:
            alpha = 200  # Glass-Look (leicht durchsichtig, aber lesbar)
        else:
            alpha = 255  # Solid (Standard System-Theme)

        rgba_string = f"rgba({sys_color.red()}, {sys_color.green()}, {sys_color.blue()}, {alpha})"
        self.setStyleSheet(f"#MainFrame {{ background-color: {rgba_string}; border-radius: 12px; }}")
        
        # Force a repaint to safely apply KWin compositor changes on-the-fly
        self.repaint()
        
    @pyqtSlot(str)
    def _on_theme_changed(self, path: str) -> None:
        """Called when a new KDE .colors scheme is selected."""
        app = QApplication.instance()
        palette = QPalette(self.default_palette)
        
        if path == "System (Default)":
            logger.info("Restoring System Default Palette.")
        else:
            custom_palette = parse_kde_scheme(path)
            if custom_palette:
                # Definitively overwrite core roles in the new palette base
                # We apply to ALL groups (Active, Inactive, Normal) by default
                for role in (QPalette.ColorRole.Window, QPalette.ColorRole.WindowText, 
                             QPalette.ColorRole.Base, QPalette.ColorRole.Text,
                             QPalette.ColorRole.Button, QPalette.ColorRole.ButtonText,
                             QPalette.ColorRole.Highlight, QPalette.ColorRole.HighlightedText,
                             QPalette.ColorRole.Link, QPalette.ColorRole.LinkVisited,
                             QPalette.ColorRole.ToolTipBase, QPalette.ColorRole.ToolTipText):
                    
                    # Read the color from the parsed active group (which is the only one we fill in theme_parser)
                    c = custom_palette.color(QPalette.ColorGroup.Active, role)
                    if c.isValid() and c != QColor("black"):
                        # Set it for ALL groups in our new working palette
                        palette.setColor(role, c)
                
                logger.info(f"Applied custom theme palette from {path}")
            else:
                logger.warning(f"Failed to apply theme palette from {path} - parsing failed.")
                
        # Force the Inactive context to match the Active context for critical elements 
        # so they stay vibrant when NativMix loses focus or runs in background.
        for role in (QPalette.ColorRole.Highlight, QPalette.ColorRole.WindowText, 
                     QPalette.ColorRole.Button, QPalette.ColorRole.Window,
                     QPalette.ColorRole.ButtonText, QPalette.ColorRole.Text,
                     QPalette.ColorRole.HighlightedText, QPalette.ColorRole.Base,
                     QPalette.ColorRole.Link, QPalette.ColorRole.LinkVisited):
            palette.setColor(QPalette.ColorGroup.Inactive, role, palette.color(QPalette.ColorGroup.Active, role))
            
        app.setPalette(palette)
        
        # ── Kvantum/Breeze "Style Jog" ──
        # Native theme engines often ignore manual setPalette() calls because they
        # hook directly into the system configuration. We force them to re-read
        # our local palette by "unpolishing" and "re-polishing" the application.
        style = app.style()
        style.unpolish(app)
        style.polish(app)
        
        self._update_top_bar_styles()
        
        # Also force a full refresh of the window itself
        style.unpolish(self)
        style.polish(self)
                
        # Cascade theme redraws
        for ch in self._channels:
            ch.refresh_theme()
                
        # the app palette changes, we need to let the glass look re-adjust the transparency
        self._apply_glass_look()

    def _update_top_bar_styles(self) -> None:
        """Generate dynamic top-bar hover state QSS using the current system Highlight color."""
        palette = QApplication.palette()
        accent = palette.color(QPalette.ColorRole.Highlight).name()
        text_on_accent = palette.color(QPalette.ColorRole.HighlightedText).name()
        
        settings_style = f"""
        QPushButton, QToolButton {{
            border: none;
            font-weight: bold;
            padding: 4px;
            border-radius: 4px;
        }}
        QPushButton:hover, QToolButton:hover, QToolButton:checked {{
            background-color: {accent};
            color: {text_on_accent};
        }}
        """
        self._toggle_settings_btn.setStyleSheet(settings_style)
        self._pin_btn.setStyleSheet(settings_style)

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
            logger.info("Panic Reset completed from GUI.")

    @pyqtSlot()
    def _on_debug_refresh(self) -> None:
        """Fetch live PipeWire data and display it in the debug accordion."""
        unmapped = self._backend.get_unmapped_streams()
        v_sinks = self._backend.get_active_virtual_sinks()
        
        lines = []
        lines.append("=== NativMix Virtual Sinks ===")
        if v_sinks:
            lines.extend("  - " + s for s in v_sinks)
        else:
            lines.append("  (None active)")
            
        lines.append("\n=== Unmapped Streams (Other Apps) ===")
        if unmapped:
            lines.extend("  - " + s for s in unmapped)
        else:
            lines.append("  (None found)")
            
        self.settings_panel.set_debug_text("\n".join(lines))

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
    # Close → hide (tray keeps the app alive)
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.settings.setValue('geometry', self.saveGeometry())
        event.ignore()
        self.hide()

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
            if not self.isActiveWindow():
                if not self.is_pinned:
                    self.settings.setValue('geometry', self.saveGeometry())
                    self.hide()
        super().changeEvent(event)
