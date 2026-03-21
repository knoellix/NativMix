#!/usr/bin/env python3
"""
Application entry point for NativMix.

Sets the process title (for task managers) and the desktop file name
(for Wayland icon association) before any Qt objects are created.
"""

from __future__ import annotations

import sys
import os
import time
import platform
import logging
import argparse
import signal
import socket
import threading

import setproctitle
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QStyleFactory
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalSocket, QLocalServer
from PyQt6.QtCore import pyqtSignal, QObject, QTimer, QSocketNotifier

APP_NAME = "nativmix"
# Qt6 setDesktopFileName requires the name WITHOUT the .desktop suffix
DESKTOP_FILE = "nativmix"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


from nativmix.utils.logger import setup_logging
from nativmix.utils.paths import get_ipc_socket_path, migrate_legacy_config_dir

IPC_SERVER_NAME = get_ipc_socket_path()


class IpcServer(QObject):
    toggle_mute_requested = pyqtSignal(int)
    list_sinks_requested = pyqtSignal(object)  # passing the socket to return data
    list_apps_requested = pyqtSignal(object)
    show_window_requested = pyqtSignal()  # emitted when a second instance sends "show"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)

        # ── Robust Stale Socket Cleanup ──
        # If the socket file exists, try a test connection to verify if a server is actually alive.
        if os.path.exists(IPC_SERVER_NAME):
            test_socket = QLocalSocket()
            test_socket.connectToServer(IPC_SERVER_NAME)
            if not test_socket.waitForConnected(200):
                # Connection failed -> socket is stale
                logger.info("Removed stale socket file: %s", IPC_SERVER_NAME)
                os.unlink(IPC_SERVER_NAME)
            test_socket.close()

        if self.server.listen(IPC_SERVER_NAME):
            logger.info("IPC Server listening on '%s'", IPC_SERVER_NAME)
        else:
            logger.error("IPC Server failed to listen: %s", self.server.errorString())
        self.server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self):
        socket = self.server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda: self._handle_ready_read(socket))
        socket.disconnected.connect(socket.deleteLater)

    def _handle_ready_read(self, socket):
        try:
            data = socket.readAll().data().decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            logger.warning("IPC: received non-UTF-8 data: %s", exc)
            socket.disconnectFromServer()
            return
        if data.startswith("toggle_mute:"):
            try:
                ch_idx = int(data.split(":")[1])
                self.toggle_mute_requested.emit(ch_idx)
            except ValueError:
                logger.error("Invalid IPC message: %s", data)
        elif data == "list_sinks":
            self.list_sinks_requested.emit(socket)
            return # Socket handled by recipient
        elif data == "list_apps":
            self.list_apps_requested.emit(socket)
            return
        elif data == "show":
            self.show_window_requested.emit()
        socket.disconnectFromServer()


def _install_excepthook() -> None:
    """Install a global exception handler that logs crashes to the XDG cache dir."""
    from nativmix.utils.paths import get_log_dir
    crash_log = get_log_dir() / "nativmix_crash.log"

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Unhandled exception — writing crash log to %s", crash_log)
        logger.exception("Crash details:", exc_info=(exc_type, exc_value, exc_tb))
        try:
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            import traceback
            with open(crash_log, "w", encoding="utf-8") as f:
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except OSError:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook


def main() -> None:
    # Rename the process so task managers show "nativmix" instead of "python"
    try:
        setproctitle.setproctitle(APP_NAME)
    except Exception as e:
        logger.warning("Could not set proctitle: %s", e)

    # ── CLI Parsing ──────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="NativMix Hardware Volume Mixer")
    parser.add_argument("--toggle-mute", type=int, metavar="CHANNEL",
                        help="Toggle mute for a channel via IPC (1-indexed: 1 = first channel)")
    parser.add_argument("--list-sinks", action="store_true", help="List active NativMix V-Sinks via IPC")
    parser.add_argument("--list-apps", action="store_true", help="List detected audio apps via IPC")
    parser.add_argument("--hidden", action="store_true", help="Start the application minimized to tray")
    parser.add_argument("--show", action="store_true", help="Bring an already running instance to foreground")
    args, unknown = parser.parse_known_args()

    # ── Single-Instance Guard + IPC client (pure Python, no Qt needed) ───────
    # Build the IPC message from CLI args (shared by both platforms).
    def _build_ipc_msg(args) -> str | None:
        """Return the IPC message to send, or None if we should just start."""
        if args.toggle_mute is not None:
            return f"toggle_mute:{args.toggle_mute - 1}"
        if args.list_sinks:
            return "list_sinks"
        if args.list_apps:
            return "list_apps"
        return "show"  # default: bring window to front

    if sys.platform == "win32":
        # QLocalServer on Windows listens on \\.\pipe\<name>.
        # Try to open the pipe; success means an instance is already running.
        import ctypes
        import ctypes.wintypes as _wt
        _pipe_path = r'\\.\pipe\\' + IPC_SERVER_NAME
        _k32 = ctypes.windll.kernel32
        _GENERIC_RW = 0xC0000000
        _OPEN_EXISTING = 3
        # Set restype to HANDLE (c_void_p) so the return value is pointer-sized.
        # Without this, ctypes defaults to c_int (32-bit) and the comparison with
        # INVALID_HANDLE_VALUE (-1 as 64-bit pointer) always fails on 64-bit Windows.
        _k32.CreateFileW.restype = _wt.HANDLE
        _INVALID_HANDLE = _wt.HANDLE(-1).value

        _k32.WaitNamedPipeW(_pipe_path, 500)   # wait up to 500 ms if server is busy
        _h = _k32.CreateFileW(_pipe_path, _GENERIC_RW, 0, None, _OPEN_EXISTING, 0, None)

        if _h is not None and _h != _INVALID_HANDLE:
            # Running instance found — forward command and exit.
            try:
                _msg = _build_ipc_msg(args)
                if args.toggle_mute is not None and args.toggle_mute < 1:
                    print(f"Error: channel number must be ≥ 1 (got {args.toggle_mute})", file=sys.stderr)
                    sys.exit(1)
                _data = _msg.encode("utf-8")
                _written = _wt.DWORD(0)
                _ok = _k32.WriteFile(_h, _data, len(_data), ctypes.byref(_written), None)
                if not _ok:
                    logging.getLogger(__name__).warning("IPC WriteFile failed (pipe may have closed)")

                if args.list_sinks or args.list_apps:
                    _chunks = []
                    _buf = ctypes.create_string_buffer(65536)
                    _nread = _wt.DWORD(0)
                    while True:
                        _ok = _k32.ReadFile(_h, _buf, len(_buf), ctypes.byref(_nread), None)
                        if not _ok or _nread.value == 0:
                            break
                        _chunks.append(_buf.raw[:_nread.value])
                    try:
                        print(b"".join(_chunks).decode("utf-8"))
                    except UnicodeDecodeError as exc:
                        logging.getLogger(__name__).warning("IPC response decode error: %s", exc)
            finally:
                _k32.CloseHandle(_h)
            logging.getLogger(__name__).info("Forwarded '%s' to running instance and exiting.", _msg)
            sys.exit(0)
        # else: no running instance → continue as primary

    else:
        # AF_UNIX (Linux / macOS) — two-phase singleton guard:
        #
        # Phase 1: fcntl.flock() on a lock file.  Acquired instantly if we are
        # the first process; fails immediately (LOCK_NB) if another process
        # already holds it.  The OS releases the lock automatically when the
        # owning process exits, so stale locks are never a problem.
        # This prevents the race condition where two instances start at the
        # same time (e.g. systemd + XDG autostart at login) and both see
        # FileNotFoundError on the IPC socket before either has created it.
        #
        # Phase 2: if the lock is already held, wait up to 1 s for the primary
        # instance to create its IPC socket, then forward the command and exit.
        import fcntl as _fcntl
        _lock_path = f"/tmp/nativmix_{os.getuid()}.lock"
        _lock_fh = open(_lock_path, "w")
        _is_primary = False
        try:
            _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _is_primary = True
            # Write PID so admins can identify the process if needed.
            _lock_fh.write(str(os.getpid()))
            _lock_fh.flush()
            # Keep _lock_fh open for the lifetime of main() so the OS lock
            # is held until the process exits.  Do NOT close it here.
        except (IOError, OSError):
            _lock_fh.close()

        if not _is_primary:
            # Another instance holds the lock.  Wait up to 1 s for its IPC
            # socket to appear, then forward the CLI command and exit.
            _sock_path = IPC_SERVER_NAME
            msg = _build_ipc_msg(args)
            if args.toggle_mute is not None and args.toggle_mute < 1:
                print(f"Error: channel number must be ≥ 1 (got {args.toggle_mute})", file=sys.stderr)
                sys.exit(1)
            _forwarded = False
            for _attempt in range(10):  # 10 × 100 ms = 1 s max wait
                _ipc = None
                try:
                    _ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    _ipc.settimeout(0.5)
                    _ipc.connect(_sock_path)
                    _ipc.sendall(msg.encode("utf-8"))
                    if args.list_sinks or args.list_apps:
                        _ipc.shutdown(socket.SHUT_WR)
                        response = b""
                        while True:
                            chunk = _ipc.recv(4096)
                            if not chunk:
                                break
                            response += chunk
                        print(response.decode("utf-8"))
                    _forwarded = True
                    break
                except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                    time.sleep(0.1)
                finally:
                    try:
                        _ipc.close()
                    except Exception:
                        pass
            if _forwarded:
                logging.getLogger(__name__).info(
                    "Forwarded '%s' to running instance and exiting.", msg)
            else:
                logging.getLogger(__name__).warning(
                    "Another instance detected but IPC socket unreachable after 1 s — exiting.")
            sys.exit(0)

    # ── Path Robustness ──────────────────────────────────────────────────────
    # If running as a standalone script (e.g. locally or via desktop file),
    # ensure the 'lib' directory is in the python path so 'import nativmix' works.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _lib_root = os.path.dirname(_script_dir) # ../ (points to lib/)
    if _lib_root not in sys.path:
        sys.path.insert(0, _lib_root)

    # ── Module Path Debug ────────────────────────────────────────────────────
    try:
        import nativmix
        import nativmix.gui.settings_panel as _sp
        logger.debug("nativmix loaded from: %s", nativmix.__file__)
        logger.debug("settings_panel loaded from: %s", _sp.__file__)
    except ImportError as e:
        # If we can't import our own package, we must exit clearly.
        print(f"CRITICAL ERROR: Could not import nativmix package: {e}", file=sys.stderr)
        sys.exit(1)

    logger.debug("Python: %s", sys.executable)

    # ── Wayland Session Guard (Linux only) ───────────────────────────────────
    # Display guard — Wayland preferred, X11 fallback for openSUSE/X11 sessions.
    # Retry briefly to handle slow compositor startup.
    # Skipped on Windows: no DISPLAY/WAYLAND_DISPLAY — Qt uses Win32 directly.
    if sys.platform != "win32":
        max_attempts = 5
        attempt = 0
        display_ready = False
        while attempt < max_attempts:
            if 'WAYLAND_DISPLAY' in os.environ or 'DISPLAY' in os.environ:
                display_ready = True
                break
            logger.warning("No display server found. Retrying (%d/%d)...", attempt + 1, max_attempts)
            time.sleep(1)
            attempt += 1

        if not display_ready:
            print("CRITICAL ERROR: No display server (WAYLAND_DISPLAY or DISPLAY) found.", file=sys.stderr)
            sys.exit(1)

    app = QApplication(sys.argv)
    # Keep the app alive when the window is closed (tray icon takes over).
    app.setQuitOnLastWindowClosed(False)

    # ── Main GUI Mode ──
    # ── Wayland App-Identity (Critical for KDE) ──
    app.setApplicationName("nativmix")
    app.setApplicationDisplayName("NativMix")
    # Enforce correct App-ID for Wayland compositor to map .desktop file
    from PyQt6.QtGui import QGuiApplication
    QGuiApplication.setDesktopFileName("nativmix")

    from nativmix.utils.paths import get_icon_path
    icon_path = get_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    # ── Dynamic Theme & Fallback Engine ──
    try:
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
    except Exception as e:
        logger.warning("Failed to apply Qt theme/style: %s. Using default.", e)

    # Required for Wayland to associate the window with the correct .desktop entry
    # (Already fully set via Wayland App-Identity above)

    # Platform guard + backend selection
    os_name = platform.system()
    if os_name == "Linux":
        from nativmix.audio.manager import PipeWireManager
        backend_instance = lambda cfg: PipeWireManager(config=cfg)  # noqa: E731
    elif os_name == "Windows":
        from nativmix.audio.wasapi_manager import WasapiManager
        backend_instance = lambda cfg: WasapiManager(config=cfg)    # noqa: E731
    else:
        raise RuntimeError(f"Unsupported platform: {os_name}")

    from nativmix.hardware.arduino import ArduinoThread
    from nativmix.hardware.midi import MidiThread
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.gui.main_window import MainWindow
    from nativmix.gui.tray_icon import TrayIcon

    # ── One-time config directory migration (NativMix → nativmix) ──────
    migrate_legacy_config_dir()

    # ── Remove legacy user-level XDG autostart entry ─────────────────
    # Old versions created ~/.config/autostart/nativmix.desktop automatically.
    # Autostart is now opt-in (systemd user service). Remove the old file
    # silently so users don't end up with two instances running.
    try:
        import pathlib
        _legacy_autostart = pathlib.Path.home() / ".config" / "autostart" / "nativmix.desktop"
        if _legacy_autostart.exists():
            _legacy_autostart.unlink()
            logger.info("Removed legacy autostart entry: %s", _legacy_autostart)
    except Exception as _e:
        logger.debug("Could not remove legacy autostart entry: %s", _e)

    # ── Config ─────────────────────────────────────────────────────────
    config = ConfigManager()

    # ── Final Logging: file + level from config ─────────────────────────
    setup_logging(config.debug_logging)
    _install_excepthook()

    # ── Audio backend ───────────────────────────────────────────────────
    backend = backend_instance(config)

    # ── Arduino thread ──────────────────────────────────────────────────
    arduino = ArduinoThread(
        port=config.hardware_port,
        num_channels=config.num_channels,
        inverted=config.invert_map,
        threshold=config.threshold,
        input_mode=config.input_mode,
    )

    # ── MIDI thread ─────────────────────────────────────────────────────
    midi = MidiThread(device_name=config.midi_device, input_mode=config.input_mode)
    midi.update_mappings(config.get_all_midi_mappings())

    # ── GUI ─────────────────────────────────────────────────────────────
    window = MainWindow(config=config, backend=backend, arduino_thread=arduino, midi_thread=midi)

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

    # MIDI volumes → audio backend
    midi.midi_volumes_changed.connect(backend.apply_midi_volumes)
    # MIDI CC movements → visual feedback on sliders
    midi.midi_volumes_changed.connect(
        lambda mappings: [window.on_channel_volume_changed(ch, vol) for ch, vol in mappings]
    )
    # MIDI CC Received → Learn handshake
    midi.midi_cc_received.connect(window.on_midi_cc_received)
    
    # MIDI Connection state → UI Learn Reset
    midi.connection_changed.connect(window._on_midi_connection_changed)

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
    config.settings_changed.connect(lambda: (
        midi.set_device(config.midi_device),
        midi.set_mode(config.input_mode),
        midi.update_mappings(config.get_all_midi_mappings())
    ))
    
    # MIDI Status display
    midi.status_changed.connect(window.settings_panel.set_midi_status)
    # MIDI Restart (Panic)
    window.settings_panel.midi_panic_triggered.connect(midi.restart_midi)
    # Initialize arduino settings immediately so the curve is applied on startup
    arduino.reload_settings(config)
    # Routing update: when the GUI changes a channel mapping, the backend must
    # immediately move the affected audio stream to/from the V-Sink.
    config.mapping_changed.connect(backend._on_mapping_changed)

    # ── Startup Coordination (Flicker Protection) ──
    class StartupCoordinator(QObject):
        ready = pyqtSignal()
        def __init__(self):
            super().__init__()
            self._backends_ready = {"audio": False}
        def mark_ready(self, source):
            self._backends_ready[source] = True
            if all(self._backends_ready.values()):
                self.ready.emit()

    coordinator = StartupCoordinator()
    backend.audit_finished.connect(lambda: coordinator.mark_ready("audio"))

    def on_app_ready():
        logger.info("Application ready - final UI assembly")
        window.finalize_ui()
        # Only show if not explicitly hidden via CLI
        if not args.hidden:
            window._show_requested = True
            window.show()
            window.raise_()
            # Do NOT call requestActivate() at startup: newly shown windows
            # receive focus from the compositor automatically.  On COSMIC,
            # xdg_activation_v1 triggers a dock bounce/flicker animation.
            # requestActivate() is kept in tray._show_window() and
            # _ipc_show_window() where the user explicitly requests focus.
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(500, lambda: setattr(window, "_show_requested", False))

    coordinator.ready.connect(on_app_ready)

    # ── Start background threads (AFTER CONNECTING SIGNALS) ─────────────
    backend.start()
    arduino.start()
    midi.start()

    # ── VU Meter peak monitor ────────────────────────────────────────────
    if hasattr(backend, "peak_monitor"):
        backend.peak_monitor.peaks_updated.connect(window.on_peaks_updated)
        if config.vu_meter_enabled:
            backend.peak_monitor.start()

        def _on_vu_settings_changed() -> None:
            if config.vu_meter_enabled:
                if not backend.peak_monitor.isRunning():
                    backend.peak_monitor.start()
            else:
                if backend.peak_monitor.isRunning():
                    backend.peak_monitor.stop()
                window.on_peaks_updated([0.0] * config.num_channels)

        config.settings_changed.connect(_on_vu_settings_changed)
    elif hasattr(backend, "peaks_updated"):
        # Windows (WasapiManager): peaks are emitted directly by the backend.
        backend.peaks_updated.connect(window.on_peaks_updated)

        def _on_vu_settings_changed_win() -> None:
            if not config.vu_meter_enabled:
                window.on_peaks_updated([0.0] * config.num_channels)

        config.settings_changed.connect(_on_vu_settings_changed_win)

    # ── IPC Server ──
    ipc_server = IpcServer(parent=app)
    ipc_server.toggle_mute_requested.connect(backend.toggle_mute)
    
    def handle_list_sinks(socket):
        import json
        data = backend.get_v_sinks_debug()
        socket.write(json.dumps(data, indent=2).encode("utf-8"))
        socket.disconnectFromServer()
        
    def handle_list_apps(socket):
        import json
        data = backend.get_active_streams_debug()
        socket.write(json.dumps(data, indent=2).encode("utf-8"))
        socket.disconnectFromServer()

    ipc_server.list_sinks_requested.connect(handle_list_sinks)
    ipc_server.list_apps_requested.connect(handle_list_apps)
    
    # "show" IPC command: bring the existing window to the foreground.
    # Uses QWindow.requestActivate() on Wayland (xdg_activation_v1, Qt 6.5+)
    # rather than QWidget.activateWindow(), which compositors ignore.
    def _ipc_show_window() -> None:
        window._show_requested = True
        window.showNormal()
        window.raise_()
        handle = window.windowHandle()
        if handle is not None:
            handle.requestActivate()
        else:
            window.activateWindow()
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(500, lambda: setattr(window, "_show_requested", False))

    ipc_server.show_window_requested.connect(_ipc_show_window)

    # ── Robust Signal Handling ────────────────────────────────────────
    if sys.platform != "win32":
        # POSIX: socketpair + set_wakeup_fd delivers signals reliably into the Qt event loop.
        # The C-level handler writes the signal number to sig_write; QSocketNotifier wakes Qt.
        sig_read, sig_write = socket.socketpair()
        sig_read.setblocking(False)

        try:
            signal.set_wakeup_fd(sig_write.fileno())
        except (ValueError, AttributeError):
            # ValueError: wakeup fd already set in a nested environment
            # AttributeError: platform does not support set_wakeup_fd
            pass

        def handle_socket_signal():
            try:
                data = sig_read.recv(1024)
                if data:
                    sig = int(data[0])
                    logger.debug("Signal %d processed via wakeup_fd", sig)
                    QApplication.quit()
            except Exception:
                pass

        notifier = QSocketNotifier(sig_read.fileno(), QSocketNotifier.Type.Read)
        notifier.activated.connect(handle_socket_signal)

        # Dummy Python handlers activate the C-level wakeup path above.
        def _sig_dummy(sig, frame):
            pass

        signal.signal(signal.SIGINT, _sig_dummy)
        signal.signal(signal.SIGTERM, _sig_dummy)
    else:
        # Windows: no socketpair / SIGTERM.  A periodic QTimer keeps the interpreter
        # alive so Ctrl+C (SIGINT) is processed between Qt event iterations.
        _sigint_timer = QTimer()
        _sigint_timer.setInterval(200)
        _sigint_timer.timeout.connect(lambda: None)  # wake the interpreter
        _sigint_timer.start()
        signal.signal(signal.SIGINT, lambda s, f: QApplication.quit())

    # ── Show window ─────────────────────────────────────────────────────
    # Window visibility is handled by the tray icon (show/hide on click)
    exit_code = app.exec()

    # ── Clean shutdown ──────────────────────────────────────────────────
    # Strategy A: a real threading.Timer survives Qt event loop exit.
    # If any stop() call hangs (e.g. libpulse/rtmidi blocked on a dead
    # socket during system shutdown), os._exit(0) fires unconditionally.
    # Exit code 0 prevents systemd/KDE from marking the process as crashed.
    _exit_watchdog = threading.Timer(
        8.0,
        lambda: (
            logger.warning("Graceful shutdown timed out — forcing exit"),
            os._exit(0),
        ),
    )
    _exit_watchdog.daemon = True
    _exit_watchdog.start()

    arduino.stop()
    midi.stop()
    backend.stop()

    _exit_watchdog.cancel()
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        from nativmix.utils.paths import get_log_dir
        crash_log = get_log_dir() / "nativmix_crash.log"
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        with open(crash_log, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)

        # Also print to stderr for terminal users
        traceback.print_exc()

        print(f"\nCRITICAL: NativMix crashed during startup.")
        print(f"A detailed crash report has been saved to: {crash_log}")
        sys.exit(1)