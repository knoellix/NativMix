#!/usr/bin/env python3
"""
Application entry point for NativMix.

Sets the process title (for task managers) and the desktop file name
(for Wayland icon association) before any Qt objects are created.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import pathlib
import platform
import signal
import socket
import sys
import threading
import time

import setproctitle
from PyQt6.QtCore import QObject, QSocketNotifier, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QStyleFactory

APP_NAME = "nativmix"
# Qt6 setDesktopFileName requires the name WITHOUT the .desktop suffix
DESKTOP_FILE = "nativmix"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


from nativmix.utils.logger import setup_logging  # noqa: E402
from nativmix.utils.paths import (  # noqa: E402
    get_ipc_socket_path,
    is_systemd_service,
    migrate_legacy_config_dir,
)

IPC_SERVER_NAME = get_ipc_socket_path()


class IpcServer(QObject):
    toggle_mute_requested = pyqtSignal(int)
    set_volume_requested = pyqtSignal(int, float)  # channel_idx (0-based), volume [0.0-1.0]
    list_sinks_requested = pyqtSignal(object)  # passing the socket to return data
    list_apps_requested = pyqtSignal(object)
    show_window_requested = pyqtSignal()  # emitted when a second instance sends "show"
    restart_requested = pyqtSignal()  # emitted when a second instance sends "restart"
    profile_switch_requested = pyqtSignal(str)  # "next", "prev", or profile name

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
                try:
                    os.unlink(IPC_SERVER_NAME)
                except FileNotFoundError:
                    pass
                except PermissionError:
                    logger.warning("Cannot remove stale socket (permission denied): %s", IPC_SERVER_NAME)
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
        # Data may already be buffered if the client sent before our readyRead
        # connection was set up — process it immediately in that case.
        if socket.bytesAvailable() > 0:
            self._handle_ready_read(socket)

    def _handle_ready_read(self, socket):
        try:
            data = socket.readAll().data().decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            logger.warning("IPC: received non-UTF-8 data: %s", exc)
            socket.disconnectFromServer()
            return
        logger.debug("IPC _handle_ready_read: data=%r bytesAvail=%d", data, socket.bytesAvailable())
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
        elif data == "restart":
            self.restart_requested.emit()
        elif data.startswith("profile:"):
            target = data[len("profile:"):]
            self.profile_switch_requested.emit(target)
        elif data.startswith("vol:"):
            try:
                _, ch_str, val_str = data.split(":")
                self.set_volume_requested.emit(int(ch_str), float(val_str))
            except (ValueError, TypeError):
                logger.error("Invalid IPC vol message: %s", data)
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
    parser.add_argument("--vol", nargs=2, metavar=("CHANNEL", "PERCENT"),
                        help="Set volume for a channel via IPC (channel: 1-indexed, percent: 0-100)")
    parser.add_argument("--list-sinks", action="store_true", help="List active NativMix V-Sinks via IPC")
    parser.add_argument("--list-apps", action="store_true", help="List detected audio apps via IPC")
    parser.add_argument("--hidden", action="store_true", help="Start the application minimized to tray")
    parser.add_argument("--show", action="store_true", help="Bring an already running instance to foreground")
    parser.add_argument("--restart", action="store_true", help="Restart a running instance (full process restart)")
    parser.add_argument(
        "--profile",
        metavar="TARGET",
        help="Switch profile: 'next', 'prev', or a profile name",
    )
    args, unknown = parser.parse_known_args()

    # ── Single-Instance Guard + IPC client (pure Python, no Qt needed) ───────
    # Build the IPC message from CLI args (shared by both platforms).
    def _build_ipc_msg(args) -> str | None:
        """Return the IPC message to send, or None if we should just start."""
        if args.toggle_mute is not None:
            return f"toggle_mute:{args.toggle_mute - 1}"
        if args.vol is not None:
            pct = float(args.vol[1])
            value = max(0.0, min(100.0, pct)) / 100.0
            return f"vol:{int(args.vol[0]) - 1}:{value:.6f}"
        if args.list_sinks:
            return "list_sinks"
        if args.list_apps:
            return "list_apps"
        if args.restart:
            return "restart"
        if args.profile:
            return f"profile:{args.profile}"
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
                if args.vol is not None:
                    try:
                        _ch = int(args.vol[0])
                        _pct = float(args.vol[1])
                    except ValueError:
                        print("Error: --vol requires integer CHANNEL and numeric PERCENT", file=sys.stderr)
                        sys.exit(1)
                    if _ch < 1:
                        print(f"Error: channel number must be ≥ 1 (got {_ch})", file=sys.stderr)
                        sys.exit(1)
                    if not (0.0 <= _pct <= 100.0):
                        print(f"Error: percent must be 0-100 (got {_pct})", file=sys.stderr)
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
        _lock_path = os.path.join(os.path.dirname(get_ipc_socket_path()), "nativmix.lock")
        _lock_fh = open(_lock_path, "w", opener=lambda path, flags: os.open(path, flags | os.O_CLOEXEC))
        _is_primary = False
        try:
            _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _is_primary = True
            # Write PID so admins can identify the process if needed.
            _lock_fh.write(str(os.getpid()))
            _lock_fh.flush()
            # Keep _lock_fh open for the lifetime of main() so the OS lock
            # is held until the process exits.  Do NOT close it here.
        except OSError:
            _lock_fh.close()

        if not _is_primary:
            # Another instance holds the lock.  Wait up to 1 s for its IPC
            # socket to appear, then forward the CLI command and exit.
            _sock_path = IPC_SERVER_NAME
            msg = _build_ipc_msg(args)
            if args.toggle_mute is not None and args.toggle_mute < 1:
                print(f"Error: channel number must be ≥ 1 (got {args.toggle_mute})", file=sys.stderr)
                sys.exit(1)
            if args.vol is not None:
                try:
                    _ch = int(args.vol[0])
                    _pct = float(args.vol[1])
                except ValueError:
                    print("Error: --vol requires integer CHANNEL and numeric PERCENT", file=sys.stderr)
                    sys.exit(1)
                if _ch < 1:
                    print(f"Error: channel number must be ≥ 1 (got {_ch})", file=sys.stderr)
                    sys.exit(1)
                if not (0.0 <= _pct <= 100.0):
                    print(f"Error: percent must be 0-100 (got {_pct})", file=sys.stderr)
                    sys.exit(1)
            _forwarded = False
            for _attempt in range(10):  # 10 × 100 ms = 1 s max wait
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as _ipc:
                        _ipc.settimeout(0.5)
                        _ipc.connect(_sock_path)
                        _ipc.sendall(msg.encode("utf-8"))
                        if args.list_sinks or args.list_apps:
                            # Do NOT call shutdown(SHUT_WR) here.
                            # Sending a FIN causes Qt's QLocalSocket to transition
                            # to UnconnectedState, making write() silently fail
                            # before the server can send its response.
                            # The server calls disconnectFromServer() after writing,
                            # which sends the FIN — recv() returns b"" at that point.
                            _ipc.settimeout(5.0)
                            response = b""
                            try:
                                while True:
                                    chunk = _ipc.recv(4096)
                                    if not chunk:
                                        break
                                    response += chunk
                            except TimeoutError:
                                pass
                            print(response.decode("utf-8"))
                    _forwarded = True
                    break
                except (TimeoutError, FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.1)
            if _forwarded:
                logging.getLogger(__name__).info(
                    "Forwarded '%s' to running instance and exiting.", msg)
                sys.exit(0)
            # IPC socket never appeared — the other instance may have crashed
            # between acquiring the lock and creating the socket.  Try to take
            # over as the primary instance instead of silently failing.
            _opener = lambda path, flags: os.open(path, flags | os.O_CLOEXEC)  # noqa: E731
            _lock_fh2 = open(_lock_path, "w", opener=_opener)
            try:
                _fcntl.flock(_lock_fh2, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                _is_primary = True
                _lock_fh = _lock_fh2
                _lock_fh.write(str(os.getpid()))
                _lock_fh.flush()
                logging.getLogger(__name__).warning(
                    "Previous instance vanished before IPC was ready — taking over as primary.")
            except OSError:
                _lock_fh2.close()
                logging.getLogger(__name__).error(
                    "Another instance holds the lock and IPC is unreachable — giving up.")
                sys.exit(1)

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
            from PyQt6.QtGui import QColor, QPalette
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
        def backend_instance(cfg):
            return PipeWireManager(config=cfg)
    elif os_name == "Windows":
        from nativmix.audio.wasapi_manager import WasapiManager
        def backend_instance(cfg):
            return WasapiManager(config=cfg)
    else:
        raise RuntimeError(f"Unsupported platform: {os_name}")

    from nativmix.gui.main_window import MainWindow
    from nativmix.gui.tray_icon import TrayIcon
    from nativmix.hardware.arduino import ArduinoThread
    from nativmix.hardware.midi import MidiThread
    from nativmix.utils.config_manager import ConfigManager
    from nativmix.utils.profile_manager import ProfileManager
    from nativmix.utils.qt_utils import _slot_guard
    from nativmix.utils.sleep_watcher import SleepWatcher

    # ── One-time config directory migration (NativMix → nativmix) ──────
    migrate_legacy_config_dir()

    # ── Remove legacy user-level XDG autostart entry ─────────────────
    # Old versions created ~/.config/autostart/nativmix.desktop automatically.
    # Autostart is now opt-in (systemd user service). Remove the old file
    # silently so users don't end up with two instances running.
    try:
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

    # ── ProfileManager ──────────────────────────────────────────────────
    profile_manager = ProfileManager()

    # Load active profile into config on startup
    active_id = config.active_profile_id or ""
    profiles = profile_manager.list_profiles()
    if not profiles:
        active_id = profile_manager.create("Profile 1", channel_count=config.hw_channel_count)
    elif not active_id or active_id not in {p["id"] for p in profiles}:
        active_id = profiles[0]["id"]

    profile_manager.set_active_silently(active_id)  # no signal before GUI is ready
    config.active_profile_id = active_id
    startup_profile = profile_manager.load(active_id)
    config.apply_profile(startup_profile)

    # ── Audio backend ───────────────────────────────────────────────────
    backend = backend_instance(config)

    # ── Arduino thread ──────────────────────────────────────────────────
    arduino = ArduinoThread(
        port=config.hardware_port,
        num_channels=config.num_channels,
        inverted=config.invert_map,
        threshold=config.threshold,
        input_mode=config.input_mode,
        baud_rate=config.baud_rate,
        auto_search_device=config.auto_search_device,
    )

    # ── MIDI thread ─────────────────────────────────────────────────────
    midi = MidiThread(device_name=config.midi_device, input_mode=config.input_mode)
    midi.update_mappings(config.get_all_midi_mappings())
    midi.update_mute_mappings(config.get_all_midi_mute_mappings())

    # ── GUI ─────────────────────────────────────────────────────────────
    window = MainWindow(
        config=config,
        backend=backend,
        arduino_thread=arduino,
        midi_thread=midi,
        profile_manager=profile_manager,
    )

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
    def _on_midi_volumes_changed(mappings: list[tuple[int, float]]) -> None:
        try:
            for ch, vol in mappings:
                window.on_channel_volume_changed(ch, vol)
        except Exception:
            logger.exception("_on_midi_volumes_changed: unhandled exception")

    midi.midi_volumes_changed.connect(_on_midi_volumes_changed)
    # MIDI CC Received → Learn handshake
    midi.midi_cc_received.connect(window.on_midi_cc_received)
    # MIDI mute CC → toggle mute on the mapped channel
    midi.midi_mute_toggled.connect(backend.toggle_mute)

    # MIDI Connection state → UI Learn Reset
    midi.connection_changed.connect(window.on_midi_connection_changed)

    # Port selector → immediate reconnect on the chosen port
    window.settings_panel.port_changed.connect(
        lambda port: arduino.set_port(port if port else None)
    )
    # Arduino connected → mark port with ★ in the combo box
    def _on_arduino_connection_changed(connected: bool) -> None:
        try:
            logger.info("Arduino %s", "connected" if connected else "disconnected")
            window.settings_panel.mark_connected_port(arduino.current_port if connected else None)
        except Exception:
            logger.exception("_on_arduino_connection_changed: unhandled exception")

    arduino.connection_changed.connect(_on_arduino_connection_changed)

    def _push_midi_fader_feedback(
        mappings: list[tuple[int, float]] | None = None,
    ) -> None:
        if not config.midi_fader_feedback or config.input_mode == "usb":
            return
        targets = mappings if mappings is not None else config.get_midi_fader_feedback_targets()
        if targets:
            midi.request_fader_sync(targets)

    # Live-Update for inversion flags and threshold without restart
    def _on_settings_changed() -> None:
        arduino.reload_settings(config)
        midi.set_device(config.midi_device)
        midi.set_mode(config.input_mode)
        midi.update_mappings(config.get_all_midi_mappings())
        midi.update_mute_mappings(config.get_all_midi_mute_mappings())
        midi.set_profile_ccs(
            config.profile_midi_next_cc,
            config.profile_midi_prev_cc,
            profile_manager.direct_cc_map,
        )
        midi.set_fader_feedback_enabled(config.midi_fader_feedback)
        _push_midi_fader_feedback()

    config.settings_changed.connect(_on_settings_changed)
    _on_settings_changed()  # initialize MIDI CCs and Arduino from startup profile

    def _on_channel_volume_midi_feedback(channel_index: int, volume: float) -> None:
        if config.get_midi_cc(channel_index) is not None:
            _push_midi_fader_feedback([(channel_index, volume)])

    backend.channel_volume_changed.connect(_on_channel_volume_midi_feedback)
    window.fader_display_synced.connect(lambda: _push_midi_fader_feedback())

    def _switch_profile(target: str) -> None:
        """Handle profile switch from IPC, MIDI, or GUI."""
        try:
            # Persist current hardware volumes to the outgoing profile file so that
            # "Load fader positions on switch" has up-to-date values to restore.
            outgoing_id = profile_manager.active_profile_id
            if outgoing_id:
                try:
                    outgoing = profile_manager.load(outgoing_id)
                    hw_vols = arduino.get_last_volumes()
                    hw_idx = 0
                    for ch in outgoing.get("channels", []):
                        if not ch.get("is_midi", False):
                            if hw_idx < len(hw_vols) and hw_vols[hw_idx] >= 0.0:
                                ch["volume"] = hw_vols[hw_idx]
                            hw_idx += 1
                    profile_manager.save_profile(outgoing)
                except Exception:
                    logger.warning(
                        "Could not persist volumes to outgoing profile %s",
                        outgoing_id,
                        exc_info=True,
                    )

            if target == "next":
                profile_manager.switch_next()
            elif target == "prev":
                profile_manager.switch_prev()
            else:
                # Match by name (case-insensitive) or by ID
                match_id = None
                for p in profile_manager.list_profiles():
                    if p["name"].lower() == target.lower() or p["id"] == target:
                        match_id = p["id"]
                        break
                if match_id is None:
                    logger.warning("Profile not found: %r", target)
                    return
                profile_manager.switch(match_id)
            # No-op: switch_next/prev returns early on single profile, switch()
            # returns early if already on the requested profile → nothing to do.
            if profile_manager.active_profile_id == outgoing_id:
                return
            profile = profile_manager.active_profile
            config.active_profile_id = profile_manager.active_profile_id
            config.apply_profile(profile)
            config.save()
            # Always clear any stale takeover from the previous profile first.
            # Without this, switching away from a restore-enabled profile leaves
            # old takeover keys in place, blocking all subsequent Arduino input.
            arduino.set_takeover_pending({})
            if profile.get("restore_fader_positions"):
                channels = profile.get("channels", [])
                vols = [ch.get("volume", 1.0) for ch in channels]
                backend.apply_poti_volumes(vols)
                window.on_volumes_changed(vols)
                arduino.set_takeover_pending({
                    i: ch.get("volume", 1.0)
                    for i, ch in enumerate(channels)
                    if not ch.get("is_midi", False)
                })
                _push_midi_fader_feedback()
            elif arduino.has_real_data:
                # No restore: immediately push current hardware positions to the
                # new profile's apps. Without this, apps only update on the next
                # fader movement — if faders are stationary, changed=False means
                # volumes_changed never fires and the new profile stays silent.
                hw_vols = arduino.get_last_volumes()
                backend.apply_poti_volumes(hw_vols)
                window.on_volumes_changed(hw_vols)
                _push_midi_fader_feedback()
        except Exception:
            logger.exception("_switch_profile: error switching to %r", target)

    def _update_profile_settings_ui(profile_id: str) -> None:
        try:
            p = profile_manager.load(profile_id)
            can_delete = len(profile_manager.list_profiles()) > 1
            window.settings_panel.update_profile_ui(p, can_delete)
            window.settings_panel.update_profile_midi_ccs(
                config.profile_midi_next_cc,
                config.profile_midi_prev_cc,
            )
        except Exception:
            logger.debug("Could not update profile settings UI for %s", profile_id, exc_info=True)

    profile_manager.profile_changed.connect(_update_profile_settings_ui)
    profile_manager.profile_list_changed.connect(
        lambda: _update_profile_settings_ui(profile_manager.active_profile_id)
    )
    # Initialize UI from startup profile
    if active_id:
        _update_profile_settings_ui(active_id)

    _profile_cc_learn_target: str | None = None

    def _on_profile_cc_learn_started(target: str) -> None:
        nonlocal _profile_cc_learn_target
        # Second click on the same learn button = cancel
        if _profile_cc_learn_target == target:
            _profile_cc_learn_target = None
            window.settings_panel.reset_cc_learn_buttons()
        else:
            _profile_cc_learn_target = target

    def _on_midi_cc_for_profile_learn(cc: int, val: int) -> None:
        nonlocal _profile_cc_learn_target
        try:
            if _profile_cc_learn_target is None or val != 127:
                return
            target = _profile_cc_learn_target
            _profile_cc_learn_target = None
            if target == "next":
                config.profile_midi_next_cc = cc
            elif target == "prev":
                config.profile_midi_prev_cc = cc
            elif target == "direct":
                curr_id = profile_manager.active_profile_id
                if curr_id:
                    try:
                        p = profile_manager.load(curr_id)
                        p["midi_switch_cc"] = cc
                        profile_manager.save_profile(p)
                    except Exception:
                        logger.exception("Error setting direct profile CC")
            logger.info("Profile MIDI CC learned: target=%r cc=%d", target, cc)
            config.settings_changed.emit()
            _update_profile_settings_ui(profile_manager.active_profile_id)
            window.settings_panel.reset_cc_learn_buttons()
        except Exception:
            logger.exception("_on_midi_cc_for_profile_learn: unhandled exception")

    window.settings_panel.profile_cc_learn_started.connect(_on_profile_cc_learn_started)
    midi.midi_cc_received.connect(_on_midi_cc_for_profile_learn)

    def _on_delete_profile_requested(profile_id: str) -> None:
        try:
            profile_manager.delete(profile_id)
            # Switch to the first remaining profile
            remaining = profile_manager.list_profiles()
            if remaining:
                _switch_profile(remaining[0]["id"])
        except ValueError as exc:
            logger.warning("Cannot delete profile %r: %s", profile_id, exc)
        except Exception:
            logger.exception("_on_delete_profile_requested: error deleting %r", profile_id)

    window.settings_panel.delete_profile_requested.connect(_on_delete_profile_requested)
    window.settings_panel.save_profile_requested.connect(
        lambda: profile_manager.save_current(config.all_channels())
    )

    def _on_restore_fader_positions_changed(enabled: bool) -> None:
        if not enabled:
            return
        # When "Load fader positions on switch" is turned on, capture the current
        # hardware volumes into the profile channels immediately — otherwise the
        # saved values stay at 1.0 and nothing meaningful is restored on switch.
        active_id = profile_manager.active_profile_id
        if not active_id:
            return
        try:
            hw_vols = arduino.get_last_volumes() if arduino.has_real_data else None
            if hw_vols is None:
                return
            profile = profile_manager.load(active_id)
            channels = profile.get("channels", [])
            for pos, ch in enumerate(channels):
                if not ch.get("is_midi", False) and pos < len(hw_vols):
                    ch["volume"] = hw_vols[pos]
            profile_manager.save_profile(profile)
            logger.debug("Saved current fader positions to profile %s on restore enable", active_id)
        except Exception:
            logger.exception("_on_restore_fader_positions_changed: error saving fader positions")

    window.settings_panel.restore_fader_positions_changed.connect(
        _on_restore_fader_positions_changed
    )

    def _on_channel_changed() -> None:
        profile_manager.save_current(config.all_channels())

    def _on_channel_count_changed(n: int) -> None:
        old_id = profile_manager.active_profile_id
        profile_manager.ensure_profile_for_hw(n)
        if profile_manager.active_profile_id != old_id:
            # New profile was auto-created and activated — apply it to config
            config.active_profile_id = profile_manager.active_profile_id
            config.apply_profile(profile_manager.active_profile)
            config.save()
        window.on_channel_count_changed(n)

    # Dynamic channel count → profile ensure + GUI rebuild
    arduino.channel_count_changed.connect(_on_channel_count_changed)

    # MIDI Status display
    midi.status_changed.connect(window.settings_panel.set_midi_status)
    # MIDI Restart (Panic)
    window.settings_panel.midi_panic_triggered.connect(midi.restart_midi)
    # Initialize arduino settings immediately so the curve is applied on startup
    arduino.reload_settings(config)
    # Routing update: when the GUI changes a channel mapping, the backend must
    # immediately move the affected audio stream to/from the V-Sink.
    config.mapping_changed.connect(backend.on_mapping_changed)
    # Auto-save channel changes to the active profile
    config.mapping_changed.connect(lambda *_: _on_channel_changed())

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
            window.set_show_requested(True)
            window.show()
            window.raise_()
            # Do NOT call requestActivate() at startup: newly shown windows
            # receive focus from the compositor automatically.  On COSMIC,
            # xdg_activation_v1 triggers a dock bounce/flicker animation.
            # requestActivate() is kept in tray._show_window() and
            # _ipc_show_window() where the user explicitly requests focus.
            QTimer.singleShot(500, lambda: window.set_show_requested(False))
        QTimer.singleShot(350, lambda: _push_midi_fader_feedback())

    coordinator.ready.connect(on_app_ready)

    # ── Start background threads (AFTER CONNECTING SIGNALS) ─────────────
    backend.start()
    arduino.start()
    midi.start()

    # Close Arduino serial before suspend so xHCI is not held busy; reconnect after.
    sleep_watcher = SleepWatcher(parent=app)

    @_slot_guard
    def _on_preparing_for_sleep() -> None:
        arduino.prepare_for_sleep()

    @_slot_guard
    def _on_resumed_from_sleep() -> None:
        arduino.resume_from_sleep()

    sleep_watcher.preparing_for_sleep.connect(_on_preparing_for_sleep)
    sleep_watcher.resumed_from_sleep.connect(_on_resumed_from_sleep)
    sleep_watcher.start()

    # ── IPC Server ──
    ipc_server = IpcServer(parent=app)
    ipc_server.toggle_mute_requested.connect(backend.toggle_mute)

    def _on_set_volume_requested(channel_idx: int, value: float) -> None:
        try:
            num = config.num_channels
            if not (0 <= channel_idx < num):
                logger.warning("--vol: channel %d out of range (0-%d)", channel_idx, num - 1)
                return
            # Build full volume list from current config, override the target channel
            vols = [config.get_channel_volume(i) for i in range(num)]
            vols[channel_idx] = value
            backend.apply_poti_volumes(vols)
            window.on_volumes_changed(vols)
            # Only add takeover for hardware channels; MIDI channels have no physical fader
            channels = config.all_channels()
            if channel_idx < len(channels) and not channels[channel_idx].get("is_midi", False):
                arduino.set_channel_takeover(channel_idx, value)
            profile_manager.save_current(config.all_channels())
            logger.info("IPC --vol: channel %d set to %.1f%%", channel_idx + 1, value * 100)
            _push_midi_fader_feedback([(channel_idx, value)])
        except Exception:
            logger.exception("_on_set_volume_requested: unhandled exception")

    ipc_server.set_volume_requested.connect(_on_set_volume_requested)
    ipc_server.profile_switch_requested.connect(_switch_profile)
    midi.profile_switch_requested.connect(_switch_profile)
    window.profile_switch_requested.connect(_switch_profile)

    def handle_list_sinks(socket):
        data = backend.get_v_sinks_debug()
        payload = json.dumps(data, indent=2).encode("utf-8")
        logger.debug("IPC handle_list_sinks: sending %d bytes", len(payload))
        socket.write(payload)
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()

    def handle_list_apps(socket):
        data = backend.get_active_streams_debug()
        payload = json.dumps(data, indent=2).encode("utf-8")
        logger.debug("IPC handle_list_apps: sending %d bytes", len(payload))
        socket.write(payload)
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()

    ipc_server.list_sinks_requested.connect(handle_list_sinks)
    ipc_server.list_apps_requested.connect(handle_list_apps)

    # "show" IPC command: bring the existing window to the foreground.
    # Uses QWindow.requestActivate() on Wayland (xdg_activation_v1, Qt 6.5+)
    # rather than QWidget.activateWindow(), which compositors ignore.
    def _ipc_show_window() -> None:
        window.set_show_requested(True)
        window.showNormal()
        window.raise_()
        handle = window.windowHandle()
        if handle is not None:
            handle.requestActivate()
        else:
            window.activateWindow()
        QTimer.singleShot(500, lambda: window.set_show_requested(False))

    ipc_server.show_window_requested.connect(_ipc_show_window)

    # ── Restart support ───────────────────────────────────────────────
    _do_restart = False

    def _on_restart_requested() -> None:
        nonlocal _do_restart
        logger.info("Restart requested via IPC — shutting down for restart")
        _do_restart = True
        QApplication.quit()

    ipc_server.restart_requested.connect(_on_restart_requested)
    tray.restart_requested.connect(_on_restart_requested)

    # ── Auto-restart after update ─────────────────────────────────────
    # Check every 60 s whether the installed package version has changed.
    # Only triggers when NativMix is installed (importlib.metadata works);
    # silently skipped in dev/editable mode or if the package is undetectable.
    from nativmix.metadata import __version__ as _running_version

    def _check_for_update() -> None:
        nonlocal _do_restart
        try:
            # Clear the path-finder cache so we read from disk, not the
            # in-process cache that was built when the old package was current.
            importlib.metadata.MetadataPathFinder.invalidate_caches()
            installed = importlib.metadata.version("nativmix")
            if installed != _running_version:
                logger.info(
                    "Update detected (%s → %s) — restarting", _running_version, installed
                )
                _do_restart = True
                QApplication.quit()
        except Exception as exc:
            logger.debug("Update check skipped: %s", exc)

    _update_check_timer = QTimer(app)
    _update_check_timer.setInterval(60_000)
    _update_check_timer.timeout.connect(_check_for_update)
    _update_check_timer.start()

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
            except Exception as exc:
                logger.debug("Signal wakeup_fd read error: %s", exc)

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
        _sigint_timer = QTimer(app)
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
    def _force_exit():
        logger.warning("Graceful shutdown timed out — forcing exit")
        os._exit(0)

    _exit_watchdog = threading.Timer(8.0, _force_exit)
    _exit_watchdog.daemon = True
    _exit_watchdog.start()

    sleep_watcher.stop()
    arduino.stop()
    midi.stop()
    backend.stop()

    _exit_watchdog.cancel()

    if _do_restart:
        # Systemd service: delegate restart so the new process starts in the correct cgroup.
        if is_systemd_service():
            import subprocess
            logger.info("Restarting via systemctl (systemd service detected)")
            try:
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                logger.warning("daemon-reload timed out — skipping")
            # Exit with code 1 so systemd's Restart=on-failure triggers the restart.
            # Popen fire-and-forget is unreliable: systemd kills cgroup children
            # when the main process exits before the subprocess delivers its command.
            sys.exit(1)
        logger.info("Performing full process restart via os.execv")
        os.execv(sys.executable, [sys.executable] + sys.argv)

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

        print("\nCRITICAL: NativMix crashed during startup.")
        print(f"A detailed crash report has been saved to: {crash_log}")
        sys.exit(1)
