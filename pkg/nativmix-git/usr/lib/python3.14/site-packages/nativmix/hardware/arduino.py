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
The Arduino must send a newline-terminated, pipe-separated list of raw ADC
values (0–1023), one line per update cycle:

    512|0|1023|256\n      ← four channels

This is compatible with the original deej firmware format.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Sequence

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

def _cubic_map(raw: int, inverted: bool = False) -> float:
    """
    Map a raw ADC value (0–1023) to a perceptual volume in [0.0, 1.0].

    A cubic curve is applied so that the physical center of the potentiometer
    feels like ~50% loudness, matching human hearing perception.

    Args:
        raw:      Raw ADC reading (0–1023).
        inverted: If True, the mapping is flipped (0 ADC → 1.0 volume).

    Returns:
        Perceptual volume in [0.0, 1.0].
    """
    # Clamp to valid range
    clamped = max(ADC_MIN, min(ADC_MAX, raw))

    # Normalise to [0.0, 1.0]
    normalised = clamped / ADC_MAX

    # Optional inversion (Regel 8: per-channel or global)
    if inverted:
        normalised = 1.0 - normalised

    # Cubic mapping: f(x) = x³  (emphasises lower end of the range)
    return normalised ** 3


# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------

class _ChannelState:
    """Tracks the smoothing window and last value for one potentiometer channel."""

    def __init__(self, inverted: bool = False, threshold: float = VOLUME_THRESHOLD) -> None:
        self.inverted: bool = inverted
        self.threshold: float = threshold
        self._window: deque[int] = deque(maxlen=SMOOTHING_WINDOW)
        self._last_volume: float = -1.0  # sentinel: triggers first emit

    def update(self, raw: int) -> float | None:
        """
        Add a new raw sample to the smoothing window and return the mapped
        volume, or None if the value has not changed (to avoid redundant signals).

        Args:
            raw: Raw ADC reading (0–1023).

        Returns:
            New volume [0.0, 1.0] if changed, or None.
        """
        self._window.append(raw)
        smoothed_raw = int(sum(self._window) / len(self._window))
        volume = _cubic_map(smoothed_raw, self.inverted)

        # Only emit a signal when the delta meets the configured threshold.
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
    error_occurred(str)
        Emitted on non-recoverable errors (for GUI status display).
    """

    volumes_changed = pyqtSignal(list)   # list[float] – one entry per channel
    connection_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    channel_count_changed = pyqtSignal(int)  # fired when Arduino reports a different channel count

    def __init__(
        self,
        port: str | None = None,
        num_channels: int = 5,
        inverted: Sequence[bool] | None = None,
        threshold: float = VOLUME_THRESHOLD,
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
            parent:       Optional Qt parent object.
        """
        super().__init__(parent)

        self._port: str | None = port
        self._num_channels: int = num_channels
        self._threshold: float = threshold
        self._running: bool = False
        self._reconnect_requested: bool = False
        self._connected_port: str | None = None  # last successfully connected port
        self._failed_attempts: int = 0
        self._error_notified: bool = False

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

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_inverted(self, channel: int, inverted: bool) -> None:
        """
        Flip the inversion flag for a single channel at runtime.

        This is called from the GUI when the user toggles the Invert checkbox.
        Python bool assignment is atomic, so no lock is needed.

        Effect on volume math (see _cubic_map):
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
        Syncs threshold and all inversion flags immediately.
        """
        self._threshold = config.threshold
        for i, ch in enumerate(self._channels):
            ch.threshold = self._threshold
            inv = config.get_effective_inversion(i)
            ch.inverted = inv
            self._inv_flags[i] = inv
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
        logger.info("Port switch requested: %s", port or "auto")

    @property
    def current_port(self) -> str | None:
        """The device path of the last successfully opened serial port."""
        return self._connected_port

    def stop(self) -> None:
        """Signal the thread to exit and wait for it to finish."""
        self._running = False
        self.wait()

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop: connect → read → reconnect on failure."""
        self._running = True
        logger.info("ArduinoThread started (channels=%d)", self._num_channels)

        while self._running:
            port = self._resolve_port()

            if port is None:
                # No Arduino found – log once and retry after interval
                self._handle_connection_failure()
                logger.debug("No Arduino found, retrying in %.1fs …", RECONNECT_INTERVAL)
                self._wait_or_stop(RECONNECT_INTERVAL)
                continue

            self._run_session(port)

        logger.info("ArduinoThread stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_connection_failure(self) -> None:
        self._failed_attempts += 1
        if self._failed_attempts >= 6 and not self._error_notified:
            import os
            os.system("notify-send 'NativMix' 'Arduino not found. NativMix will continue to search for the device in the background.'")
            self._error_notified = True

    def _handle_connection_success(self) -> None:
        self._failed_attempts = 0
        self._error_notified = False

    def _resolve_port(self) -> str | None:
        """
        Determine which serial port to use.

        Order of preference:
        1. User-configured port (self._port), if the device file exists.
        2. Ports from DEFAULT_PORTS that exist on the filesystem.
        3. First USB-serial port detected by pyserial's list_ports.

        Returns:
            Device path string, or None if nothing is found.
        """
        import os

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
        logger.info("Connecting to Arduino on %s @ %d baud …", port, BAUD_RATE)
        try:
            with serial.Serial(port, baudrate=BAUD_RATE, timeout=READ_TIMEOUT) as ser:
                logger.info("Arduino connected on %s", port)
                self._connected_port = port
                self._handle_connection_success()
                self.connection_changed.emit(True)

                # Flush any stale bytes left in the buffer
                ser.reset_input_buffer()

                while self._running and not self._reconnect_requested:
                    self._read_line(ser)

                if self._reconnect_requested:
                    self._reconnect_requested = False
                    logger.info("Reconnecting to new port: %s", self._port or "auto")
                    self.connection_changed.emit(False)

        except serial.SerialException as exc:
            # Device disconnected or unavailable – start reconnect loop
            logger.warning("Serial error on %s: %s", port, exc)
            self._handle_connection_failure()
            self.connection_changed.emit(False)
            # Brief pause so we don't spin-lock if the device keeps failing
            self._wait_or_stop(RECONNECT_INTERVAL)

        except OSError as exc:
            # Covers permission errors, missing device nodes, etc.
            logger.error("OS error on %s: %s", port, exc)
            self._handle_connection_failure()
            self.error_occurred.emit(str(exc))
            self.connection_changed.emit(False)
            self._wait_or_stop(RECONNECT_INTERVAL)

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

        # Dynamic channel adaptation
        if n != self._num_channels:
            logger.info(
                "Channel count changed: %d → %d", self._num_channels, n
            )
            self._num_channels = n
            self._adapt_channels(n)
            self.channel_count_changed.emit(n)

        changed = False
        current_volumes: list[float] = []

        for i, part in enumerate(parts):
            try:
                raw = int(part.strip())
            except ValueError:
                logger.debug("Non-integer value in channel %d: %r", i, part)
                return  # discard the entire malformed line

            new_vol = self._channels[i].update(raw)

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
        """
        current = len(self._channels)
        if n > current:
            # Extend: add new channels with default inversion (False)
            for i in range(current, n):
                inv = self._inv_flags[i] if i < len(self._inv_flags) else False
                self._channels.append(_ChannelState(inverted=inv, threshold=self._threshold))
        elif n < current:
            # Shrink: drop excess channels
            self._channels = self._channels[:n]

    def _wait_or_stop(self, seconds: float) -> None:
        """
        Sleep for ``seconds`` while checking self._running periodically.

        Splits the wait into 100 ms slices so stop() is reactive.
        """
        slices = int(seconds / 0.1)
        for _ in range(slices):
            if not self._running:
                return
            time.sleep(0.1)
