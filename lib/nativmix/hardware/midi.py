"""
MIDI hardware backend for NativMix.

Handles MIDI input devices (via mido/rtmidi) and maps Control Change (CC)
messages to volume levels. Supports a "Learn" mode for interactive setup.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import mido
from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)

# Ignore inbound mapped fader CC while within this band of the last outbound sync.
_FADER_FEEDBACK_TOLERANCE = 0.05

# Arduino example sketch (nativmix_midi_controller.ino) LED hue encoding.
_LED_HUE_MUTED = 0
_LED_HUE_UNMUTED = 42
_EXAMPLE_MUTE_CC_MIN = 5
_EXAMPLE_MUTE_CC_MAX = 8
_EXAMPLE_LED_CC_BASE = 32
_MUTE_OUTBOUND_SUPPRESS_S = 0.15


def _example_led_cc_for_mute(mute_cc: int) -> int | None:
    """Map example mute CC 5–8 → LED hue CC 32–35; else None."""
    if _EXAMPLE_MUTE_CC_MIN <= mute_cc <= _EXAMPLE_MUTE_CC_MAX:
        return _EXAMPLE_LED_CC_BASE + (mute_cc - _EXAMPLE_MUTE_CC_MIN)
    return None


def _inbound_fader_suppressed(takeover_volume: float | None, cc_value: int) -> bool:
    """Return True when an inbound CC likely echoes our own outbound fader sync."""
    if takeover_volume is None:
        return False
    return abs(cc_value / 127.0 - takeover_volume) <= _FADER_FEEDBACK_TOLERANCE


def _match_midi_port(names: list[str], device_key: str) -> str | None:
    """Find the first port name containing *device_key*."""
    for name in names:
        if device_key in name:
            return name
    return None


def ensure_midi_backend() -> str | None:
    """Probe and set the best available mido backend.

    On Windows: uses rtmidi/WinMM directly — no portmidi library search.
    On Linux: tries rtmidi first; on Fedora/Nobara portmidi is preferred.
    Returns the backend name ('rtmidi' or 'portmidi') or None if none is available.
    Idempotent — safe to call multiple times.
    """
    if sys.platform == "win32":
        try:
            import rtmidi  # noqa: F401

            mido.set_backend("mido.backends.rtmidi")
            return "rtmidi"
        except ImportError:
            return None

    from nativmix.utils.distro import is_fedora

    backends_to_try = ["portmidi", "rtmidi"] if is_fedora() else ["rtmidi", "portmidi"]

    for b_name in backends_to_try:
        try:
            if b_name == "rtmidi":
                import rtmidi  # noqa: F401

                mido.set_backend("mido.backends.rtmidi")
                return "rtmidi"
            else:
                import ctypes
                import ctypes.util

                _lib = ctypes.util.find_library("portmidi")
                if not _lib:
                    raise ImportError("libportmidi.so not found")
                ctypes.CDLL(_lib)
                mido.set_backend("mido.backends.portmidi")
                return "portmidi"
        except (ImportError, OSError):
            continue
    return None


class MidiThread(QThread):
    """
    Background thread that listens for MIDI CC messages from a specific device.

    Signals
    -------
    midi_volumes_changed(list[tuple[int, float]])
        Emitted when mapped MIDI CC values change.
        List of (channel_index, volume_0_to_1).
    midi_cc_received(int, int, int)
        Emitted for the "Learn" handshake: (midi_channel, control_number, value).
    connection_changed(bool)
        Emitted when the device is opened (True) or closed/missing (False).
    """

    midi_volumes_changed = pyqtSignal(list)  # list[tuple[int, float]]
    midi_cc_received = pyqtSignal(int, int, int)  # midi_channel, cc, value
    midi_mute_toggled = pyqtSignal(int)  # channel_index
    connection_changed = pyqtSignal(bool)
    # Status signal: (status_type, display_message)
    # Types: "connecting", "stable", "error_temporary", "error_critical"
    status_changed = pyqtSignal(str, str)
    profile_switch_requested = pyqtSignal(str)  # "next", "prev", or profile_id
    fader_sync_requested = pyqtSignal(list)  # list[tuple[int, float]] (channel, volume)
    mute_feedback_requested = pyqtSignal(list)  # list[tuple[int, bool]] (channel, muted)

    def __init__(self, device_name: str = "", input_mode: str = "hybrid", parent=None) -> None:
        super().__init__(parent)
        self._device_name: str = device_name
        self._input_mode: str = input_mode  # "usb", "hybrid", "midi_only"
        self._running: bool = False
        self._panic_flag: bool = False
        self._critical_error: bool = False
        self._error_count: int = 0
        # (midi_channel, cc) -> NativMix channel_index
        self._cc_map: dict[tuple[int, int], int] = {}
        self._mute_cc_map: dict[tuple[int, int], int] = {}
        self._map_lock = threading.RLock()
        self._last_values: dict[tuple[int, int], int] = {}
        self._last_vol_emit: dict[tuple[int, int], float] = {}
        # Persistent virtual port – kept alive across USB ↔ hybrid mode
        # switches so ALSA clients see one stable "NativMix:Input" port.
        self._virtual_client = None
        self._profile_next_cc: int | None = None
        self._profile_prev_cc: int | None = None
        self._profile_direct_map: dict[int, str] = {}  # cc -> profile_id
        self._fader_feedback_enabled: bool = False
        self._feedback_lock = threading.Lock()
        self._feedback_takeover: dict[int, float] = {}  # channel_index -> last sent volume
        self._last_sent_cc_value: dict[tuple[int, int], int] = {}  # (midi_ch, cc) -> 0-127
        self._pending_sync: list[tuple[int, float]] | None = None
        self._pending_mute_feedback: list[tuple[int, bool]] | None = None
        # Ignore mute-toggle echoes shortly after we send mute/LED feedback.
        self._mute_outbound_suppress_until: dict[tuple[int, int], float] = {}
        self.fader_sync_requested.connect(self._queue_fader_sync)
        self.mute_feedback_requested.connect(self._queue_mute_feedback)

    def set_fader_feedback_enabled(self, enabled: bool) -> None:
        """Enable or disable outbound MIDI CC fader / mute LED sync."""
        if self._fader_feedback_enabled != enabled:
            logger.debug("MIDI fader feedback %s", "enabled" if enabled else "disabled")
        self._fader_feedback_enabled = enabled
        if not enabled:
            with self._feedback_lock:
                self._feedback_takeover.clear()
                self._last_sent_cc_value.clear()
                self._pending_sync = None
                self._pending_mute_feedback = None
                self._mute_outbound_suppress_until.clear()

    @pyqtSlot(list)
    def _queue_fader_sync(self, mappings: list[tuple[int, float]]) -> None:
        """Queue outbound fader positions (thread-safe via queued signal)."""
        if not self._fader_feedback_enabled or not mappings:
            return
        with self._feedback_lock:
            self._pending_sync = list(mappings)

    @pyqtSlot(list)
    def _queue_mute_feedback(self, states: list[tuple[int, bool]]) -> None:
        """Queue outbound mute CC + optional Arduino LED hue."""
        if not self._fader_feedback_enabled or not states:
            return
        with self._feedback_lock:
            self._pending_mute_feedback = list(states)

    def request_fader_sync(self, mappings: list[tuple[int, float]]) -> None:
        """Request outbound CC sync; safe to call from the GUI/main thread."""
        self.fader_sync_requested.emit(mappings)

    def request_mute_feedback(self, states: list[tuple[int, bool]]) -> None:
        """Request mute/LED outbound sync; safe from the GUI/main thread."""
        self.mute_feedback_requested.emit(states)

    def set_device(self, name: str) -> None:
        """Update the target MIDI device. Reconnects on the next loop cycle."""
        if self._device_name != name:
            logger.info("MIDI Port change requested: %s", name)
            self._device_name = name
            self._panic_flag = True

    def set_mode(self, mode: str) -> None:
        """Update the input mode (to know if MIDI is allowed)."""
        if self._input_mode != mode:
            logger.debug("MIDI Mode changed: %s -> %s", self._input_mode, mode)
            self._input_mode = mode
            self._panic_flag = True

    def update_mappings(self, mappings: dict[tuple[int, int], int]) -> None:
        """
        Update volume CC mappings.
        Args:
            mappings: (midi_channel, cc) -> NativMix channel index.
        """
        with self._map_lock:
            self._cc_map = dict(mappings)
        logger.debug("MIDI CC mappings updated: %s", mappings)

    def update_mute_mappings(self, mappings: dict[tuple[int, int], int]) -> None:
        """
        Update mute-CC mappings.
        Args:
            mappings: (midi_channel, cc) -> NativMix channel index.
        """
        with self._map_lock:
            self._mute_cc_map = dict(mappings)
        logger.debug("MIDI Mute CC mappings updated: %s", mappings)

    def set_profile_ccs(
        self,
        next_cc: int | None,
        prev_cc: int | None,
        direct_map: dict[int, str],
    ) -> None:
        """
        Configure MIDI CCs for profile switching.

        next_cc:    CC number that triggers switch_next (fires on value 127).
        prev_cc:    CC number that triggers switch_prev (fires on value 127).
        direct_map: {cc_number: profile_id} for direct profile jumps.
        """
        self._profile_next_cc = next_cc
        self._profile_prev_cc = prev_cc
        self._profile_direct_map = dict(direct_map)
        logger.debug(
            "Profile CCs updated: next=%s prev=%s direct=%s",
            next_cc,
            prev_cc,
            direct_map,
        )

    def get_mapped_volumes(self) -> list[tuple[int, float]]:
        """Return a list of (channel_index, volume) for all current mappings."""
        results = []
        with self._map_lock:
            items = list(self._cc_map.items())
        for key, ch_idx in items:
            if key in self._last_values:
                val = self._last_values[key]
                results.append((ch_idx, val / 127.0))
        return results

    def refresh_ports(self) -> None:
        """Trigger a re-scan of MIDI ports (Hot-Plug support)."""
        logger.info("MIDI Refresh requested (Hot-Plug).")
        self._panic_flag = True

    def stop(self) -> None:
        """Gracefully stop the thread loop."""
        self._running = False
        # Give the loop one more slice to check _running
        # Only terminate if it's really stuck (finally blocks might not run!)
        if not self.wait(2000):
            logger.warning("MidiThread: Force-terminating (graceful stop took too long)")
            self.terminate()
            # Strategy B: bounded wait after terminate() so rtmidi/ALSA calls
            # blocked during system audio teardown cannot hang indefinitely.
            if not self.wait(1000):
                logger.error("MidiThread still alive after terminate — abandoning")
        # Close the persistent virtual port (if still open) now that the
        # thread has stopped.  This releases the ALSA sequencer client.
        if self._virtual_client is not None:
            try:
                self._virtual_client.close_port()
            except (OSError, RuntimeError) as exc:
                logger.debug("MidiThread: virtual port close: %s", exc)
            self._virtual_client = None

    def restart_midi(self) -> None:
        """Manual reset to clear critical errors and restart the backend."""
        logger.info("MIDI Restart requested by user/system.")
        self._critical_error = False
        self._error_count = 0
        self._panic_flag = True
        self.status_changed.emit("connecting", "Restarting MIDI...")

    def run(self) -> None:
        """Main loop with Circuit Breaker protection."""
        self._running = True
        self._panic_flag = False
        self._critical_error = False
        self._error_count = 0

        logger.info("MidiThread started. (Mode: %s, Device: %s)", self._input_mode, self._device_name)

        while self._running:
            try:
                self._run_safe()
                # _run_safe() exited cleanly (e.g. stop() called) — reset circuit breaker
                # so a subsequent restart() begins from a clean state.
                self._critical_error = False
                self._error_count = 0
            except Exception as exc:
                self._error_count += 1
                logger.exception("CRITICAL MidiThread crash (Circuit Breaker triggered)")

                if self._error_count >= 3:
                    self._critical_error = True
                    self.status_changed.emit("error_critical", f"MIDI Error: {str(exc)}")
                    logger.error(
                        "MIDI Circuit Breaker: Backend disabled after %d consecutive failures.",
                        self._error_count,
                    )
                else:
                    self.status_changed.emit("error_temporary", "MIDI Backend crashed - Recovering...")

                # Cooldown before retry or while disabled
                self._sleep_checked(5.0)

    def _run_safe(self) -> None:
        """Inner loop for MIDI processing logic."""
        backend_found = ensure_midi_backend()

        if backend_found == "rtmidi":
            logger.info("MIDI Backend loaded: rtmidi (supports virtual ports)")
        elif backend_found == "portmidi":
            logger.info("MIDI Backend loaded: portmidi via ctypes")

        if not backend_found:
            logger.error("CRITICAL: No MIDI backend (rtmidi or portmidi) found! MIDI will not work.")
            self.connection_changed.emit(False)
            self.status_changed.emit("error_critical", "No MIDI backend found.")
            # Stay in loop but idle
            while self._running and not self._panic_flag:
                self._sleep_checked(1.0)
            return

        self._error_count = 0  # Reset on successful backend load
        self.status_changed.emit("stable", "MIDI Ready")

        _vport_warning_logged = False
        while self._running:
            if self._panic_flag:
                self._panic_flag = False
                logger.debug("MidiThread: Internally restarting due to flag.")

            # Is MIDI even enabled?
            if self._input_mode == "usb":
                # USB-only: idle without closing the virtual port so ALSA
                # clients see one stable "NativMix:Input" across mode switches.
                if self._virtual_client is None:
                    self.connection_changed.emit(False)
                # Wait for setting changes
                while self._running and not self._panic_flag and self._input_mode == "usb":
                    time.sleep(0.5)
                continue

            try:
                if self._critical_error:
                    self._sleep_checked(2.0)
                    continue

                target_device = self._device_name if self._device_name else "VIRTUAL_PORT"

                if target_device == "VIRTUAL_PORT":
                    if sys.platform == "win32":
                        # WinMM does not support virtual MIDI ports.
                        if not _vport_warning_logged:
                            logger.warning("MidiThread: Virtual Port is not supported on Windows (WinMM).")
                            _vport_warning_logged = True
                        self.connection_changed.emit(False)
                        self.status_changed.emit("disabled", "Virtual Port: not supported on Windows")
                        self._sleep_checked(5.0)
                        continue

                    if backend_found != "rtmidi":
                        if not _vport_warning_logged:
                            logger.info(
                                "MidiThread: Virtual Port requires rtmidi, but %s is loaded"
                                " — expected on Fedora/Nobara. Skipping.",
                                backend_found,
                            )
                            _vport_warning_logged = True
                        self.connection_changed.emit(False)
                        self.status_changed.emit("disabled", "Virtual Port needs rtmidi")
                        self._sleep_checked(5.0)
                        continue

                    # Reuse the existing virtual port if already open so ALSA
                    # clients see one stable port across USB ↔ hybrid switches.
                    if self._virtual_client is None:
                        logger.debug("MidiThread: Opening Virtual Port 'NativMix:Input'...")
                        self.status_changed.emit("connecting", "Opening Virtual Port...")
                        _client = None
                        try:
                            import rtmidi  # Local import for safety

                            _client = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name="NativMix")
                            _client.open_virtual_port("Input")
                            self._virtual_client = _client
                        except Exception as e:
                            logger.warning("MidiThread: Could not open virtual port: %s", e)
                            if _client is not None:
                                try:
                                    _client.close_port()
                                except Exception as exc:
                                    logger.debug("MidiThread: close_port cleanup failed: %s", exc)
                            self._virtual_client = None
                            self.connection_changed.emit(False)
                            self.status_changed.emit("error_temporary", "Virtual Port failed - retrying...")
                            self._sleep_checked(5.0)
                            continue
                    else:
                        logger.debug("MidiThread: Reusing existing Virtual Port 'NativMix:Input'.")

                    self.connection_changed.emit(True)
                    self.status_changed.emit("stable", "Virtual MIDI Online")

                    while self._running and not self._panic_flag:
                        # Only exit if switching to a physical device; a mode
                        # change to USB keeps the port alive (handled above).
                        if self._device_name not in ("", "VIRTUAL_PORT"):
                            self._virtual_client.close_port()
                            self._virtual_client = None
                            logger.debug("MidiThread: Virtual Port closed (device change).")
                            break

                        # In USB mode just idle – don't process MIDI events.
                        if self._input_mode == "usb":
                            time.sleep(0.01)
                            continue

                        self._process_pending_sync(None)

                        msg_data = self._virtual_client.get_message()
                        if msg_data:
                            msg, _ = msg_data
                            if len(msg) >= 3 and (msg[0] & 0xF0) == 0xB0:
                                midi_ch = int(msg[0] & 0x0F)
                                self._handle_cc(midi_ch, msg[1], msg[2])

                        time.sleep(0.01)

                else:
                    # Physical Device Mode
                    logger.info("MidiThread: Connecting to physical device: %s", target_device)
                    names = mido.get_input_names()
                    logger.info("MidiThread: Available MIDI ports: %s", names)
                    target_name = None
                    for name in names:
                        if target_device in name:
                            target_name = name
                            break

                    if not target_name:
                        logger.warning("MidiThread: Device '%s' not found. Available: %s", target_device, names)
                        self.connection_changed.emit(False)
                        self.status_changed.emit("error_temporary", f"Device '{target_device}' not found")
                        self._sleep_checked(5.0)
                        continue

                    out_name = None
                    if self._fader_feedback_enabled:
                        try:
                            out_name = _match_midi_port(mido.get_output_names(), target_device)
                        except Exception as exc:
                            logger.debug("MidiThread: could not list MIDI outputs: %s", exc)
                        if out_name is None:
                            logger.warning(
                                "MIDI fader feedback enabled but no output port matched '%s'",
                                target_device,
                            )

                    if out_name:
                        with mido.open_input(target_name) as inport, mido.open_output(out_name) as outport:
                            logger.info("MidiThread: Connected to %s (out: %s)", target_name, out_name)
                            self.status_changed.emit("stable", f"Connected: {target_device}")
                            self.connection_changed.emit(True)
                            self._device_loop(inport, outport, target_device)
                    else:
                        with mido.open_input(target_name) as inport:
                            logger.info("MidiThread: Connected to %s", target_name)
                            self.status_changed.emit("stable", f"Connected: {target_device}")
                            self.connection_changed.emit(True)
                            self._device_loop(inport, None, target_device)

            except (OSError, EOFError, RuntimeError, TypeError) as exc:
                logger.warning("MIDI Recoverable Error: %s", exc)
                self.connection_changed.emit(False)
                self.status_changed.emit("error_temporary", "MIDI Disconnected - Retrying...")
                self._sleep_checked(5.0)

        logger.debug("MidiThread stopped")

    def _device_loop(self, inport, outport, target_device: str) -> None:
        """Poll a physical MIDI input (and optional output) until reconnect is needed."""
        while self._running and not self._panic_flag:
            if self._input_mode == "usb" or self._device_name != target_device:
                break
            self._process_pending_sync(outport)
            self._process_pending_mute_feedback(outport)
            msg = inport.receive(block=False)
            if msg is None:
                time.sleep(0.05)
                continue
            if msg.type == "control_change":
                self._handle_cc(int(msg.channel), msg.control, msg.value)

    def _process_pending_sync(self, outport) -> None:
        """Send queued outbound fader CC values when feedback is enabled."""
        if not self._fader_feedback_enabled:
            return
        with self._feedback_lock:
            pending = self._pending_sync
            self._pending_sync = None
        if not pending or outport is None:
            return

        ch_to_bindings: dict[int, list[tuple[int, int]]] = {}
        with self._map_lock:
            items = list(self._cc_map.items())
        for key, ch_idx in items:
            ch_to_bindings.setdefault(ch_idx, []).append(key)
        for ch_idx, volume in pending:
            for midi_ch, cc in ch_to_bindings.get(ch_idx, []):
                self._send_fader_cc(outport, midi_ch, cc, ch_idx, volume)

    def _process_pending_mute_feedback(self, outport) -> None:
        """Send mute CC state and optional Arduino LED hue."""
        if not self._fader_feedback_enabled:
            return
        with self._feedback_lock:
            pending = self._pending_mute_feedback
            self._pending_mute_feedback = None
        if not pending or outport is None:
            return

        with self._map_lock:
            ch_to_mute: dict[int, tuple[int, int]] = {ch_idx: key for key, ch_idx in self._mute_cc_map.items()}
        now = time.monotonic()
        for ch_idx, muted in pending:
            binding = ch_to_mute.get(ch_idx)
            if binding is None:
                continue
            midi_ch, cc = binding
            value = 127 if muted else 0
            self._send_raw_cc(outport, midi_ch, cc, value)
            with self._feedback_lock:
                self._mute_outbound_suppress_until[binding] = now + _MUTE_OUTBOUND_SUPPRESS_S
            led_cc = _example_led_cc_for_mute(cc)
            if led_cc is not None:
                hue = _LED_HUE_MUTED if muted else _LED_HUE_UNMUTED
                self._send_raw_cc(outport, midi_ch, led_cc, hue)

    def _send_raw_cc(self, outport, midi_ch: int, cc: int, value: int) -> None:
        """Send one CC without volume takeover arming."""
        cc_value = max(0, min(127, int(value)))
        key = (midi_ch, cc)
        with self._feedback_lock:
            if self._last_sent_cc_value.get(key) == cc_value:
                return
            self._last_sent_cc_value[key] = cc_value
            self._last_values[key] = cc_value
        try:
            outport.send(
                mido.Message(
                    "control_change",
                    channel=max(0, min(15, midi_ch)),
                    control=cc,
                    value=cc_value,
                )
            )
            logger.debug(
                "MIDI outbound CC: midi_ch=%d cc=%d value=%d",
                midi_ch,
                cc,
                cc_value,
            )
        except (OSError, RuntimeError) as exc:
            logger.warning("MIDI outbound CC send failed (cc=%d): %s", cc, exc)

    def _send_fader_cc(
        self,
        outport,
        midi_ch: int,
        cc: int,
        ch_idx: int,
        volume: float,
    ) -> None:
        """Send one outbound volume CC and arm takeover suppression for that channel."""
        cc_value = max(0, min(127, int(round(max(0.0, min(1.0, volume)) * 127))))
        key = (midi_ch, cc)
        with self._feedback_lock:
            if self._last_sent_cc_value.get(key) == cc_value:
                return
            self._last_sent_cc_value[key] = cc_value
            self._last_values[key] = cc_value
            self._feedback_takeover[ch_idx] = cc_value / 127.0
        try:
            outport.send(
                mido.Message(
                    "control_change",
                    channel=max(0, min(15, midi_ch)),
                    control=cc,
                    value=cc_value,
                )
            )
            logger.debug(
                "MIDI fader feedback: nmix_ch=%d midi_ch=%d cc=%d value=%d",
                ch_idx,
                midi_ch,
                cc,
                cc_value,
            )
        except (OSError, RuntimeError) as exc:
            logger.warning("MIDI fader feedback send failed (cc=%d): %s", cc, exc)

    def _handle_cc(self, midi_ch: int, cc: int, val: int) -> None:
        """Process a single MIDI Control Change message on *midi_ch* (0-15)."""
        midi_ch = max(0, min(15, int(midi_ch)))
        key = (midi_ch, cc)
        self._last_values[key] = val

        # 1. Always emit for Learn handshake
        self.midi_cc_received.emit(midi_ch, cc, val)

        # 2. Check if mapped to a fader — throttled to 50 Hz per binding (20 ms)
        with self._map_lock:
            ch_idx = self._cc_map.get(key)
        if ch_idx is not None:
            with self._feedback_lock:
                takeover_vol = self._feedback_takeover.get(ch_idx)
            if _inbound_fader_suppressed(takeover_vol, val):
                return
            if takeover_vol is not None:
                with self._feedback_lock:
                    self._feedback_takeover.pop(ch_idx, None)
            now = time.monotonic()
            if now - self._last_vol_emit.get(key, 0.0) >= 0.02:
                self._last_vol_emit[key] = now
                vol = val / 127.0
                self.midi_volumes_changed.emit([(ch_idx, vol)])

        # 3. Mute toggle (value == 127 only). Suppress echoes of our own outbound.
        if val == 127:
            with self._map_lock:
                mute_ch = self._mute_cc_map.get(key)
            if mute_ch is not None:
                with self._feedback_lock:
                    until = self._mute_outbound_suppress_until.get(key, 0.0)
                if time.monotonic() >= until:
                    self.midi_mute_toggled.emit(mute_ch)

        # 4. Profile switching (only on button press, value == 127)
        if val == 127:
            if cc == self._profile_next_cc:
                self.profile_switch_requested.emit("next")
            elif cc == self._profile_prev_cc:
                self.profile_switch_requested.emit("prev")
            elif cc in self._profile_direct_map:
                self.profile_switch_requested.emit(self._profile_direct_map[cc])

    def _sleep_checked(self, seconds: float) -> None:
        """Sleep while checking for thread stop request."""
        end_time = time.time() + seconds
        while self._running and time.time() < end_time:
            time.sleep(0.1)
