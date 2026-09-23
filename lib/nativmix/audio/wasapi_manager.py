"""
Windows WASAPI audio backend for NativMix.

Uses pycaw (Python Core Audio Windows) for per-app volume control via the
Windows Audio Session API (WASAPI).  Equivalent to PipeWireManager on Linux.

Feature differences vs. Linux backend:
  - V-Sinks: not supported — enable_v_sink / disable_v_sink are No-Ops.
  - Session detection: polling (~100 ms); soft-apply volume/mute on new sessions
    (no Linux-style reflex mute — pause/resume would sound like a ramp from 0).
  - Hardware sink control: system master only (via default audio endpoint).
  - Other Apps: same catch-all semantics as Linux (unmapped sessions).
  - Session list: short TTL cache for mute/volume applies (COM GetAllSessions).
  - COM: CoInitialize per calling thread (_ensure_com) so GUI mute/volume works.

Install dependencies:
    pip install "nativmix[windows]"
    # pulls in: pycaw>=20230407, comtypes>=1.2, psutil>=5.9
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot

from nativmix.audio.base import AudioBackendBase, StreamInfo

if TYPE_CHECKING:
    from nativmix.utils.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# Reuse WASAPI session list briefly across mute/volume applies (COM GetAllSessions is costly).
_SESSION_CACHE_TTL_S = 0.15

# Per-thread COM apartment init — mute/volume often run on the GUI thread while
# CoInitialize historically only ran inside the listener QThread.
_com_tls = threading.local()

# ---------------------------------------------------------------------------
# Optional import guard — pycaw / comtypes / psutil are Windows-only
# ---------------------------------------------------------------------------

try:
    import psutil  # noqa: F401 (used inside helper functions)
    from pycaw.utils import AudioUtilities

    _WASAPI_AVAILABLE = True
except ImportError:
    _WASAPI_AVAILABLE = False
    logger.warning('pycaw or psutil not installed — WASAPI backend unavailable. Run: pip install "nativmix[windows]"')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_com() -> bool:
    """Initialize COM for the current thread if needed. Returns True when usable."""
    if getattr(_com_tls, "ready", False):
        return True
    if not _WASAPI_AVAILABLE:
        return False
    try:
        import comtypes

        comtypes.CoInitialize()
        _com_tls.ready = True
        return True
    except Exception as exc:
        # Already initialized on this thread is fine; treat as ready.
        msg = str(exc).lower()
        if "already" in msg or "rpc_e_changed_mode" in msg:
            _com_tls.ready = True
            return True
        logger.warning("WASAPI: CoInitialize failed on thread: %s", exc)
        return False


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
    if not _ensure_com():
        return
    try:
        sv = session.SimpleAudioVolume
        if sv:
            sv.SetMasterVolume(max(0.0, min(1.0, volume)), None)
    except Exception as exc:
        logger.warning("WASAPI SetMasterVolume failed: %s", exc)


def _set_session_mute(session, muted: bool) -> None:
    """Set mute state on a pycaw session's ISimpleAudioVolume."""
    if not _ensure_com():
        return
    try:
        sv = session.SimpleAudioVolume
        if sv:
            sv.SetMute(muted, None)
    except Exception as exc:
        logger.warning("WASAPI SetMute failed: %s", exc)


def _get_sessions() -> list:
    """Return all active pycaw AudioSession objects, or [] on error."""
    if not _WASAPI_AVAILABLE or not _ensure_com():
        return []
    try:
        return AudioUtilities.GetAllSessions()
    except Exception as exc:
        logger.warning("WASAPI GetAllSessions failed: %s", exc)
        return []


def _session_key(session) -> str | None:
    """
    Stable key for one WASAPI session.

    Prefer InstanceIdentifier / Identifier so multiple sessions under the same
    PID (browsers, Electron) are tracked separately. Fall back to pid+name.
    """
    try:
        pid = int(getattr(session, "ProcessId", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid == 0:
        return None
    for attr in ("InstanceIdentifier", "Identifier"):
        try:
            val = getattr(session, attr, None)
            if callable(val):
                val = val()
            if val:
                return f"{attr[0].lower()}:{val}"
        except Exception as exc:
            logger.debug("WASAPI session %s unavailable: %s", attr, exc)
    name = _session_name(session) or "?"
    return f"p:{pid}:{name.lower()}"


# ---------------------------------------------------------------------------
# Background listener thread
# ---------------------------------------------------------------------------


class _WasapiListenerThread(QThread):
    """
    Polls Windows Audio Sessions every 100 ms to detect new / removed streams.

    Emits stream_added for each newly detected session so that
    WasapiManager can apply the Two-Stage Mute-Catch.
    """

    stream_added = pyqtSignal(object)  # StreamInfo
    stream_removed = pyqtSignal(str)  # session key
    audit_finished = pyqtSignal()
    status_changed = pyqtSignal(str, str)  # (status_type, message)

    _POLL_INTERVAL_MS = 100

    def __init__(self, config: ConfigManager, parent: QThread | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._running = False
        # session_key → app_name for currently known sessions
        self._known: dict[str, str] = {}

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True

        # COM must be initialised per-thread on Windows.
        if not _ensure_com():
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

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _initial_audit(self) -> None:
        """Snapshot existing sessions without muting them."""
        for session in _get_sessions():
            key = _session_key(session)
            if not key:
                continue
            name = _session_name(session)
            if name:
                self._known[key] = name
        logger.debug("WASAPI initial audit: %d sessions found", len(self._known))

    def _poll_sessions(self) -> None:
        """Diff current sessions against known ones; emit added/removed signals."""
        sessions = _get_sessions()
        current: dict[str, str] = {}

        for session in sessions:
            key = _session_key(session)
            if not key:
                continue
            name = _session_name(session)
            if not name:
                continue
            current[key] = name

            if key not in self._known:
                self._known[key] = name
                try:
                    pid = int(getattr(session, "ProcessId", 0) or 0)
                except (TypeError, ValueError):
                    pid = 0
                info = StreamInfo(index=abs(hash(key)) % (10**9), app_name=name, pid=pid)
                self.stream_added.emit(info)

        for key in set(self._known) - set(current):
            del self._known[key]
            self.stream_removed.emit(key)


# ---------------------------------------------------------------------------
# WasapiManager
# ---------------------------------------------------------------------------


class WasapiManager(AudioBackendBase):
    """
    Windows audio backend.  Mirrors the public API of PipeWireManager so that
    main.py needs no platform-specific code beyond the instantiation switch.
    """

    mute_state_changed = pyqtSignal(int, bool)
    channel_volume_changed = pyqtSignal(int, float)
    other_apps_changed = pyqtSignal(list)
    audit_finished = pyqtSignal()
    status_changed = pyqtSignal(str, str)

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
        # Cached IAudioEndpointVolume — acquired lazily in _set_system_master_volume
        self._endpoint_volume = None
        # Short-lived session list cache for mute/volume (invalidated on stream churn)
        self._sessions_cache: list[Any] | None = None
        self._sessions_cache_mono: float = 0.0
        self._last_other_apps: list[str] = []

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
        self._thread.start()
        logger.info("WasapiManager started (WASAPI available: %s)", _WASAPI_AVAILABLE)

    def stop(self) -> None:
        """Stop the polling thread and release resources."""
        self._running = False
        if self._thread:
            # Disconnect signals first to prevent slots firing after cleanup
            try:
                self._thread.stream_added.disconnect(self._on_stream_added)
                self._thread.stream_removed.disconnect(self._on_stream_removed)
                self._thread.audit_finished.disconnect(self.audit_finished)
                self._thread.status_changed.disconnect(self.status_changed)
            except RuntimeError:
                pass  # Already disconnected
            self._thread.stop()
            if not self._thread.wait(3000):
                logger.warning("WasapiManager: thread did not stop in time, terminating")
                self._thread.terminate()
                self._thread.wait(1000)
            self._thread.deleteLater()
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
        self.channel_volume_changed.emit(channel_index, volume)

    def is_channel_muted(self, channel_index: int) -> bool:
        """Return True if the mixer channel is currently muted."""
        with self._state_lock:
            return bool(self._channel_muted.get(channel_index, False))

    def toggle_mute(self, channel_index: int) -> None:
        """Toggle the mute state of a channel (IPC hotkey / GUI button)."""
        with self._state_lock:
            self._do_toggle_mute(channel_index)

    # ------------------------------------------------------------------
    # Mapping-changed slot (called by config.mapping_changed signal)
    # ------------------------------------------------------------------

    def on_mapping_changed(self, channel_index: int, app_names: list[str]) -> None:
        """Apply current channel volume to newly mapped apps."""
        with self._state_lock:
            vol = self._poti_volumes.get(channel_index, 0.5)
            muted = self._channel_muted.get(channel_index, False)
        for app_name in app_names:
            self._apply_volume_by_name(app_name, vol)
            self._apply_mute_by_name(app_name, muted)
        self._refresh_other_apps_list()

    # ------------------------------------------------------------------
    # Stream lifecycle (Two-Stage Mute-Catch)
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def _on_stream_added(self, info: StreamInfo) -> None:
        """
        Apply fader volume / channel mute when a WASAPI session appears.

        Unlike Linux PipeWire (Two-Stage Mute-Catch with an immediate reflex
        mute), Windows often recreates sessions on pause/resume (e.g. YouTube).
        A reflex mute there sounds like a ramp from silence to the preset.
        We only soft-apply the desired volume and mute state — still catching
        default 1.0 sessions without the mute dance.
        """
        try:
            self._invalidate_session_cache()
            ch = self._resolve_target_channel(info.app_name)
            if ch is None:
                logger.debug("New unmapped session: %s (pid=%d)", info.app_name, info.pid)
                self._refresh_other_apps_list()
                return

            with self._state_lock:
                vol = self._poti_volumes.get(ch, 0.5)
                muted = self._channel_muted.get(ch, False)
            self._apply_volume_by_name(info.app_name, vol)
            self._apply_mute_by_name(info.app_name, muted)
            logger.debug(
                "WASAPI soft-apply: vol=%.2f muted=%s to %s (ch=%d)",
                vol,
                muted,
                info.app_name,
                ch,
            )
            self._refresh_other_apps_list()
        except Exception:
            logger.exception("_on_stream_added: unhandled exception for %s", info.app_name)

    @pyqtSlot(str)
    def _on_stream_removed(self, session_key: str) -> None:
        logger.debug("WASAPI session removed: %s", session_key)
        self._invalidate_session_cache()
        self._refresh_other_apps_list()

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
            except Exception as exc:
                logger.debug("get_active_streams: could not read session volume/mute: %s", exc)
                vol, muted = 1.0, False
            result.append(
                StreamInfo(
                    index=idx,
                    app_name=name,
                    pid=session.ProcessId,
                    volume=vol,
                    muted=muted,
                )
            )
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

    def _invalidate_session_cache(self) -> None:
        with self._state_lock:
            self._sessions_cache = None

    def _cached_sessions(self) -> list[Any]:
        """Return active WASAPI sessions, reusing a short TTL cache."""
        now = time.monotonic()
        with self._state_lock:
            if self._sessions_cache is not None and (now - self._sessions_cache_mono) < _SESSION_CACHE_TTL_S:
                return self._sessions_cache
        sessions = _get_sessions()
        with self._state_lock:
            self._sessions_cache = sessions
            self._sessions_cache_mono = now
        return sessions

    def _iter_named_sessions(self) -> list[tuple[Any, str]]:
        """Active sessions with a resolvable app name."""
        named: list[tuple[Any, str]] = []
        for session in self._cached_sessions():
            name = _session_name(session)
            if name:
                named.append((session, name))
        return named

    def _get_explicitly_assigned_apps(self) -> set[str]:
        """Lowercased app names mapped to channels, excluding catch-alls."""
        assigned = set(self._config.get_all_assigned_apps_by_name())
        assigned.discard("other apps")
        assigned.discard("system master")
        return assigned

    def _resolve_target_channel(self, app_name: str) -> int | None:
        """
        Map a resolved session name to a channel index.

        Exact profile mappings win. If a channel owns the "Other Apps"
        catch-all, any stream not explicitly assigned (and not System Master)
        maps to that channel — same rule as Linux PipeWireManager.
        """
        direct = self._config.find_channel_for_app(app_name)
        if direct is not None:
            return direct

        name_l = app_name.lower()
        if name_l in ("system master", "other apps"):
            return None

        other_ch: int | None = None
        for ch_idx in range(self._config.num_channels):
            apps = [str(a).lower() for a in self._config.get_app_names(ch_idx)]
            if "other apps" in apps:
                other_ch = ch_idx
                break

        if other_ch is not None and name_l not in self._get_explicitly_assigned_apps():
            return other_ch
        return None

    def _refresh_other_apps_list(self) -> None:
        """Emit other_apps_changed when the unmapped session set changes."""
        assigned = self._config.get_all_assigned_apps_by_name()
        unmapped: list[str] = []
        seen: set[str] = set()
        for _session, name in self._iter_named_sessions():
            key = name.lower()
            if key in assigned or key == "system master" or key in seen:
                continue
            seen.add(key)
            unmapped.append(name)
        unmapped.sort(key=str.lower)
        if unmapped != self._last_other_apps:
            self._last_other_apps = unmapped
            self.other_apps_changed.emit(unmapped)

    def _do_toggle_mute(self, channel_index: int) -> None:
        """Toggle mute — must be called while holding _state_lock."""
        new_muted = not self._channel_muted.get(channel_index, False)
        self._channel_muted[channel_index] = new_muted
        if new_muted:
            self._muted_at_volume[channel_index] = self._poti_volumes.get(channel_index, 0.5)
        self.mute_state_changed.emit(channel_index, new_muted)

        mode = self._config.get_channel_mode(channel_index)
        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index) or ""
            if "system master" in hw_id.lower():
                self._set_system_master_mute(new_muted)
            logger.debug("Channel %d muted=%s (hardware)", channel_index, new_muted)
            return

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
        """Set volume on all active sessions matching app_name (or Other Apps)."""
        app_lower = app_name.lower()
        if app_lower == "system master":
            self._set_system_master_volume(volume)
            return

        other_apps_mode = app_lower == "other apps"
        assigned_apps = self._get_explicitly_assigned_apps() if other_apps_mode else set()

        for session, name in self._iter_named_sessions():
            name_l = name.lower()
            if other_apps_mode:
                if name_l not in assigned_apps and name_l != "system master":
                    _set_session_volume(session, volume)
            elif name_l == app_lower:
                _set_session_volume(session, volume)

    def _apply_mute_by_name(self, app_name: str, muted: bool) -> None:
        """Set mute on all active sessions matching app_name (or System Master / Other Apps)."""
        app_lower = app_name.lower()
        if app_lower == "system master":
            self._set_system_master_mute(muted)
            return

        other_apps_mode = app_lower == "other apps"
        assigned_apps = self._get_explicitly_assigned_apps() if other_apps_mode else set()

        for session, name in self._iter_named_sessions():
            name_l = name.lower()
            if other_apps_mode:
                if name_l not in assigned_apps and name_l != "system master":
                    _set_session_mute(session, muted)
            elif name_l == app_lower:
                _set_session_mute(session, muted)

    def _ensure_endpoint_volume(self):
        """Return cached IAudioEndpointVolume, or None on failure."""
        if not _ensure_com():
            return None
        if self._endpoint_volume is not None:
            return self._endpoint_volume
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.api.endpointvolume import IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._endpoint_volume = cast(interface, POINTER(IAudioEndpointVolume))
            return self._endpoint_volume
        except Exception as exc:
            logger.warning("WASAPI: could not acquire IAudioEndpointVolume: %s", exc)
            self._endpoint_volume = None
            return None

    def _set_system_master_volume(self, volume: float) -> None:
        """Set the default audio endpoint master volume."""
        endpoint = self._ensure_endpoint_volume()
        if endpoint is None:
            return
        try:
            endpoint.SetMasterVolumeLevelScalar(max(0.0, min(1.0, volume)), None)
        except Exception as exc:
            logger.warning("WASAPI system master volume failed: %s", exc)
            self._endpoint_volume = None

    def _set_system_master_mute(self, muted: bool) -> None:
        """Mute/unmute the default audio endpoint (System Master)."""
        endpoint = self._ensure_endpoint_volume()
        if endpoint is None:
            return
        try:
            endpoint.SetMute(1 if muted else 0, None)
        except Exception as exc:
            logger.warning("WASAPI system master mute failed: %s", exc)
            self._endpoint_volume = None
