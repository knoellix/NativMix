"""
Windows WASAPI audio backend for NativMix.

Uses pycaw (Python Core Audio Windows) for per-app volume control via the
Windows Audio Session API (WASAPI).  Equivalent to PipeWireManager on Linux.

Feature differences vs. Linux backend:
  - V-Sinks: not supported — enable_v_sink / disable_v_sink are No-Ops.
  - Session detection: polling at ~250 ms (STA/MTA threading constraints make
    COM session callbacks impractical; polling is the stable approach).
  - Hardware sink control: system master only (via default audio endpoint).

Install dependencies:
    pip install "nativmix[windows]"
    # pulls in: pycaw>=20230407, comtypes>=1.2, psutil>=5.9
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from nativmix.audio.base import AudioBackendBase, StreamInfo

if TYPE_CHECKING:
    from nativmix.utils.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional import guard — pycaw / comtypes / psutil are Windows-only
# ---------------------------------------------------------------------------

try:
    import psutil  # noqa: F401 (used inside helper functions)
    from pycaw.utils import AudioUtilities
    _WASAPI_AVAILABLE = True
except ImportError:
    _WASAPI_AVAILABLE = False
    logger.warning(
        "pycaw or psutil not installed — WASAPI backend unavailable. "
        "Run: pip install \"nativmix[windows]\""
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _session_name(session) -> str:
    """
    Return a clean app name for a pycaw AudioSession, or '' on failure.

    Uses resolve_app_name_windows() for Electron/Chromium disambiguation
    (--user-data-dir, --app-id, PPID walk) before falling back to the
    raw executable name.
    """
    try:
        pid = session.ProcessId
        if pid and pid != 0:
            from nativmix.utils.proc_resolver import resolve_app_name_windows
            name = resolve_app_name_windows(pid, fallback="")
            if name:
                return name
        # Fallback: raw exe name from psutil
        proc = session.Process
        if proc:
            name = proc.name()
            return name[:-4] if name.lower().endswith(".exe") else name
    except Exception as exc:
        logger.debug("_session_name: failed to resolve session name: %s", exc)
    return ""


def _set_session_volume(session, volume: float) -> None:
    """Set master volume on a pycaw session's ISimpleAudioVolume."""
    try:
        sv = session.SimpleAudioVolume
        if sv:
            sv.SetMasterVolume(max(0.0, min(1.0, volume)), None)
    except Exception as exc:
        logger.debug("WASAPI SetMasterVolume failed: %s", exc)


def _set_session_mute(session, muted: bool) -> None:
    """Set mute state on a pycaw session's ISimpleAudioVolume."""
    try:
        sv = session.SimpleAudioVolume
        if sv:
            sv.SetMute(muted, None)
    except Exception as exc:
        logger.debug("WASAPI SetMute failed: %s", exc)


def _get_sessions() -> list:
    """Return all active pycaw AudioSession objects, or [] on error."""
    if not _WASAPI_AVAILABLE:
        return []
    try:
        return AudioUtilities.GetAllSessions()
    except Exception as exc:
        logger.warning("WASAPI GetAllSessions failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Background listener thread
# ---------------------------------------------------------------------------

class _WasapiListenerThread(QThread):
    """
    Polls Windows Audio Sessions every 150 ms to detect new / removed streams.

    Emits stream_added for each newly detected session so that
    WasapiManager can apply the Two-Stage Mute-Catch.
    """

    stream_added   = pyqtSignal(object)   # StreamInfo
    stream_removed = pyqtSignal(int)      # pid
    audit_finished = pyqtSignal()
    status_changed = pyqtSignal(str, str) # (status_type, message)
    peaks_updated  = pyqtSignal(list)     # list[float], one per channel

    _POLL_INTERVAL_MS = 250

    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self._config = config
        self._running = False
        # pid → app_name for currently known sessions
        self._known: dict[int, str] = {}
        # Cached IAudioEndpointVolume — acquired once, reused for all master-volume calls
        self._endpoint_volume = None

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        # COM must be initialised per-thread on Windows.
        try:
            import comtypes
            comtypes.CoInitialize()
            _com_init = True
        except Exception as exc:
            _com_init = False
            logger.warning("WASAPI: COM initialization failed (%s) — audio backend unavailable", exc)

        if not _com_init:
            self.status_changed.emit("error_critical", "COM init failed — audio unavailable")
            self.audit_finished.emit()
            return

        try:
            self._initial_audit()
        except Exception as exc:
            logger.error("WASAPI initial audit error: %s", exc)
            self.status_changed.emit("error", str(exc))

        self.audit_finished.emit()

        while self._running:
            try:
                self._poll_sessions()
            except Exception as exc:
                logger.warning("WASAPI poll error: %s", exc)
            self.msleep(self._POLL_INTERVAL_MS)

        if _com_init:
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _initial_audit(self) -> None:
        """Snapshot existing sessions without muting them."""
        for session in _get_sessions():
            pid = session.ProcessId
            if pid == 0:
                continue
            name = _session_name(session)
            if name:
                self._known[pid] = name
        logger.debug("WASAPI initial audit: %d sessions found", len(self._known))

    def _poll_sessions(self) -> None:
        """Diff current sessions against known ones; emit added/removed signals."""
        sessions = _get_sessions()
        current: dict[int, str] = {}

        for session in sessions:
            pid = session.ProcessId
            if pid == 0:
                continue
            name = _session_name(session)
            if not name:
                continue
            current[pid] = name

            if pid not in self._known:
                self._known[pid] = name
                info = StreamInfo(index=pid, app_name=name, pid=pid)
                self.stream_added.emit(info)

        # Detect removed sessions
        for pid in set(self._known) - set(current):
            del self._known[pid]
            self.stream_removed.emit(pid)

        if self._config.vu_meter_enabled:
            self.peaks_updated.emit(self._collect_peaks(sessions, current))

    def _collect_peaks(self, sessions: list, pid_to_name: dict[int, str]) -> list[float]:
        """Return per-channel peak levels by querying IAudioMeterInformation."""
        try:
            from pycaw.pycaw import IAudioMeterInformation
            from ctypes import cast, POINTER
        except ImportError:
            return [0.0] * self._config.num_channels

        name_to_channel = self._config.get_all_assigned_apps_by_name()
        channel_peaks: list[float] = [0.0] * self._config.num_channels
        for session in sessions:
            pid = session.ProcessId
            if pid == 0:
                continue
            name = pid_to_name.get(pid, "")
            ch = name_to_channel.get(name.lower())
            if ch is None:
                continue
            try:
                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                peak = meter.GetPeakValue()
                channel_peaks[ch] = max(channel_peaks[ch], float(peak))
            except Exception:
                pass
        return channel_peaks


# ---------------------------------------------------------------------------
# WasapiManager
# ---------------------------------------------------------------------------

class WasapiManager(AudioBackendBase):
    """
    Windows audio backend.  Mirrors the public API of PipeWireManager so that
    main.py needs no platform-specific code beyond the instantiation switch.
    """

    mute_state_changed    = pyqtSignal(int, bool)
    channel_volume_changed = pyqtSignal(int, float)
    other_apps_changed    = pyqtSignal(list)
    audit_finished        = pyqtSignal()
    status_changed        = pyqtSignal(str, str)
    peaks_updated         = pyqtSignal(list)     # list[float], one per channel

    def __init__(self, config: ConfigManager | None = None, parent=None) -> None:
        super().__init__(parent)
        # Lazy import to avoid circular dependency at module load time
        if config is None:
            from nativmix.utils.config_manager import ConfigManager as _CM
            config = _CM()
        self._config: ConfigManager = config
        self._thread: _WasapiListenerThread | None = None
        self._running: bool = False
        # channel → last applied volume from hardware
        self._poti_volumes: dict[int, float] = {}
        # channel → explicit mute state (from IPC hotkeys or GUI button)
        self._channel_muted: dict[int, bool] = {}
        # channel → poti position at the time of muting (for auto-unmute threshold)
        self._muted_at_volume: dict[int, float] = {}
        # RLock because apply_poti_volumes calls _do_toggle_mute on the same thread
        self._state_lock = threading.RLock()

    # ------------------------------------------------------------------
    # AudioBackendBase interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background session-polling thread."""
        if self._thread is not None and self._thread.isRunning():
            logger.warning("WasapiManager.start() called but thread already running")
            return
        self._running = True
        self._thread = _WasapiListenerThread(config=self._config)
        self._thread.stream_added.connect(self._on_stream_added)
        self._thread.stream_removed.connect(self._on_stream_removed)
        self._thread.audit_finished.connect(self.audit_finished)
        self._thread.status_changed.connect(self.status_changed)
        self._thread.peaks_updated.connect(self.peaks_updated)
        self._thread.start()
        logger.info("WasapiManager started (WASAPI available: %s)", _WASAPI_AVAILABLE)

    def stop(self) -> None:
        """Stop the polling thread and release resources."""
        self._running = False
        if self._thread:
            self._thread.stop()
            self._thread.wait(3000)
            self._thread = None
        logger.info("WasapiManager stopped")

    def set_volume(self, stream_index: int, volume: float) -> None:
        """Low-level stream volume — not used directly; routing goes via app name."""

    def set_mute(self, stream_index: int, muted: bool) -> None:
        """Low-level stream mute — not used directly; routing goes via app name."""

    # ------------------------------------------------------------------
    # Volume control (called from Arduino / MIDI / GUI slider)
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    def apply_poti_volumes(self, volumes: list[float]) -> None:
        """Slot: receives raw hardware fader values from ArduinoThread."""
        with self._state_lock:
            for ch, vol in enumerate(volumes):
                if ch >= self._config.num_channels:
                    break
                self._poti_volumes[ch] = vol
                # Auto-unmute: if the user moves the fader >5% from where
                # it was when the channel was muted, unmute it.
                if self._channel_muted.get(ch, False):
                    if abs(vol - self._muted_at_volume.get(ch, vol)) > 0.05:
                        self._do_toggle_mute(ch)
                self._apply_channel_volume(ch, vol)

    @pyqtSlot(list)
    def apply_midi_volumes(self, mappings: list[tuple[int, float]]) -> None:
        """Slot: receives CC values from MidiThread."""
        with self._state_lock:
            for ch, vol in mappings:
                if ch >= self._config.num_channels:
                    continue
                self._poti_volumes[ch] = vol
                if self._channel_muted.get(ch, False):
                    if abs(vol - self._muted_at_volume.get(ch, vol)) > 0.05:
                        self._do_toggle_mute(ch)
                self._apply_channel_volume(ch, vol)

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        """Called directly by the GUI slider."""
        with self._state_lock:
            if self._channel_muted.get(channel_index, False):
                self._do_toggle_mute(channel_index)
            self._poti_volumes[channel_index] = volume
            self._apply_channel_volume(channel_index, volume)

    def toggle_mute(self, channel_index: int) -> None:
        """Toggle the mute state of a channel (IPC hotkey / GUI button)."""
        with self._state_lock:
            self._do_toggle_mute(channel_index)

    # ------------------------------------------------------------------
    # Mapping-changed slot (called by config.mapping_changed signal)
    # ------------------------------------------------------------------

    def _on_mapping_changed(self, channel_index: int, app_names: list[str]) -> None:
        """Apply current channel volume to newly mapped apps."""
        with self._state_lock:
            vol = self._poti_volumes.get(channel_index, 0.5)
            muted = self._channel_muted.get(channel_index, False)
        for app_name in app_names:
            self._apply_volume_by_name(app_name, vol)
            self._apply_mute_by_name(app_name, muted)

    # ------------------------------------------------------------------
    # Stream lifecycle (Two-Stage Mute-Catch)
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def _on_stream_added(self, info: StreamInfo) -> None:
        """
        Two-Stage Mute-Catch (Windows edition):

        Stage 1 — Reflex: mute the new session immediately.
        Stage 2 — Resolution: apply the correct fader volume, then unmute.

        Note: because we use polling, the session may have already been
        audible for up to ~250 ms before this slot fires.
        """
        ch = self._config.find_channel_for_app(info.app_name)
        if ch is None:
            logger.debug("New unmapped session: %s (pid=%d)", info.app_name, info.pid)
            return

        # Stage 1: mute immediately
        self._apply_mute_by_name(info.app_name, True)

        # Stage 2: apply volume and restore channel mute state
        with self._state_lock:
            vol = self._poti_volumes.get(ch, 0.5)
            muted = self._channel_muted.get(ch, False)
        self._apply_volume_by_name(info.app_name, vol)
        self._apply_mute_by_name(info.app_name, muted)
        logger.debug(
            "Two-Stage: applied vol=%.2f muted=%s to %s (ch=%d)",
            vol, muted, info.app_name, ch,
        )

    @pyqtSlot(int)
    def _on_stream_removed(self, pid: int) -> None:
        logger.debug("WASAPI session removed: pid=%d", pid)

    # ------------------------------------------------------------------
    # V-Sink stubs (not supported on Windows)
    # ------------------------------------------------------------------

    def enable_v_sink(self, channel_index: int) -> None:
        """No-Op on Windows — V-Sinks require PipeWire null-sinks."""
        logger.debug("enable_v_sink: No-Op on Windows (channel %d)", channel_index)

    def disable_v_sink(self, channel_index: int) -> None:
        """No-Op on Windows."""
        logger.debug("disable_v_sink: No-Op on Windows (channel %d)", channel_index)

    # ------------------------------------------------------------------
    # Debug / IPC helpers
    # ------------------------------------------------------------------

    def get_active_streams(self) -> list[StreamInfo]:
        """Return a snapshot of current audio sessions (for GUI / IPC)."""
        result: list[StreamInfo] = []
        for idx, session in enumerate(_get_sessions()):
            name = _session_name(session)
            if not name:
                continue
            try:
                sv = session.SimpleAudioVolume
                vol = sv.GetMasterVolume() if sv else 1.0
                muted = bool(sv.GetMute()) if sv else False
            except Exception:
                vol, muted = 1.0, False
            result.append(StreamInfo(
                index=idx,
                app_name=name,
                pid=session.ProcessId,
                volume=vol,
                muted=muted,
            ))
        return result

    def get_real_sinks(self) -> list[tuple[str, str]]:
        """Return available audio output endpoints as (description, id) pairs.

        On Windows there is no concept of PipeWire sinks — we return the
        default audio endpoint so the Master Output dropdown is populated.
        """
        if not _WASAPI_AVAILABLE:
            return []
        try:
            from pycaw.utils import AudioUtilities
            device = AudioUtilities.GetSpeakers()
            # Use the device's friendly name as both label and id
            name = getattr(device, "FriendlyName", None) or "Default Audio Output"
            return [(name, name)]
        except Exception as exc:
            logger.debug("get_real_sinks failed: %s", exc)
            return [("Default Audio Output", "default")]

    def get_real_sources(self) -> list[tuple[str, str]]:
        """Return available audio input endpoints. Stub — not used by the GUI yet."""
        return []

    def get_default_sink_name(self) -> str | None:
        """Return the id of the current default output, or None."""
        try:
            sinks = self.get_real_sinks()
            return sinks[0][1] if sinks else None
        except Exception:
            return None

    def set_default_sink(self, name: str) -> None:
        """Changing the default output device is not yet supported on Windows."""
        logger.debug("set_default_sink: not implemented on Windows (%s)", name)

    def set_default_sink_and_move_loopbacks(self, name: str) -> None:
        """No-Op on Windows — loopback routing requires PipeWire."""
        logger.debug("set_default_sink_and_move_loopbacks: not implemented on Windows (%s)", name)

    def get_v_sinks_debug(self) -> list[dict]:
        """Always empty on Windows — no V-Sinks."""
        return []

    def get_active_streams_debug(self) -> dict:
        streams = self.get_active_streams()
        assigned = self._config.get_all_assigned_apps_by_name()
        streams_list = [
            {
                "index": s.index,
                "app_name": s.app_name,
                "pid": s.pid,
                "volume": round(s.volume, 2),
                "muted": s.muted,
                "is_unmapped": s.app_name.lower() not in assigned,
            }
            for s in streams
        ]
        return {
            "backend": "wasapi",
            "active_streams": streams_list,
            "unmapped_summary": [s["app_name"] for s in streams_list if s["is_unmapped"]],
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _do_toggle_mute(self, channel_index: int) -> None:
        """Toggle mute — must be called while holding _state_lock."""
        new_muted = not self._channel_muted.get(channel_index, False)
        self._channel_muted[channel_index] = new_muted
        if new_muted:
            self._muted_at_volume[channel_index] = self._poti_volumes.get(channel_index, 0.5)
        self.mute_state_changed.emit(channel_index, new_muted)
        for app_name in self._config.get_app_names(channel_index):
            self._apply_mute_by_name(app_name, new_muted)
        logger.debug("Channel %d muted=%s", channel_index, new_muted)

    def _apply_channel_volume(self, channel_index: int, volume: float) -> None:
        """Apply volume to all apps on a channel and emit the GUI signal."""
        if self._channel_muted.get(channel_index, False):
            return
        mode = self._config.get_channel_mode(channel_index)
        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index) or ""
            if "system master" in hw_id.lower():
                self._set_system_master_volume(volume)
        else:
            for app_name in self._config.get_app_names(channel_index):
                self._apply_volume_by_name(app_name, volume)
        self.channel_volume_changed.emit(channel_index, volume)

    def _apply_volume_by_name(self, app_name: str, volume: float) -> None:
        """Set volume on all active sessions matching app_name."""
        app_lower = app_name.lower()
        if app_lower == "system master":
            self._set_system_master_volume(volume)
            return
        for session in _get_sessions():
            if _session_name(session).lower() == app_lower:
                _set_session_volume(session, volume)

    def _apply_mute_by_name(self, app_name: str, muted: bool) -> None:
        """Set mute state on all active sessions matching app_name."""
        app_lower = app_name.lower()
        for session in _get_sessions():
            if _session_name(session).lower() == app_lower:
                _set_session_mute(session, muted)

    def _set_system_master_volume(self, volume: float) -> None:
        """Set the default audio endpoint master volume.

        The IAudioEndpointVolume interface is acquired once and cached to avoid
        repeated COM Activate() calls (and the reference-leak risk they carry).
        The cache is invalidated on any error so the next call re-acquires it.
        """
        if self._endpoint_volume is None:
            try:
                from ctypes import cast, POINTER
                from comtypes import CLSCTX_ALL
                from pycaw.api.endpointvolume import IAudioEndpointVolume
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self._endpoint_volume = cast(interface, POINTER(IAudioEndpointVolume))
            except Exception as exc:
                logger.debug("WASAPI: could not acquire IAudioEndpointVolume: %s", exc)
                return
        try:
            self._endpoint_volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, volume)), None)
        except Exception as exc:
            logger.debug("WASAPI system master volume failed: %s", exc)
            self._endpoint_volume = None  # Re-acquire on next call
