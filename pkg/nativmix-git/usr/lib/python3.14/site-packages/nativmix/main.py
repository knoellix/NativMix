"""
Application entry point for NativMix.

Sets the process title (for task managers) and the desktop file name
(for Wayland icon association) before any Qt objects are created.
"""

from __future__ import annotations

import os
import sys
import platform
import logging
import argparse

import setproctitle
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QStyleFactory
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalSocket, QLocalServer
from PyQt6.QtCore import pyqtSignal, QObject

APP_NAME = "nativmix"
# Qt6 setDesktopFileName requires the name WITHOUT the .desktop suffix
DESKTOP_FILE = "nativmix"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# IPC Server Name
IPC_SERVER_NAME = "nativmix_ipc"

class IpcServer(QObject):
    toggle_mute_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        # Clean up stale socket from previous crash
        QLocalServer.removeServer(IPC_SERVER_NAME)
        if self.server.listen(IPC_SERVER_NAME):
            logger.info("IPC Server listening on '%s'", IPC_SERVER_NAME)
        else:
            logger.error("IPC Server failed to listen: %s", self.server.errorString())
        self.server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self):
        socket = self.server.nextPendingConnection()
        socket.readyRead.connect(lambda: self._handle_ready_read(socket))
        socket.disconnected.connect(socket.deleteLater)

    def _handle_ready_read(self, socket):
        data = socket.readAll().data().decode("utf-8").strip()
        if data.startswith("toggle_mute:"):
            try:
                ch_idx = int(data.split(":")[1])
                self.toggle_mute_requested.emit(ch_idx)
            except ValueError:
                logger.error("Invalid IPC message: %s", data)
        socket.disconnectFromServer()


def main() -> None:
    # Rename the process so task managers show "nativmix" instead of "python"
    setproctitle.setproctitle(APP_NAME)
    
    app = QApplication(sys.argv)
    
    # ── CLI Parsing (Client Mode) ──
    parser = argparse.ArgumentParser(description="NativMix Hardware Volume Mixer")
    parser.add_argument("--toggle-mute", type=int, metavar="CHANNEL_INDEX",
                        help="Toggle mute for a specific channel via IPC (0-indexed)")
    args, unknown = parser.parse_known_args()

    if args.toggle_mute is not None:
        socket = QLocalSocket(app)
        socket.connectToServer(IPC_SERVER_NAME)
        if socket.waitForConnected(1000):
            socket.write(f"toggle_mute:{args.toggle_mute}".encode("utf-8"))
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            sys.exit(0)
        else:
            print(f"Error: NativMix is not running (could not connect to IPC server '{IPC_SERVER_NAME}').")
            logger.error("IPC Client Error: %s", socket.errorString())
            sys.exit(1)

    # ── Main GUI Mode ──
    # ── Wayland App-Identity (Critical for KDE) ──
    app.setApplicationName("nativmix")
    app.setApplicationDisplayName("NativMix")
    app.setDesktopFileName(DESKTOP_FILE)

    from nativmix.utils.paths import get_icon_path
    icon_path = get_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    # ── Dynamic Theme & Fallback Engine ──
    # Retrieve all available styles, convert to lower case for insensitive matching
    available_styles = {s.lower(): s for s in QStyleFactory.keys()}
    
    # Priority 1: kvantum (Plasma transparency/blur engines)
    # Priority 2: breeze (Plasma standard)
    # Priority 3: fusion (Qt standard fallback)
    chosen_style = None
    for pref in ("kvantum", "breeze", "fusion"):
        if pref in available_styles:
            chosen_style = available_styles[pref]
            app.setStyle(chosen_style)
            logger.info("Theme engine loaded: %s", chosen_style)
            break
            
    # If we fell all the way back to fusion (which defaults to bright gray),
    # force a dark palette to prevent blinding the user.
    if chosen_style and chosen_style.lower() == "fusion":
        from PyQt6.QtGui import QPalette, QColor
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.Text, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(208, 208, 208))
        dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
        app.setPalette(dark_palette)
        logger.info("Applied dark fallback palette for Fusion")

    # Required for Wayland to associate the window with the correct .desktop entry
    # (Already fully set via Wayland App-Identity above)

    # Keep running when the main window is closed (tray icon keeps the app alive)
    app.setQuitOnLastWindowClosed(False)

    # Platform guard
    os_name = platform.system()
    if os_name != "Linux":
        if os_name == "Windows":
            raise NotImplementedError("Windows backend (WASAPI) is not yet implemented.")
        raise RuntimeError(f"Unsupported platform: {os_name}")

    from nativmix.audio.manager import PipeWireManager
    from nativmix.hardware.arduino import ArduinoThread
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.gui.main_window import MainWindow
    from nativmix.gui.tray_icon import TrayIcon

    # ── Config ─────────────────────────────────────────────────────────
    config = ConfigManager()

    # ── Audio backend ───────────────────────────────────────────────────
    backend = PipeWireManager(config=config)

    # ── Arduino thread ──────────────────────────────────────────────────
    arduino = ArduinoThread(
        port=config.hardware_port,
        num_channels=config.num_channels,
        inverted=config.invert_map,
        threshold=config.threshold,
    )

    # ── GUI ─────────────────────────────────────────────────────────────
    window = MainWindow(config=config, backend=backend)

    tray = TrayIcon(main_window=window)
    if not tray.isSystemTrayAvailable():
        logger.warning("System tray not available – running without tray icon")
    else:
        tray.show()

    # ── Signal wiring ───────────────────────────────────────────────────
    # Backend mute updates → GUI Mute Buttons
    backend.mute_state_changed.connect(window.on_mute_state_changed)
    
    # Arduino poti values → audio backend (volume control)
    arduino.volumes_changed.connect(backend.apply_poti_volumes)
    # Arduino poti values → GUI sliders (visual feedback)
    arduino.volumes_changed.connect(window.on_volumes_changed)
    backend.channel_volume_changed.connect(window.on_channel_volume_changed)
    # Dynamic channel count → GUI rebuild + config update
    arduino.channel_count_changed.connect(window.on_channel_count_changed)
    # Port selector → immediate reconnect on the chosen port
    window.settings_panel.port_changed.connect(
        lambda port: arduino.set_port(port if port else None)
    )
    # Arduino connected → mark port with ★ in the combo box
    arduino.connection_changed.connect(
        lambda connected: (
            logger.info("Arduino %s", "connected" if connected else "disconnected"),
            window.settings_panel.mark_connected_port(arduino.current_port if connected else None),
        )
    )
    # Live-Update for inversion flags and threshold without restart
    config.settings_changed.connect(lambda: arduino.reload_settings(config))

    # ── Start background threads ────────────────────────────────────────
    backend.start()
    arduino.start()

    # ── IPC Server ──
    ipc_server = IpcServer(parent=app)
    ipc_server.toggle_mute_requested.connect(backend.toggle_mute)

    # ── Show window ─────────────────────────────────────────────────────
    # window.show()  # Let tray icon handle visibility to start hidden or normal

    exit_code = app.exec()

    # ── Clean shutdown ──────────────────────────────────────────────────
    arduino.stop()
    backend.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
