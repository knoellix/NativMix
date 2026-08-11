"""
Arduino hardware backend for NativMix.

Reads analog potentiometer values (0–1023) from an Arduino via a serial USB
connection and maps them to linear volume values (0.0–1.0).

Rules implemented
-----------------
Rule 7  – Uses pyserial; /dev/ttyUSB0 or /dev/ttyACM0 are auto-detected.
Rule 8  – Moving-average smoothing; cubic (perceptual) volume mapping;
           per-channel inversion option.
Rule 9  – Runs in a QThread; communicates via pyqtSignal only.
Rule 10 – Catches SerialException; reconnection loop for hot-plug support.
Rule 13 – Starts without an Arduino (headless / dummy mode) so CI builds pass.

Expected Arduino serial format
-------------------------------
NativMix handles volume control via Raw Serial (via USB). The hardware (Arduino/ESP)
acts as a Composite Device:
1. HID Keyboard for macros (handled by the OS directly).
2. Raw Serial for volume (handled by this thread).

This bypasses System Volume OSD popups while maintaining HID macro functionality.
The expected format is a newline-terminated, pipe-separated list of raw ADC
values (0–1023), one line per update cycle:

    512|0|1023|256\n      ← four channels

This is compatible with the original deej firmware format.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Sequence

import serial
import serial.tools.list_ports
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Raw ADC range from the Arduino (10-bit ADC → 0–1023)
ADC_MIN: int = 0
ADC_MAX: int = 1023

#: How many samples to keep for the moving-average smoother per channel
SMOOTHING_WINDOW: int = 5

#: Minimum volume delta that triggers a signal emit (1 % of full scale)
#: Tuned for PipeWire on Arch: suppresses noise without sacrificing responsiveness.
VOLUME_THRESHOLD: float = 0.01

#: Seconds to wait between reconnection attempts when the Arduino is missing
RECONNECT_INTERVAL: float = 2.0

#: Serial baud rate – must match the Arduino sketch
BAUD_RATE: int = 9600

#: Read timeout in seconds (keeps the thread responsive to stop() calls)
READ_TIMEOUT: float = 0.5

#: Candidate device paths tried in order during auto-detection
DEFAULT_PORTS: tuple[str, ...] = ("/dev/ttyACM0", "/dev/ttyUSB0")


# ---------------------------------------------------------------------------
# Volume mapping helpers
# ---------------------------------------------------------------------------

def _power_map(raw: int, inverted: bool = False, exponent: float = 2.0) -> float:
    """
    Map a raw ADC value (0–1023) to a perceptual volume in [0.0, 1.0].

    A power-law curve is applied so that the physical position of the
    potentiometer feels natural to the human ear.  The exponent is
    configurable (1.0 = linear, 2.0 = quadratic default, 3.0 = cubic).

    Args:
        raw:      Raw ADC reading (0–1023).
        inverted: If True, the mapping is flipped (0 ADC → 1.0 volume).
        exponent: Power-law exponent; controlled by the settings slider.

    Returns:
        Perceptual volume in [0.0, 1.0].
    """
    clamped = max(ADC_MIN, min(ADC_MAX, raw))
    normalised = clamped / ADC_MAX

    if inverted:
        normalised = 1.0 - normalised

    # Power-law mapping: f(x) = x^n
    val = normalised ** exponent

    # Snap to hard limits (avoids "never quite reaches 100%" at high exponents)
    if val > 0.99:
        return 1.0
    if val < 0.005:
        return 0.0

    return val



# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------

class _ChannelState:
    """Tracks the smoothing window and last value for one potentiometer channel."""

    def __init__(
        self,
        inverted: bool = False,
        threshold: float = VOLUME_THRESHOLD,
        exponent: float = 2.0,
    ) -> None:
        self.inverted: bool = inverted
        self.threshold: float = threshold
        self.exponent: float = exponent
        self._window: deque[int] = deque(maxlen=SMOOTHING_WINDOW)
        self._last_volume: float = -1.0  # sentinel: triggers first emit

    def update(self, raw: int) -> float | None:
        """
        Add a new raw sample to the smoothing window and return the mapped
        volume, or None if the value has not changed (to avoid redundant signals).
        """
        self._window.append(raw)
        smoothed_raw = int(sum(self._window) / len(self._window))
        volume = _power_map(smoothed_raw, self.inverted, self.exponent)

        if abs(volume - self._last_volume) < self.threshold:
            return None

        self._last_volume = volume
        return volume


# ---------------------------------------------------------------------------
# Arduino QThread
# ---------------------------------------------------------------------------

class ArduinoThread(QThread):
    """
    Background thread that reads potentiometer values from an Arduino over USB.

    Signals
    -------
    volumes_changed(list[float])
        Emitted whenever at least one channel volume changes.
        The list contains one float per channel in [0.0, 1.0].
    connection_changed(bool)
        Emitted when the Arduino connects (True) or disconnects (False).
    """

    volumes_changed = pyqtSignal(list)   # list[float] – one entry per channel
    connection_changed = pyqtSignal(bool)
    channel_count_changed = pyqtSignal(int)  # fired when Arduino reports a different channel count

    _BACKOFF_BASE: float = 2.0
    _BACKOFF_MAX: float = 60.0

    def __init__(
        self,
        port: str | None = None,
        num_channels: int = 5,
        inverted: Sequence[bool] | None = None,
        threshold: float = VOLUME_THRESHOLD,
        input_mode: str = "usb",
        baud_rate: int = BAUD_RATE,
        auto_search_device: bool = True,
        parent=None,
    ) -> None:
        """
        Args:
            port:         Serial device path, e.g. '/dev/ttyACM0'. If None,
                          the thread tries DEFAULT_PORTS and then auto-detects.
            num_channels: Number of potentiometer channels to expect.
            inverted:     Per-channel inversion flags. Must have length
                          ``num_channels`` if provided. Defaults to all False.
            threshold:    Minimum volume delta [0.0–1.0] that triggers a signal
                          emit. Loaded from ConfigManager at start-up.
            input_mode:   "usb", "hybrid", or "midi_only" for input mode.
            baud_rate:    Serial baud rate (default 9600).
            auto_search_device: If False, use only the configured port (disable auto-discovery).
                          If True, try auto-detection even with a port configured.
            parent:       Optional Qt parent object.
        """
        super().__init__(parent)
        self._port: str | None = port
        self._num_channels: int = num_channels
        self._threshold: float = threshold
        self._baud_rate: int = baud_rate
        self._auto_search_device: bool = auto_search_device
        self._running: bool = False
        self._reconnect_requested: bool = False
        # True while logind PrepareForSleep(true)..false — keep serial closed
        # so the USB/xHCI controller can suspend (see SleepWatcher).
        self._system_sleeping: bool = False
        self._connected_port: str | None = None  # last successfully connected port
        self._failed_attempts: int = 0
        self._error_notified: bool = False
        self._input_mode: str = input_mode
        self._reconnect_attempts: int = 0

        # Normalize inversion flags to exactly num_channels entries.
        # Pad with False or trim silently – avoids a crash when the config
        # was saved with a different channel count than what the Arduino sends.
        raw_flags: list[bool] = list(inverted) if inverted is not None else []
        if len(raw_flags) < num_channels:
            raw_flags += [False] * (num_channels - len(raw_flags))
        self._inv_flags: list[bool] = raw_flags[:num_channels]
        self._channels: list[_ChannelState] = [
            _ChannelState(inverted=flag, threshold=threshold) for flag in self._inv_flags
        ]
        self._channels_lock = threading.Lock()  # guards _channels against reload_settings() race
        # Current open serial port — closed by stop() to unblock readline() immediately.
        self._active_ser: serial.Serial | None = None
        # Stability counter: channel_count_changed fires only after 3 consecutive
        # frames with the same count — prevents transient post-reconnect garbage
        # from corrupting the GUI and config.
        self._pending_count: int = num_channels
        self._pending_count_stable: int = 0
        # Fader takeover: channels mapped to their profile-loaded volume.
        # Hardware input is suppressed per channel until the first movement is detected.
        self._takeover_pending: dict[int, float] = {}
        self._takeover_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_inverted(self, channel: int, inverted: bool) -> None:
        """
        Flip the inversion flag for a single channel at runtime.

        This is called from the GUI when the user toggles the Invert checkbox.
        Python bool assignment is atomic, so no lock is needed.

        Effect on volume math (see _power_map):
          inverted=False:  vol = (raw / 1023) ** 3           (0 ADC → quiet)
          inverted=True:   vol = (1.0 - raw / 1023) ** 3    (0 ADC → loud)
        """
        if 0 <= channel < len(self._channels):
            self._channels[channel].inverted = inverted
            self._inv_flags[channel] = inverted
        else:
            logger.warning("set_inverted: channel %d out of range", channel)

    def reload_settings(self, config) -> None:
        """
        Reload global settings from a ConfigManager instance.

        Called safely from the main thread when ConfigManager.settings_changed fires.
        Syncs threshold, all inversion flags, the volume exponent, and auto-search settings.
        """
        old_mode = self._input_mode
        old_baud = self._baud_rate
        old_auto_search = self._auto_search_device
        self._threshold = config.threshold
        self._input_mode = config.input_mode
        self._baud_rate = config.baud_rate
        self._auto_search_device = config.auto_search_device

        # If mode, baud rate, or auto-search setting changed, force reconnect to re-open the serial port
        if old_mode != self._input_mode:
            self._reconnect_requested = True
            logger.debug("ArduinoThread: Mode changed (%s -> %s), re-evaluating session", old_mode, self._input_mode)
        elif old_baud != self._baud_rate:
            self._reconnect_requested = True
            logger.debug("ArduinoThread: Baud rate changed (%d -> %d), reconnecting", old_baud, self._baud_rate)
        elif old_auto_search != self._auto_search_device:
            self._reconnect_requested = True
            logger.debug(
                "ArduinoThread: Auto-search changed (%s -> %s), reconnecting",
                old_auto_search,
                self._auto_search_device,
            )

        exponent = config.get_volume_exponent()
        with self._channels_lock:
            for i, ch in enumerate(self._channels):
                ch.threshold = self._threshold
                ch.exponent = exponent
                inv = config.get_effective_inversion(i)
                ch.inverted = inv
                self._inv_flags[i] = inv
        logger.debug("Volume curve exponent updated to: %.2f", exponent)
        logger.debug("ArduinoThread settings reloaded from config")

    def set_port(self, port: str | None) -> None:
        """
        Switch to a different serial port at runtime.

        Safe to call from the main thread while the ArduinoThread is running.
        The current serial session is interrupted and a new one starts on
        the requested port within one read-timeout cycle (~0.5 s).

        Args:
            port: Device path (e.g. '/dev/ttyACM0') or None for auto-detect.
        """
        self._port = port
        self._reconnect_requested = True
        logger.debug("Port switch requested: %s", port or "auto")

    def set_auto_search_device(self, enable: bool) -> None:
        """
        Enable or disable automatic device discovery.

        If disabled (False) and a port is configured, only that port will be used.
        If enabled (True), auto-discovery proceeds even with a configured port.

        Args:
            enable: True to enable auto-discovery, False to disable it.
        """
        self._auto_search_device = enable
        # Force reconnection to re-evaluate port selection
        self._reconnect_requested = True
        logger.debug("Auto-search device: %s", "enabled" if enable else "disabled")

    def set_takeover_pending(self, channel_volumes: dict[int, float]) -> None:
        """
        Replace the full takeover dict. Hardware input is suppressed per channel
        until the first movement beyond threshold.

        Args:
            channel_volumes: mapping of channel index → target volume (0.0–1.0).

        Safe to call from the main thread while the ArduinoThread is running.
        """
        with self._takeover_lock:
            self._takeover_pending = dict(channel_volumes)
        logger.debug("Takeover pending for channels: %s", list(channel_volumes.keys()))

    def set_channel_takeover(self, channel: int, volume: float) -> None:
        """
        Add or update a single channel in the takeover dict without touching
        the other entries. Use this when an external command (e.g. --vol IPC)
        sets one channel's volume and hardware should follow on next movement.

        Safe to call from the main thread while the ArduinoThread is running.
        """
        with self._takeover_lock:
            self._takeover_pending[channel] = volume
        logger.debug("Takeover set for channel %d: %.2f", channel, volume)

    @property
    def current_port(self) -> str | None:
        """The device path of the last successfully opened serial port."""
        return self._connected_port

    @property
    def has_real_data(self) -> bool:
        """True once at least one real hardware reading has been received."""
        return any(ch._last_volume >= 0 for ch in self._channels)

    def get_last_volumes(self) -> list[float]:
        """Return the current smoothed volume for all hardware channels."""
        vols = []
        for ch in self._channels:
            # Handle the -1.0 sentinel (no data yet) by returning 1.0
            val = ch._last_volume
            vols.append(val if val >= 0 else 1.0)
        return vols

    def prepare_for_sleep(self) -> None:
        """
        Release the USB serial port before system suspend.

        Safe to call from the GUI/main thread.  Keeps the worker from
        reopening the port until ``resume_from_sleep()`` so xHCI can idle.
        """
        logger.info("ArduinoThread: prepare_for_sleep — closing serial")
        self._system_sleeping = True
        self._close_active_serial()

    def resume_from_sleep(self) -> None:
        """Allow serial reconnect after system resume. Safe from main thread."""
        logger.info("ArduinoThread: resume_from_sleep — reconnect allowed")
        self._system_sleeping = False

    def _close_active_serial(self) -> None:
        """Close the open serial handle to unblock readline() / free USB."""
        _ser = self._active_ser
        if _ser is None:
            return
        try:
            _ser.close()
        except Exception as exc:
            logger.debug("ArduinoThread: close active serial: %s", exc)

    def stop(self) -> None:
        """Signal the thread to exit and wait for it to finish (with timeout)."""
        self._running = False
        self._system_sleeping = False
        # Close the active serial port to unblock readline() immediately,
        # avoiding the need to wait for READ_TIMEOUT (0.5 s) or call terminate().
        self._close_active_serial()
        if not self.wait(2000):
            logger.warning("ArduinoThread did not stop in time, terminating...")
            self.terminate()
            # Strategy B: bounded wait after terminate() so a thread stuck in a
            # C extension (e.g. pyserial during USB teardown) cannot block forever.
            if not self.wait(1000):
                logger.error("ArduinoThread still alive after terminate — abandoning")

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop: connect → read → reconnect on failure."""
        self._running = True
        logger.info("ArduinoThread started (channels=%d)", self._num_channels)

        while self._running:
            try:
                if self._system_sleeping:
                    self._wait_while_sleeping()
                    continue

                if self._input_mode == "midi_only":
                    # Pause Arduino polling entirely
                    self._wait_or_stop(RECONNECT_INTERVAL)
                    continue

                port = self._resolve_port()

                if port is None:
                    # No Arduino found – log once and retry with exponential backoff
                    self._handle_connection_failure()
                    wait = min(self._BACKOFF_BASE * (2 ** self._reconnect_attempts), self._BACKOFF_MAX)
                    self._reconnect_attempts += 1
                    logger.debug("No Arduino found, retrying in %.0fs (attempt %d) …", wait, self._reconnect_attempts)
                    self._wait_or_stop(wait)
                    continue

                self._run_session(port)

            except Exception:
                logger.exception("Unexpected ArduinoThread error — retrying in %.0fs", RECONNECT_INTERVAL)
                self.connection_changed.emit(False)
                self._wait_or_stop(RECONNECT_INTERVAL)

        logger.info("ArduinoThread stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_connection_failure(self) -> None:
        self._failed_attempts += 1
        if self._failed_attempts >= 6 and not self._error_notified:
            try:
                subprocess.run(
                    ["notify-send", "NativMix",
                     "Arduino not found. NativMix will continue to search for the device in the background."],
                    check=False,
                    timeout=5,
                )
            except Exception:
                pass  # Notification is best-effort; never block the Arduino thread
            self._error_notified = True

    def _handle_connection_success(self) -> None:
        self._failed_attempts = 0
        self._error_notified = False
        self._reconnect_attempts = 0

    def _resolve_port(self) -> str | None:
        """
        Determine which serial port to use.

        Order of preference when auto_search_device is True (auto-discovery enabled):
        1. User-configured port (self._port), if the device file exists.
        2. Ports from DEFAULT_PORTS that exist on the filesystem.
        3. First USB-serial port detected by pyserial's list_ports.

        When auto_search_device is False (auto-discovery disabled):
        1. ONLY the user-configured port (self._port) if it exists.
        2. None if the port doesn't exist or is None.

        Returns:
            Device path string, or None if nothing is found.
        """
        # If auto-search is disabled and a port is configured, use only that port
        if not self._auto_search_device:
            if self._port:
                if os.path.exists(self._port):
                    logger.info("Using configured port: %s (auto-discovery disabled)", self._port)
                    return self._port
                else:
                    logger.warning(
                        "Configured port %s does not exist and auto-discovery is disabled",
                        self._port
                    )
                    return None
            else:
                logger.debug("No port configured and auto-discovery is disabled")
                return None

        # Auto-discovery enabled: proceed with standard port resolution
        # 1) User-configured port
        if self._port and os.path.exists(self._port):
            return self._port

        # 2) Try well-known default paths
        for candidate in DEFAULT_PORTS:
            if os.path.exists(candidate):
                logger.info("Auto-detected Arduino on %s", candidate)
                return candidate

        # 3) Fallback: pyserial port enumeration (looks for USB-serial adapters)
        for port_info in serial.tools.list_ports.comports():
            if "USB" in (port_info.description or "").upper() or \
               "ACM" in (port_info.device or "").upper():
                logger.info("Detected USB-serial device: %s", port_info.device)
                return port_info.device

        return None

    def _run_session(self, port: str) -> None:
        """
        Open the serial port and read data until an error or stop request.

        Args:
            port: Device path of the Arduino serial port.
        """
        logger.info("Connecting to Arduino on %s @ %d baud …", port, self._baud_rate)
        try:
            with serial.Serial(port, baudrate=self._baud_rate, timeout=READ_TIMEOUT) as ser:
                self._active_ser = ser
                logger.info("Arduino connected on %s", port)
                self._connected_port = port
                self._handle_connection_success()
                self.connection_changed.emit(True)

                # Flush any stale bytes left in the buffer
                ser.reset_input_buffer()

                while (
                    self._running
                    and not self._reconnect_requested
                    and not self._system_sleeping
                ):
                    self._read_line(ser)

                if self._system_sleeping:
                    logger.info("ArduinoThread: session ended for system sleep")
                    self.connection_changed.emit(False)
                elif self._reconnect_requested:
                    self._reconnect_requested = False
                    logger.info("Reconnecting to new port: %s", self._port or "auto")
                    self.connection_changed.emit(False)

        except serial.SerialException as exc:
            # Device disconnected, closed for sleep, or unavailable
            if self._system_sleeping:
                logger.debug("Serial closed for sleep on %s: %s", port, exc)
                self.connection_changed.emit(False)
            else:
                logger.warning("Serial error on %s: %s", port, exc)
                self._handle_connection_failure()
                self.connection_changed.emit(False)
                # Brief pause so we don't spin-lock if the device keeps failing
                self._wait_or_stop(RECONNECT_INTERVAL)

        except OSError as exc:
            # Covers permission errors, missing device nodes, etc.
            if self._system_sleeping:
                logger.debug("OS error during sleep close on %s: %s", port, exc)
                self.connection_changed.emit(False)
            else:
                logger.error("OS error on %s: %s", port, exc)
                self._handle_connection_failure()
                self.connection_changed.emit(False)
                self._wait_or_stop(RECONNECT_INTERVAL)

        finally:
            self._active_ser = None

    def _read_line(self, ser: serial.Serial) -> None:
        """
        Read exactly one line from the serial port and process it.

        The expected format is:  ``512|0|1023|256\n``

        A read timeout is set on the Serial object so this call returns
        regularly even when no data arrives, allowing the while-loop to check
        self._running without blocking indefinitely.

        Args:
            ser: An open, configured Serial instance.
        """
        try:
            raw_bytes = ser.readline()
        except serial.SerialException as exc:
            # Re-raise so _run_session can handle reconnect logic
            raise exc

        if not raw_bytes:
            # Timeout expired – no data received, loop again
            return

        try:
            line = raw_bytes.decode("ascii", errors="replace").strip()
        except Exception as exc:
            logger.debug("Decode error: %s", exc)
            return

        if not line:
            return

        self._process_line(line)

    def _process_line(self, line: str) -> None:
        """
        Parse a pipe-separated line of ADC values and emit volumes_changed.

        Automatically adapts to whatever channel count the Arduino sends.
        On first packet or when the count changes, channel_count_changed is
        emitted so the GUI can rebuild its layout.
        """
        parts = line.split("|")
        n = len(parts)

        if n == 0:
            return

        # Parse all values first — a malformed frame (e.g. post-reconnect
        # garbage like "4\xef") must not trigger channel_count_changed before
        # we know the data is actually valid.
        raw_values: list[int] = []
        for i, part in enumerate(parts):
            try:
                raw_values.append(int(part.strip()))
            except ValueError:
                logger.debug("Non-integer value in channel %d: %r — frame discarded", i, part)
                return  # discard the entire malformed frame, channel count unchanged

        # Only update channel count after 3 consecutive clean frames with the same
        # count — single-frame outliers (e.g. partial frames after reconnect) are
        # silently discarded so transient oscillations never corrupt the config.
        if n != self._num_channels:
            if n == self._pending_count:
                self._pending_count_stable += 1
            else:
                self._pending_count = n
                self._pending_count_stable = 1
            if self._pending_count_stable >= 3:
                logger.info("Channel count changed: %d → %d", self._num_channels, n)
                self._num_channels = n
                self._adapt_channels(n)
                self.channel_count_changed.emit(n)
                self._pending_count_stable = 0
            else:
                logger.debug(
                    "Ignoring transient channel count %d (expected %d, stable=%d/3)",
                    n,
                    self._num_channels,
                    self._pending_count_stable,
                )
                return
        else:
            self._pending_count = n
            self._pending_count_stable = 0

        changed = False
        current_volumes: list[float] = []

        with self._channels_lock:
            with self._takeover_lock:
                takeover = dict(self._takeover_pending)  # snapshot: {ch_idx: profile_vol}

            for i, raw in enumerate(raw_values):
                new_vol = self._channels[i].update(raw)

                if i in takeover:
                    if new_vol is not None:
                        # First movement detected — release takeover for this channel
                        with self._takeover_lock:
                            self._takeover_pending.pop(i, None)
                        logger.debug("Takeover released for channel %d", i)
                        changed = True
                        current_volumes.append(new_vol)
                    else:
                        # No movement yet — emit the profile-loaded volume, NOT _last_volume.
                        # _last_volume is the old hardware position; using it here would silently
                        # override the profile level whenever another channel changes.
                        current_volumes.append(takeover[i])
                else:
                    if new_vol is not None:
                        changed = True
                        current_volumes.append(new_vol)
                    else:
                        current_volumes.append(self._channels[i]._last_volume)

        if changed:
            logger.debug("Volumes: %s", [f"{v:.2f}" for v in current_volumes])
            self.volumes_changed.emit(current_volumes)

    def _adapt_channels(self, n: int) -> None:
        """
        Resize the internal channel list to *n* entries.

        Existing channels are preserved (retains smoothing state and inversion).
        New channels use defaults; excess channels are dropped.
        _inv_flags is kept in sync so reload_settings() never goes out of bounds.
        """
        current = len(self._channels)
        if n > current:
            for i in range(current, n):
                inv = self._inv_flags[i] if i < len(self._inv_flags) else False
                self._channels.append(_ChannelState(inverted=inv, threshold=self._threshold))
                if i >= len(self._inv_flags):
                    self._inv_flags.append(False)
        elif n < current:
            self._channels = self._channels[:n]
            self._inv_flags = self._inv_flags[:n]

    def _wait_or_stop(self, seconds: float) -> None:
        """
        Sleep for ``seconds`` while checking self._running periodically.

        Splits the wait into 100 ms slices so stop() is reactive.
        """
        slices = max(1, int(seconds / 0.1))
        for _ in range(slices):
            if not self._running or self._system_sleeping:
                return
            time.sleep(0.1)

    def _wait_while_sleeping(self) -> None:
        """Idle until resume_from_sleep() or stop() without reopening serial."""
        while self._running and self._system_sleeping:
            time.sleep(0.1)
