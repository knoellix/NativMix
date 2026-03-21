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
import subprocess
from pathlib import Path

import serial.tools.list_ports
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
)


logger = logging.getLogger(__name__)

_AUTOSTART_DIR  = Path.home() / ".config" / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "nativmix.desktop"

# Windows registry key for autostart
_WIN_RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_APP_NAME = "NativMix"


def _is_autostart_enabled_windows() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY)
        winreg.QueryValueEx(key, _WIN_APP_NAME)
        winreg.CloseKey(key)
        return True
    except (ImportError, OSError):
        return False


def _enable_autostart_windows() -> bool:
    try:
        import shutil
        import winreg
        exe = shutil.which("nativmix") or os.path.abspath(__import__("sys").argv[0])
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, _WIN_APP_NAME, 0, winreg.REG_SZ, f'"{exe}" --hidden')
        winreg.CloseKey(key)
        logger.debug("Windows autostart enabled: %s", exe)
        return True
    except (ImportError, OSError) as exc:
        logger.error("Windows autostart enable failed: %s", exc)
        return False


def _disable_autostart_windows() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, _WIN_APP_NAME)
        winreg.CloseKey(key)
        logger.debug("Windows autostart disabled")
        return True
    except (ImportError, OSError) as exc:
        logger.error("Windows autostart disable failed: %s", exc)
        return False


def _is_autostart_enabled() -> bool:
    return _AUTOSTART_FILE.exists()


def _enable_autostart() -> bool:
    try:
        from nativmix.utils.paths import get_binary_dir
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        exec_path = get_binary_dir() / "nativmix"
        # Icon: prefer system-installed path, fall back to XDG data dir
        system_icon = Path("/usr/share/nativmix/assets/icon.png")
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        local_icon = xdg_data / "nativmix" / "icon.png"
        icon_path = system_icon if system_icon.exists() else local_icon
        content = f"""[Desktop Entry]
Type=Application
Name=NativMix
Exec={exec_path} --hidden
Icon={icon_path}
"""
        _AUTOSTART_FILE.write_text(content)
        logger.debug("Autostart enabled: created %s", _AUTOSTART_FILE)
        return True
    except OSError as exc:
        logger.error("Autostart enable failed: %s", exc)
        return False


def _disable_autostart() -> bool:
    try:
        _AUTOSTART_FILE.unlink(missing_ok=True)
        logger.debug("Autostart disabled")
        return True
    except OSError as exc:
        logger.error("Autostart disable failed: %s", exc)
        return False


def _systemd_unit_available() -> bool:
    """True wenn nativmix.service der systemd --user Instanz bekannt ist."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "cat", "nativmix.service"],
            capture_output=True, timeout=2,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _is_service_enabled() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", "nativmix.service"],
            capture_output=True, timeout=2,
        )
        return r.stdout.strip() == b"enabled"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _enable_service() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "enable", "nativmix.service"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _disable_service() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "disable", "nativmix.service"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
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
    midi_panic_triggered = pyqtSignal()
    master_output_changed = pyqtSignal(str)
    master_refresh_requested = pyqtSignal()
    midi_device_changed = pyqtSignal(str)


    def __init__(self, config, connected_port: str | None = None, parent=None) -> None:
        from nativmix.metadata import __version__
        super().__init__("Settings", parent)
        self._config = config
        self._connected_port: str | None = connected_port  # updated by main.py

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(5, 5, 5, 5)
        root_layout.setSpacing(4)
        
        # ── Input Mode & MIDI ──
        mode_layout = QHBoxLayout()
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(4)
        
        mode_layout.addWidget(QLabel("Input Mode:"))
        self._input_mode_box = QComboBox()
        self._input_mode_box.addItems(["USB Only (Default)", "USB + MIDI (Hybrid)", "MIDI Only"])
        self._input_mode_box.setToolTip("Select the active control inputs.")
        modes = ["usb", "hybrid", "midi_only"]
        current_mode = self._config.input_mode
        self._input_mode_box.setCurrentIndex(modes.index(current_mode) if current_mode in modes else 0)
        mode_layout.addWidget(self._input_mode_box)
        
        mode_layout.addSpacing(16)
        
        mode_layout.addWidget(QLabel("MIDI Hardware:"))
        self._midi_box = QComboBox()
        self._midi_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._midi_box.setToolTip("Select MIDI input device.")
        self._populate_midi_ports()
        mode_layout.addWidget(self._midi_box)
        
        midi_refresh_btn = QPushButton("↺")
        midi_refresh_btn.setFixedSize(26, 26)
        midi_refresh_btn.setToolTip("Refresh MIDI ports.")
        midi_refresh_btn.clicked.connect(self._populate_midi_ports)
        mode_layout.addWidget(midi_refresh_btn)
        
        self._midi_status_label = QLabel("MIDI: Offline")
        self._midi_status_label.setFixedWidth(120)
        self._midi_status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        small_font = self._midi_status_label.font()
        small_font.setPointSize(8)
        self._midi_status_label.setFont(small_font)
        mode_layout.addWidget(self._midi_status_label)
        
        root_layout.addLayout(mode_layout)
        
        # ── USB Port & Autostart ──
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)

        top_layout.addWidget(QLabel("USB Port:"))

        self._port_box = QComboBox()
        self._port_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._port_box.setToolTip("Select USB port.")
        self._populate_ports()
        top_layout.addWidget(self._port_box)

        refresh_btn = QPushButton("↺")
        refresh_btn.setFixedSize(26, 26)
        refresh_btn.setToolTip("Refresh USB ports.")
        refresh_btn.clicked.connect(self._populate_ports)
        top_layout.addWidget(refresh_btn)

        top_layout.addSpacing(16)

        from nativmix.utils.paths import is_windows
        self._use_windows_autostart: bool = is_windows()
        self._use_systemd: bool = False
        if self._use_windows_autostart:
            _autostart_on = _is_autostart_enabled_windows()
            _suffix = " (Registry)"
            _tip = "Autostart via Windows registry (HKCU\\...\\Run)."
        else:
            self._use_systemd = _systemd_unit_available()
            if self._use_systemd and _AUTOSTART_FILE.exists():
                # Migration: XDG-Datei vorhanden aber systemd verfügbar → einmalig migrieren
                if not _is_service_enabled():
                    _enable_service()
                    logger.info("Migrated autostart from XDG to systemd user service")
                _disable_autostart()  # .desktop Datei entfernen
            _autostart_on = _is_service_enabled() if self._use_systemd else _is_autostart_enabled()
            _suffix = " (systemd)" if self._use_systemd else ""
            _tip = "Autostart via systemd user service." if self._use_systemd else "Autostart via XDG (~/.config/autostart/)."
        self._autostart_btn = QPushButton(f"Autostart: {'ON' if _autostart_on else 'OFF'}{_suffix}")
        self._autostart_btn.setCheckable(True)
        self._autostart_btn.setChecked(_autostart_on)
        self._autostart_btn.setToolTip(_tip)
        self._autostart_btn.toggled.connect(self._on_autostart_toggled)
        top_layout.addWidget(self._autostart_btn)
        
        root_layout.addLayout(top_layout)
        
        try:
            # ── Master Output ──
            mo_layout = QHBoxLayout()
            mo_layout.setContentsMargins(0, 0, 0, 0)
            mo_layout.setSpacing(4)
            mo_layout.addWidget(QLabel("Master Output:"))
            
            self._master_box = QComboBox()
            self._master_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._master_box.setToolTip("Select system default audio output.")
            self._master_box.activated.connect(self._on_master_selected)
            mo_layout.addWidget(self._master_box)
            
            mo_refresh_btn = QPushButton("↺")
            mo_refresh_btn.setFixedSize(26, 26)
            mo_refresh_btn.setToolTip("Refresh outputs.")
            mo_refresh_btn.clicked.connect(self.master_refresh_requested.emit)
            mo_layout.addWidget(mo_refresh_btn)

            root_layout.addLayout(mo_layout)
            root_layout.addSpacing(10)

            # ── Fader Curve Intensity ──────────────────────────────────────────
            fc_layout = QHBoxLayout()
            fc_layout.setContentsMargins(0, 0, 0, 0)
            fc_layout.setSpacing(6)

            fc_layout.addWidget(QLabel("Fader Curve Intensity (Linear to Natural):"))

            self._curve_slider = QSlider(Qt.Orientation.Horizontal)
            self._curve_slider.setRange(100, 300)          # maps to 1.00 – 3.00
            self._curve_slider.setSingleStep(1)
            self._curve_slider.setPageStep(10)
            self._curve_slider.setTickInterval(50)
            self._curve_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            self._curve_slider.setMinimumWidth(200)
            self._curve_slider.setToolTip(
                "Controls the volume curve shape.\n"
                "1.0 = Linear | 2.0 = Quadratic (default) | 3.0 = Cubic (most natural)"
            )
            initial_exp = self._config.get_volume_exponent()
            self._curve_slider.setValue(round(initial_exp * 100))
            self._curve_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            fc_layout.addWidget(self._curve_slider)

            self._curve_value_label = QLabel(f"Value: {initial_exp:.2f}")
            self._curve_value_label.setMinimumWidth(70)
            fc_layout.addWidget(self._curve_value_label)

            self._curve_slider.valueChanged.connect(self._on_curve_changed)

            root_layout.addLayout(fc_layout)
            root_layout.addSpacing(6)

            # Bottom row (Toggles & Debug)

            bottom_layout = QHBoxLayout()
            bottom_layout.setContentsMargins(0, 0, 0, 0)
            bottom_layout.setSpacing(4)
            
            self._transparency_cb = QCheckBox("Transparency")
            self._transparency_cb.setToolTip("Enable translucent window background.")
            self._transparency_cb.setChecked(self._config.transparency)
            self._transparency_cb.toggled.connect(self._on_transparency_toggled)
            bottom_layout.addWidget(self._transparency_cb)
            
            self._show_invert_cb = QCheckBox("Show Invert Option")
            self._show_invert_cb.setToolTip("Show or hide the 'Invert' checkbox for each audio channel in the main mixer.")
            self._show_invert_cb.setChecked(self._config.show_invert_option)
            self._show_invert_cb.toggled.connect(self._on_show_invert_toggled)
            bottom_layout.addWidget(self._show_invert_cb)

            self._vu_meter_cb = QCheckBox("VU Meter (experimental)")
            self._vu_meter_cb.setToolTip(
                "Show real-time audio level bars next to each fader. "
                "Experimental — disable if you notice performance issues."
            )
            self._vu_meter_cb.setChecked(self._config.vu_meter_enabled)
            self._vu_meter_cb.toggled.connect(self._on_vu_meter_toggled)
            bottom_layout.addWidget(self._vu_meter_cb)

            bottom_layout.addStretch()
            
            root_layout.addLayout(bottom_layout)
            
            # ── Panic Buttons ──
            panic_layout = QHBoxLayout()
            panic_layout.setContentsMargins(0, 0, 0, 0)
            panic_layout.setSpacing(4)
            
            self._panic_btn = QPushButton("⚠ Reset Audio (Panic)")
            self._panic_btn.setStyleSheet("""
                QPushButton { color: #ff4444; font-weight: bold; border: 1px solid rgba(255, 68, 68, 0.3); }
                QPushButton:hover { background-color: rgba(255, 68, 68, 0.15); color: #ff6666; }
            """)
            self._panic_btn.setToolTip("Evacuate all apps to default output, destroy V-Sinks, reset UI mapping.")
            self._panic_btn.clicked.connect(self.panic_triggered.emit)
            self._panic_btn.setVisible(not is_windows())
            panic_layout.addWidget(self._panic_btn)

            self._midi_panic_btn = QPushButton("🎹 Reset MIDI (Panic)")
            self._midi_panic_btn.setStyleSheet("""
                QPushButton { color: #ff4444; font-weight: bold; border: 1px solid rgba(255, 68, 68, 0.3); }
                QPushButton:hover { background-color: rgba(255, 68, 68, 0.15); color: #ff6666; }
            """)
            self._midi_panic_btn.setToolTip("Restart MIDI subsystem and clean up virtual ports.")
            self._midi_panic_btn.clicked.connect(self.midi_panic_triggered.emit)
            self._midi_panic_btn.setVisible(not is_windows())
            panic_layout.addWidget(self._midi_panic_btn)
            
            root_layout.addLayout(panic_layout)
            
            
            # ── Logging Controls ──
            self._debug_box = QGroupBox("Logging Controls")
            
            debug_layout = QVBoxLayout(self._debug_box)
            debug_layout.setContentsMargins(5, 5, 5, 5)
            debug_layout.setSpacing(4)
            
            log_ctrl_layout = QHBoxLayout()
            log_ctrl_layout.setContentsMargins(0, 0, 0, 0)
            log_ctrl_layout.setSpacing(4)
            
            self._debug_logging_cb = QCheckBox("Enable Extensive Debug Logging")
            self._debug_logging_cb.setToolTip("Switch log level to DEBUG. Takes effect immediately (early start-up logs require restart).")
            self._debug_logging_cb.setChecked(self._config.debug_logging)
            self._debug_logging_cb.toggled.connect(self._on_debug_logging_toggled)
            log_ctrl_layout.addWidget(self._debug_logging_cb)

            self._open_log_folder_btn = QPushButton("Open Log Folder")
            self._open_log_folder_btn.setToolTip("Open the directory where NativMix stores its log files (in Dolphin or system file manager).")
            self._open_log_folder_btn.clicked.connect(self._open_log_folder)
            log_ctrl_layout.addWidget(self._open_log_folder_btn)

            debug_layout.addLayout(log_ctrl_layout)
            
            root_layout.addWidget(self._debug_box)

            # ── About ──
            about_label = QLabel(
                f'NativMix v{__version__}'
                ' &nbsp;·&nbsp; '
                'by <a href="https://knoellix.net/">knoelliX</a>'
                ' &nbsp;·&nbsp; '
                '<a href="https://github.com/knoelliX/NativMix">GitHub</a>'
                ' &nbsp;·&nbsp; '
                '<a href="https://github.com/knoelliX/NativMix/issues">Report Issue</a>'
            )
            about_label.setOpenExternalLinks(True)
            about_label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
            about_label.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
            about_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            small = about_label.font()
            small.setPointSize(8)
            about_label.setFont(small)
            root_layout.addWidget(about_label)

        except Exception:
            logger.exception("Failed to build extended settings UI")

        self._input_mode_box.currentIndexChanged.connect(self._on_input_mode_changed)
        self._midi_box.currentIndexChanged.connect(self._on_midi_device_selected)
        self._port_box.currentIndexChanged.connect(self._on_port_selected)
        self._update_hardware_ui_state()



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

    def set_midi_status(self, status_type: str, message: str) -> None:
        """Update the MIDI status label with color coding."""
        colors = {
            "stable": "#44ff44",          # Green
            "connecting": "#ffff44",      # Yellow
            "error_temporary": "#ffaa44",  # Orange
            "error_critical": "#ff4444"    # Red
        }
        color = colors.get(status_type, "#ffffff")
        self._midi_status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        self._midi_status_label.setText(message)
        self._midi_status_label.setToolTip(message)


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

    def _populate_midi_ports(self) -> None:
        self._midi_box.blockSignals(True)
        self._midi_box.clear()

        from nativmix.utils.paths import is_windows
        from nativmix.hardware.midi import ensure_midi_backend

        # Probe MIDI backend once — used for port enumeration and vport availability check.
        backend = ensure_midi_backend()

        # Virtual Port: hidden on Windows (WinMM has no virtual port support).
        # On Linux with non-rtmidi backend (e.g. portmidi on Fedora): show but gray out.
        if not is_windows():
            self._midi_box.addItem("NativMix (Virtual Port)", userData="VIRTUAL_PORT")
            if backend != "rtmidi":
                from PyQt6.QtGui import QStandardItem
                item: QStandardItem = self._midi_box.model().item(0)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(
                    "Virtual MIDI ports require the rtmidi backend.\n"
                    f"The active backend is {backend} (typical on Fedora/Nobara).\n"
                    "Install python-rtmidi to enable this option."
                )

        try:
            import mido
            names = mido.get_input_names()

            seen_bases = set()
            filtered_names = []

            for name in names:
                if "Midi Through" in name or "NativMix" in name:
                    continue
                # Naive deduplication: ALSA often appends client/port numbers like " 20:0"
                parts = name.rsplit(" ", 1)
                base_name = parts[0] if len(parts) > 1 and ":" in parts[1] else name

                if base_name not in seen_bases:
                    seen_bases.add(base_name)
                    filtered_names.append(name)

            for name in filtered_names:
                self._midi_box.addItem(name, userData=name)
        except ImportError:
            logger.warning("No MIDI backend available, cannot populate MIDI ports")
        except Exception as exc:
            logger.error("Error enumerating MIDI ports: %s", exc)

        restore = self._config.midi_device
        # Default to VIRTUAL_PORT if nothing is set (Linux only; on Windows pick first physical device)
        if not restore:
            restore = "" if is_windows() else "VIRTUAL_PORT"

        idx = self._midi_box.findData(restore)
        if idx >= 0:
            self._midi_box.setCurrentIndex(idx)
        else:
            # If a physical device was selected but is now gone, show it as disconnected
            if restore not in ("VIRTUAL_PORT", ""):
                self._midi_box.addItem(f"{restore} (Disconnected)", userData=restore)
                self._midi_box.setCurrentIndex(self._midi_box.count() - 1)
            else:
                # On Windows Virtual Port is not in the list → index 0 is the first physical port.
                # On Linux with non-rtmidi backend → Virtual Port is at 0 but disabled, skip to 1.
                _skip_vport = not is_windows() and backend != "rtmidi"
                self._midi_box.setCurrentIndex(1 if _skip_vport and self._midi_box.count() > 1 else 0)

        self._midi_box.blockSignals(False)

    def _update_hardware_ui_state(self) -> None:
        mode = self._config.input_mode
        self._midi_box.setEnabled(mode in ("hybrid", "midi_only"))
        self._port_box.setEnabled(mode in ("usb", "hybrid"))

    @pyqtSlot(int)
    def _on_input_mode_changed(self, index: int) -> None:
        modes = ["usb", "hybrid", "midi_only"]
        mode = modes[index] if 0 <= index < len(modes) else "usb"
        self._config.input_mode = mode
        self._config.save()
        self._update_hardware_ui_state()
        logger.debug("Input mode changed to: %s", mode)

    @pyqtSlot(int)
    def _on_midi_device_selected(self, index: int) -> None:
        device = self._midi_box.itemData(index)
        if device is not None:
            self._config.midi_device = device
            self._config.save()
            self.midi_device_changed.emit(device)
            logger.debug("MIDI device selected: %s", device)

    @pyqtSlot(int)
    def _on_port_selected(self, index: int) -> None:
        port = self._port_box.itemData(index)   # None = Auto
        self._config.hardware_port = port
        self._config.save()
        self.port_changed.emit(port or "")
        logger.debug("Port selection changed: %s", port or "auto")

    @pyqtSlot(bool)
    def _on_debug_logging_toggled(self, checked: bool) -> None:
        self._config.debug_logging = checked
        self._config.save()
        if checked:
            logging.getLogger().setLevel(logging.DEBUG)
            logger.debug("Extensive Debug Logging enabled.")
        else:
            logging.getLogger().setLevel(logging.INFO)
            logger.debug("Extensive Debug Logging disabled.")

    @pyqtSlot()
    def _open_log_folder(self) -> None:
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        from nativmix.utils.paths import get_log_dir
        
        log_dir = get_log_dir()
        if log_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))
        else:
            logger.warning("Log directory does not exist yet: %s", log_dir)

    @pyqtSlot(int)
    def _on_master_selected(self, index: int) -> None:
        name = self._master_box.itemData(index)
        if name:
            self.master_output_changed.emit(name)
            logger.debug("Master output selected via GUI: %s", name)

    @pyqtSlot(bool)
    def _on_autostart_toggled(self, checked: bool) -> None:
        if self._use_windows_autostart:
            ok = _enable_autostart_windows() if checked else _disable_autostart_windows()
            actual = _is_autostart_enabled_windows()
            _suffix = " (Registry)"
        elif self._use_systemd:
            ok = _enable_service() if checked else _disable_service()
            actual = _is_service_enabled()
            _suffix = " (systemd)"
        else:
            ok = _enable_autostart() if checked else _disable_autostart()
            actual = _is_autostart_enabled()
            _suffix = ""
        self._autostart_btn.blockSignals(True)
        self._autostart_btn.setChecked(actual)
        self._autostart_btn.setText(f"Autostart: {'ON' if actual else 'OFF'}{_suffix}")
        self._autostart_btn.blockSignals(False)
        if not ok:
            logger.warning("Autostart toggle failed (windows=%s, systemd=%s)", self._use_windows_autostart, self._use_systemd)

    @pyqtSlot(bool)
    def _on_transparency_toggled(self, checked: bool) -> None:
        self._config.transparency = checked
        self._config.save()
        logger.debug("Transparency toggled: %s", checked)

    @pyqtSlot(bool)
    def _on_show_invert_toggled(self, checked: bool) -> None:
        self._config.show_invert_option = checked
        self._config.save()
        logger.debug("Show Invert Option toggled: %s", checked)

    @pyqtSlot(bool)
    def _on_vu_meter_toggled(self, checked: bool) -> None:
        self._config.vu_meter_enabled = checked
        self._config.save()
        logger.debug("VU Meter toggled: %s", checked)

    @pyqtSlot(int)
    def _on_curve_changed(self, slider_value: int) -> None:
        """Convert slider integer (100-300) to exponent (1.00-3.00) and persist."""
        exponent = slider_value / 100.0
        self._curve_value_label.setText(f"Value: {exponent:.2f}")
        self._config.set_volume_exponent(exponent)
        self._config.save()
        logger.debug("Volume curve exponent updated to: %.2f", exponent)

