"""
Settings panel for NativMix – shown above the channel sliders.

Provides:
- USB port selector (QComboBox) – only shows ports with real hardware
  (hwid / description not empty). Marks the currently connected port.
- Autostart toggle (QPushButton, checkable) – copies/removes .desktop file
  in ~/.config/autostart/ per Rule 5 (never uses sudo, no /etc paths)

Design philosophy: ZERO manual colors. 100% native Qt style.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import serial.tools.list_ports
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QSizePolicy,
    QVBoxLayout,
    QTextEdit,
)

from nativmix.gui.theme_parser import discover_kde_schemes

logger = logging.getLogger(__name__)

_AUTOSTART_DIR  = Path.home() / ".config" / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "nativmix.desktop"


def _is_autostart_enabled() -> bool:
    return _AUTOSTART_FILE.exists()


def _enable_autostart() -> bool:
    try:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        exec_path = os.path.expanduser("~/.local/bin/nativmix")
        icon_path = os.path.expanduser("~/.local/share/nativmix/nativmix.png")
        content = f"""[Desktop Entry]
Type=Application
Name=NativMix
Exec={exec_path} --hidden
Icon={icon_path}
"""
        _AUTOSTART_FILE.write_text(content)
        logger.info("Autostart enabled: created %s", _AUTOSTART_FILE)
        return True
    except OSError as exc:
        logger.error("Autostart enable failed: %s", exc)
        return False


def _disable_autostart() -> bool:
    try:
        _AUTOSTART_FILE.unlink(missing_ok=True)
        logger.info("Autostart disabled")
        return True
    except OSError as exc:
        logger.error("Autostart disable failed: %s", exc)
        return False


def _real_ports() -> list[serial.tools.list_ports_common.ListPortInfo]:
    """Return only ports that appear to have actual hardware attached."""
    result = []
    for info in serial.tools.list_ports.comports():
        hwid = (info.hwid or "").strip()
        desc = (info.description or "").strip()
        # Exclude generic "n/a" placeholders that have no real device
        if hwid and hwid.upper() != "N/A":
            result.append(info)
        elif desc and desc.lower() not in ("n/a", ""):
            result.append(info)
    return result


class SettingsPanel(QGroupBox):
    """
    Toolbar-style group box with port selector and autostart toggle.

    Signals
    -------
    port_changed(str)
        Emitted when the user picks a different serial port.
        Empty string → auto-detect.
    panic_triggered()
        Emitted when the user requests a complete reset.
    debug_refresh_requested()
        Emitted when the user wants to refresh the debug data.
    """

    port_changed = pyqtSignal(str)
    panic_triggered = pyqtSignal()
    debug_refresh_requested = pyqtSignal()
    master_output_changed = pyqtSignal(str)
    master_refresh_requested = pyqtSignal()
    theme_changed = pyqtSignal(str)

    def __init__(self, config, connected_port: str | None = None, parent=None) -> None:
        super().__init__("Settings", parent)
        self._config = config
        self._connected_port: str | None = connected_port  # updated by main.py

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(6, 4, 6, 4)
        root_layout.setSpacing(6)
        
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(10)

        top_layout.addWidget(QLabel("USB Port:"))

        self._port_box = QComboBox()
        self._port_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._port_box.setToolTip("Select USB port.")
        self._populate_ports()
        top_layout.addWidget(self._port_box)

        refresh_btn = QPushButton("↺")
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip("Refresh ports.")
        refresh_btn.clicked.connect(self._populate_ports)
        top_layout.addWidget(refresh_btn)

        top_layout.addSpacing(16)

        self._autostart_btn = QPushButton(
            "Autostart: ON" if _is_autostart_enabled() else "Autostart: OFF"
        )
        self._autostart_btn.setCheckable(True)
        self._autostart_btn.setChecked(_is_autostart_enabled())
        self._autostart_btn.setToolTip("Toggle system autostart.")
        self._autostart_btn.toggled.connect(self._on_autostart_toggled)
        top_layout.addWidget(self._autostart_btn)
        
        root_layout.addLayout(top_layout)
        
        # ── Master Output ──
        mo_layout = QHBoxLayout()
        mo_layout.setContentsMargins(0, 0, 0, 0)
        mo_layout.setSpacing(10)
        mo_layout.addWidget(QLabel("Master Output:"))
        
        self._master_box = QComboBox()
        self._master_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._master_box.setToolTip("Select system default audio output.")
        self._master_box.activated.connect(self._on_master_selected)
        mo_layout.addWidget(self._master_box)
        
        mo_refresh_btn = QPushButton("↺")
        mo_refresh_btn.setFixedWidth(32)
        mo_refresh_btn.setToolTip("Refresh outputs.")
        mo_refresh_btn.clicked.connect(self.master_refresh_requested.emit)
        mo_layout.addWidget(mo_refresh_btn)
        
        root_layout.addLayout(mo_layout)
        
        # Bottom row (Toggles & Debug)
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        
        self._glass_cb = QCheckBox("Transparency")
        self._glass_cb.setToolTip("Enable translucent window background.")
        self._glass_cb.setChecked(self._config.glass_look)
        self._glass_cb.toggled.connect(self._on_glass_toggled)
        bottom_layout.addWidget(self._glass_cb)
        
        bottom_layout.addSpacing(16)
        
        bottom_layout.addWidget(QLabel("Theme:"))
        self._theme_box = QComboBox()
        self._theme_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._theme_box.setToolTip("Select KDE Color Scheme.")
        self._populate_themes()
        self._theme_box.activated.connect(self._on_theme_selected)
        bottom_layout.addWidget(self._theme_box)
        
        root_layout.addLayout(bottom_layout)
        
        # ── Panic Button ──
        self._panic_btn = QPushButton("🚨 Reset All Routing (Panic Button)")
        self._panic_btn.setStyleSheet("QPushButton { color: #ff4444; font-weight: bold; }")
        self._panic_btn.setToolTip("Evacuate all apps to default output, destroy V-Sinks, reset UI mapping.")
        self._panic_btn.clicked.connect(self.panic_triggered.emit)
        root_layout.addWidget(self._panic_btn)
        
        # ── Debug Info (Expandable) ──
        self._debug_box = QGroupBox("Debug Info (PipeWire state)")
        self._debug_box.setCheckable(True)
        self._debug_box.setChecked(False)  # Collapsed by default
        
        debug_layout = QVBoxLayout(self._debug_box)
        
        dbg_btn_layout = QHBoxLayout()
        self._debug_refresh_btn = QPushButton("Refresh Data")
        self._debug_refresh_btn.clicked.connect(self.debug_refresh_requested.emit)
        dbg_btn_layout.addWidget(self._debug_refresh_btn)
        dbg_btn_layout.addStretch()
        debug_layout.addLayout(dbg_btn_layout)
        
        self._debug_text = QTextEdit()
        self._debug_text.setReadOnly(True)
        self._debug_text.setMaximumHeight(150)
        self._debug_text.setStyleSheet("QTextEdit { font-family: monospace; font-size: 11px; }")
        self._debug_text.setPlaceholderText("Click Refresh to load live PipeWire data...")
        debug_layout.addWidget(self._debug_text)
        
        root_layout.addWidget(self._debug_box)

        self._port_box.currentIndexChanged.connect(self._on_port_selected)

    # ------------------------------------------------------------------
    # Public API for Main Window
    # ------------------------------------------------------------------
    
    def set_debug_text(self, text: str) -> None:
        """Update the debug accordion text."""
        self._debug_text.setPlainText(text)

    def populate_master_outputs(self, sinks: list[tuple[str, str]], current: str | None) -> None:
        """Populate the dropdown with (description, name) and set the current default."""
        self._master_box.blockSignals(True)
        self._master_box.clear()
        
        for desc, name in sinks:
            self._master_box.addItem(desc, userData=name)
            
        if current:
            idx = self._master_box.findData(current)
            if idx >= 0:
                self._master_box.setCurrentIndex(idx)
                
        self._master_box.blockSignals(False)

    def _populate_themes(self) -> None:
        """Discover and populate KDE themes."""
        self._theme_box.blockSignals(True)
        self._theme_box.clear()
        
        self._theme_box.addItem("System (Default)", userData="System (Default)")
        
        schemes = discover_kde_schemes()
        for name in sorted(schemes.keys()):
            path = schemes[name]
            self._theme_box.addItem(name, userData=path)
            
        current = self._config.active_theme
        idx = self._theme_box.findData(current)
        if idx >= 0:
            self._theme_box.setCurrentIndex(idx)
            
        self._theme_box.blockSignals(False)

    # ------------------------------------------------------------------

    def mark_connected_port(self, port: str | None) -> None:
        """Called from main.py when the Arduino connects to update the ★ marker."""
        self._connected_port = port
        self._populate_ports(restore=port or self._port_box.currentData())

    def _populate_ports(self, restore: str | None = None) -> None:
        """Rebuild the combo box from currently available real serial ports."""
        self._port_box.blockSignals(True)
        if restore is None:
            restore = self._port_box.currentData() or self._config.hardware_port

        self._port_box.clear()
        self._port_box.addItem("Auto-detect", userData=None)

        for info in _real_ports():
            connected = (info.device == self._connected_port)
            prefix = "★ " if connected else ""
            label = f"{prefix}{info.device}"
            if info.description and info.description.lower() not in ("n/a", ""):
                label += f"  ({info.description})"
            self._port_box.addItem(label, userData=info.device)

        if restore:
            idx = self._port_box.findData(restore)
            if idx >= 0:
                self._port_box.setCurrentIndex(idx)

        self._port_box.blockSignals(False)

    @pyqtSlot(int)
    def _on_port_selected(self, index: int) -> None:
        port = self._port_box.itemData(index)   # None = Auto
        self._config.hardware_port = port
        self._config.save()
        self.port_changed.emit(port or "")
        logger.info("Port selection changed: %s", port or "auto")

    @pyqtSlot(int)
    def _on_master_selected(self, index: int) -> None:
        name = self._master_box.itemData(index)
        if name:
            self.master_output_changed.emit(name)
            logger.info("Master output selected via GUI: %s", name)

    @pyqtSlot(bool)
    def _on_autostart_toggled(self, checked: bool) -> None:
        ok = _enable_autostart() if checked else _disable_autostart()
        actual = _is_autostart_enabled()
        self._autostart_btn.blockSignals(True)
        self._autostart_btn.setChecked(actual)
        self._autostart_btn.setText("Autostart: ON" if actual else "Autostart: OFF")
        self._autostart_btn.blockSignals(False)
        if not ok:
            logger.warning("Autostart toggle failed")

    @pyqtSlot(bool)
    def _on_glass_toggled(self, checked: bool) -> None:
        self._config.glass_look = checked
        self._config.save()
        logger.info("Glass-Look toggled: %s", checked)

    @pyqtSlot(int)
    def _on_theme_selected(self, index: int) -> None:
        path = self._theme_box.itemData(index)
        if path:
            self._config.set_active_theme(path)
            self._config.save()
            self.theme_changed.emit(path)
            logger.info("Theme selected: %s", path)
