"""
Linux audio backend: PipeWireManager

Listens to PipeWire/PulseAudio sink-input events via pulsectl and manages
per-stream volume control. Runs inside a QThread to keep the GUI responsive.

Key design: Two-Stage Mute-Catch (Rule 11)
-------------------------------------------
Stage 1 – Reflex (on 'new' event):
    When a new sink_input appears, immediately mute it BEFORE trying to
    identify the application. At this point no metadata is available yet.

Stage 2 – Resolution (on 'change' event):
    When the 'change' event fires for the same index, read application.process.id
    and resolve the real application name. Then apply the correct volume from the
    hardware mapping and unmute the stream.

This prevents the "audio blast" caused by new streams starting at 100% volume
before they can be identified and volume-controlled.

---------------------------------------------------------------------------
Setup & Installation Info (CachyOS / Arch Linux)
---------------------------------------------------------------------------
To test this module or NativMix locally, ensure you have the required 
packages installed. `pulsectl` is strictly required for PipeWire routing.

For local development / testing without AUR:
  sudo pacman -S python-pulsectl python-pyqt6 python-pyserial python-setproctitle

Or using a venv:
  python -m venv .venv && source .venv/bin/activate
  pip install pulsectl PyQt6 pyserial setproctitle
---------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
import subprocess
from typing import Any

import pulsectl
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, pyqtSlot

from nativmix.audio.base import AudioBackendBase, StreamInfo
from nativmix.utils.proc_resolver import resolve_app_name, invalidate_cache
from nativmix.utils.config_manager import ConfigManager
from nativmix.utils import routing

logger = logging.getLogger(__name__)

# Volume applied immediately on 'new' event as a safety floor
_SAFETY_VOLUME: float = 0.0  # fully muted until we know who this stream belongs to


@dataclass
class _PendingStream:
    """Tracks a stream that has been muted but not yet identified."""
    index: int


def _is_internal_stream(proplist: dict[str, str]) -> bool:
    """
    Unified filter for internal, system, or NativMix-managed streams.
    Returns True if the stream should be HIDDEN from both the main list 
    and the "Other Apps" tooltip.
    """
    app_name = proplist.get("application.name", "") or proplist.get("media.name", "")
    media_class = proplist.get("media.class", "").lower()

    # Filter by common system/monitor keywords
    keywords = ["loopback", "monitor", "peak detect", "dummy", "speech-dispatcher", "nativmix"]
    name_lower = app_name.lower()
    
    if any(kd in name_lower for kd in keywords):
        return True
        
    if "monitor" in media_class or "loopback" in media_class:
        return True

    return False


class _AudioListenerThread(QThread):
    """
    Background thread that subscribes to PulseAudio/PipeWire events.

    Signals
    -------
    stream_added(StreamInfo)
        Emitted when a new stream is fully identified and ready to be mapped.
    stream_removed(int)
        Emitted when a stream has been removed (index is passed).
    stream_changed(StreamInfo)
        Emitted when the volume or mute state of a known stream changes.
    """

    stream_added = pyqtSignal(object)    # StreamInfo
    stream_removed = pyqtSignal(int)     # sink_input index
    stream_changed = pyqtSignal(object)  # StreamInfo
    stream_list_changed = pyqtSignal()   # emitted after add or remove (for GUI refresh)
    status_changed = pyqtSignal(str, str)           # (status_type, message)

    # Timeout for pactl subprocess calls inside this thread.
    # Must match PipeWireManager._SUBPROCESS_TIMEOUT — kept in sync manually.
    _SUBPROCESS_TIMEOUT: int = 5

    def __init__(self, config: ConfigManager, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._running = False
        self.daemon = True
        # Thread-safe dictionary to detach from GUI / Config updates
        # Format: { channel_index: {'vol': float, 'v_sink': bool, 'apps': list[str]} }
        self.channel_states: dict[int, dict] = {}
        # Track streams muted by the reflex stage
        self._reflex_muted: set[int] = set()
        # Track streams we have already emitted `stream_added` for
        self._known_streams: set[int] = set()
        self._pulse: pulsectl.Pulse | None = None
        self._resolver: pulsectl.Pulse | None = None  # Second connection for lookups/actions
        # Cooldown: tracks last routing timestamp per sink_input index to
        # suppress duplicate log lines from audit + stream_changed race.
        self._recently_routed: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop running inside the worker thread."""
        self._running = True
        logger.info("AudioListenerThread started")
        self.status_changed.emit("connecting", "Connecting to PipeWire...")

        try:
            # We use TWO connections:
            # 1. 'pulse' for the blocking event_listen loop
            # 2. 'resolver' for all information requests (info) and actions (volume/mute)
            # This prevents SIGSEGV crashes caused by re-entrant calls in the callback thread.
            with pulsectl.Pulse("nativmix-listener") as pulse, \
                 pulsectl.Pulse("nativmix-resolver") as resolver:

                self._pulse = pulse
                self._resolver = resolver

                # 1. Fetch currently existing streams
                try:
                    for si in resolver.sink_input_list():
                        if _is_internal_stream(dict(si.proplist)):
                            continue

                        # ANTI-BLAST for existing streams (Rule 11/18):
                        # Mute immediately to prevent start-up spikes before mapping is applied.
                        try:
                            # Use pactl for non-blocking execution
                            subprocess.run(
                                ["pactl", "set-sink-input-mute", str(si.index), "1"],
                                capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
                            )
                            self._reflex_muted.add(si.index)
                        except Exception:
                            pass

                        info = self._build_stream_info(si)
                        if info:
                            # Auto-sync on startup
                            self._apply_auto_reconnect(resolver, info)
                            self.stream_added.emit(info)

                            # Reflex Unmute for startup streams
                            if si.index in self._reflex_muted:
                                try:
                                    resolver.sink_input_mute(si.index, mute=False)
                                    self._reflex_muted.remove(si.index)
                                except pulsectl.PulseError:
                                    pass
                except pulsectl.PulseError as exc:
                    logger.warning("Could not list initial streams: %s", exc)

                # 2. Subscribe to future events
                pulse.event_mask_set("sink_input", "sink")
                pulse.event_callback_set(self._on_event)

                self.status_changed.emit("stable", "PipeWire connected")
                logger.info("Listening for PulseAudio sink_input events …")
                # Block and process events in 100ms chunks
                while self._running:
                    try:
                        pulse.event_listen(timeout=0.1)
                    except pulsectl.PulseLoopStop:
                        break
                    except Exception as e:
                        logger.error("Error in pulse event_listen: %s", e)
                        time.sleep(0.1)

            logger.info("AudioListenerThread finished cleanly")

        except pulsectl.PulseError as exc:
            logger.error("PulseAudio connection error: %s", exc)
            self.status_changed.emit("error_temporary", str(exc))
        except Exception as exc:
            logger.exception("Unhandled crash in AudioListenerThread")
            self.status_changed.emit("error_critical", str(exc))

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to finish (with timeout)."""
        self._running = False
        if not self.wait(2000):
            logger.warning("AudioListenerThread did not stop in time, terminating...")
            self.terminate()
            # Strategy B: bounded wait after terminate() so libpulse blocked on
            # a dead PipeWire socket during system shutdown cannot hang forever.
            if not self.wait(1000):
                logger.error("AudioListenerThread still alive after terminate — abandoning")

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_event(self, event: pulsectl.PulseEventInfo) -> None:
        """Called by pulsectl for every subscribed event."""
        if not self._pulse:
            return

        try:
            if event.facility == pulsectl.PulseEventFacilityEnum.sink_input:
                if event.t == pulsectl.PulseEventTypeEnum.new:
                    # Stage 1: Reflex - Mute immediately (Rule 11)
                    # We use 'pactl' because it is external and 100% safe from Pulse thread locks.
                    try:
                        subprocess.run(
                            ["pactl", "set-sink-input-mute", str(event.index), "1"],
                            capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
                        )
                        self._reflex_muted.add(event.index)
                    except Exception as e:
                        logger.debug("Reflex mute failed for index %d: %s", event.index, e)
                            
                elif event.t == pulsectl.PulseEventTypeEnum.change:
                    # Stage 2: Resolution - Resolve and Reconnect
                    # We MUST use the separate 'resolver' connection here.
                    if not self._resolver: return
                    try:
                        si = self._resolver.sink_input_info(event.index)
                        if si and not isinstance(si, int):
                            props = getattr(si, "proplist", {})
                            if not hasattr(props, "get") and not isinstance(props, dict):
                                props = {}
                            if _is_internal_stream(dict(props)):
                                # Ensure we clean up any reflex mute even for internal streams
                                if event.index in self._reflex_muted:
                                    try:
                                        self._resolver.sink_input_mute(event.index, mute=False)
                                        self._reflex_muted.remove(event.index)
                                    except pulsectl.PulseError:
                                        pass
                                return
                        else:
                            # Cleanup reflex mute even if we can't get info (e.g. vanished)
                            if event.index in self._reflex_muted:
                                self._reflex_muted.remove(event.index)
                            return
                    except (pulsectl.PulseIndexError, pulsectl.PulseError):
                        if event.index in self._reflex_muted:
                            self._reflex_muted.remove(event.index)
                        return
                            
                    info = self._build_stream_info(si)
                    if info:
                        # PERSISTENCE / AUTO-RECONNECT (using resolver)
                        self._apply_auto_reconnect(self._resolver, info)
                        
                        # Reflex Unmute
                        if event.index in self._reflex_muted:
                            try:
                                self._resolver.sink_input_mute(event.index, mute=False)
                            except pulsectl.PulseError:
                                pass
                            self._reflex_muted.remove(event.index)
                            
                        self.stream_changed.emit(info)
                        if event.index not in self._known_streams:
                            self._known_streams.add(event.index)
                            self.stream_added.emit(info)

                elif event.t == pulsectl.PulseEventTypeEnum.remove:
                    if event.index in self._known_streams:
                        self._known_streams.remove(event.index)
                    if event.index in self._reflex_muted:
                        self._reflex_muted.remove(event.index)
                    self.stream_removed.emit(event.index)

            elif event.facility == pulsectl.PulseEventFacilityEnum.sink:
                pass  # Sink volume/hotplug is polled by SinkPollThread — no IPC here
                
        except Exception as e:
            logger.error("Listener Error: %s", e)

    def _apply_auto_reconnect(self, pulse: pulsectl.Pulse, info: StreamInfo) -> None:
        """Apply volume and V-Sink routing based on persistence config."""
        # 1. Check if app is assigned to any channel
        target_ch = self._config.find_channel_for_app(info.app_name)
        if target_ch is None:
            return

        # 2. Get state (from cache or config)
        state = self.channel_states.get(target_ch, {})
        vol = state.get("vol", self._config.get_channel_volume(target_ch))
        vsink_enabled = state.get("v_sink", self._config.is_v_sink_enabled(target_ch))

        try:
            if vsink_enabled:
                v_sink_name = f"NativMix_CH_{target_ch}"
                try:
                    v_sink = pulse.get_sink_by_name(v_sink_name)
                except pulsectl.PulseError:
                    v_sink = None

                if not v_sink:
                    if state.get("v_sink_busy", False):
                        logger.debug("V-Sink %s being (re)created — reconnect deferred", v_sink_name)
                    else:
                        logger.warning("V-Sink %s not found for reconnect", v_sink_name)
                    return

                if info.props.get("sink_name") != v_sink_name:
                    now = time.monotonic()
                    last = self._recently_routed.get(info.index)
                    if last is not None and now - last < 2.0:
                        logger.debug(
                            "Routing %s skipped – cooldown active (%.0fms ago)",
                            info.app_name, (now - last) * 1000,
                        )
                    else:
                        # Prune stale entries (> 10 s) to keep the dict small
                        self._recently_routed = {
                            k: v for k, v in self._recently_routed.items()
                            if now - v < 10.0
                        }
                        self._recently_routed[info.index] = now
                        logger.debug("Routing %s into V-Sink %s", info.app_name, v_sink_name)
                        # Use pactl as it is more robust for moving streams across backends
                        try:
                            subprocess.run(
                                ["pactl", "move-sink-input", str(info.index), v_sink_name],
                                capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
                            )
                        except subprocess.TimeoutExpired:
                            logger.warning("pactl move-sink-input timed out after %ds (stream %d -> %s)",
                                           self._SUBPROCESS_TIMEOUT, info.index, v_sink_name)

                        # Robust pulsectl call: fetch fresh info object and VALIDATE type
                        try:
                            si_fresh = pulse.sink_input_info(info.index)
                            if si_fresh and not isinstance(si_fresh, int):
                                pulse.volume_set_all_chans(si_fresh, 1.0) # Unity gain inside V-Sink
                            else:
                                # If si_fresh is 200 (int) or None, we cannot resolve metadata right now
                                logger.info("Received status ID (%s) instead of metadata object for %s, skipping volume sync",
                                            si_fresh, info.app_name)
                        except (pulsectl.PulseError, TypeError, ValueError) as e:
                            logger.debug("Minor: Could not update volume after move (stream may have closed): %s", e)
            else:
                try:
                    si_fresh = pulse.sink_input_info(info.index)
                    if si_fresh and not isinstance(si_fresh, int):
                        pulse.volume_set_all_chans(si_fresh, vol)
                    else:
                        logger.info("Received status ID (%s) instead of metadata object for %s, skipping volume sync", 
                                    si_fresh, info.app_name)
                except (pulsectl.PulseError, TypeError, ValueError) as e:
                    logger.debug("Minor: Could not apply volume (stream may have closed): %s", e)
        except Exception as e:
            logger.error("Auto-reconnect process error for %s: %s", info.app_name, e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_stream_info(sink_input: Any) -> StreamInfo | None:
        """
        Convert a pulsectl PulseSinkInputInfo into our StreamInfo dataclass.
        Returns None if sink_input is invalid (e.g. an integer status code).
        """
        if sink_input is None or isinstance(sink_input, int):
            # logger.debug("Cannot build StreamInfo: sink_input is %s", type(sink_input))
            return None

        # Robust Metadata Parsing (Fix for "Firefox-Bug")
        # Ensure proplist is actually a dictionary-like object.
        raw_props = getattr(sink_input, "proplist", {})
        if not hasattr(raw_props, "get") and not isinstance(raw_props, dict):
            logger.warning("Stream %d has invalid metadata (proplist type: %s)", 
                           sink_input.index, type(raw_props))
            props = {}
        else:
            props = dict(raw_props)

        pid_str = str(props.get("application.process.id", "0"))
        try:
            pid = int(pid_str)
        except (ValueError, TypeError):
            pid = 0

        # Determine a pa-level fallback in case /proc is unavailable (e.g. containers)
        pa_fallback = (
            str(props.get("application.name", ""))
            or str(props.get("application.process.binary", ""))
            or str(props.get("media.name", ""))
            or "Unknown"
        )

        # Full /proc-based resolution with Electron/Chromium hack
        app_name = resolve_app_name(pid, fallback=pa_fallback)

        # Retrieve volume: take the average across all channels
        volume_values = sink_input.volume.values
        avg_volume = sum(volume_values) / len(volume_values) if volume_values else 0.0

        return StreamInfo(
            index=sink_input.index,
            app_name=app_name,
            pid=pid,
            volume=avg_volume,
            muted=bool(sink_input.mute),
            props=props,
        )


class SinkPollThread(QThread):
    """
    Polls the PipeWire default sink every 250 ms from a dedicated background thread.

    This replaces the previous approach of doing server_info() / get_sink_by_name()
    directly inside the PipeWire event callback (_AudioListenerThread._on_event),
    which caused deadlocks and CPU spikes during event storms (e.g. external
    system volume changes).

    Signals
    -------
    master_volume_changed(float, bool)
        Emitted whenever the default sink's volume or mute state changes.
    default_sink_changed(str)
        Emitted whenever the default sink name changes (hotplug / device switch).
    """

    master_volume_changed = pyqtSignal(float, bool)
    default_sink_changed = pyqtSignal(str)

    _POLL_INTERVAL: float = 0.25  # seconds

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._running = False

    def run(self) -> None:
        self._running = True
        _last_sink_name: str | None = None
        _last_vol: float | None = None
        _last_mute: bool | None = None

        while self._running:
            try:
                with pulsectl.Pulse("nativmix-sink-poll") as pulse:
                    while self._running:
                        try:
                            info = pulse.server_info()
                            name = info.default_sink_name
                            if name != _last_sink_name:
                                _last_sink_name = name
                                self.default_sink_changed.emit(name)
                            sink = pulse.get_sink_by_name(name)
                            vol = sink.volume.value_flat
                            mute = bool(sink.mute)
                            if vol != _last_vol or mute != _last_mute:
                                _last_vol = vol
                                _last_mute = mute
                                self.master_volume_changed.emit(vol, mute)
                        except pulsectl.PulseError:
                            pass
                        # Interruptible sleep: 5 × 50 ms so stop() wakes us promptly
                        for _ in range(5):
                            if not self._running:
                                break
                            time.sleep(self._POLL_INTERVAL / 5)
            except Exception:
                # PipeWire restarted or connection failed — retry after 1 s
                for _ in range(10):
                    if not self._running:
                        break
                    time.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)
        if self.isRunning():
            self.terminate()
            self.wait(500)


class PipeWireManager(AudioBackendBase):
    """
    Linux audio backend using pulsectl to control PipeWire/PulseAudio.

    Manages the listener thread and exposes volume/mute control methods
    that are safe to call from the main (GUI) thread.
    """

    mute_state_changed = pyqtSignal(int, bool)
    channel_volume_changed = pyqtSignal(int, float)
    other_apps_changed = pyqtSignal(list)
    audit_finished = pyqtSignal()
    status_changed = pyqtSignal(str, str)  # (status_type, message) — forwarded from _AudioListenerThread

    _BACKOFF_BASE: float = 2.0
    _BACKOFF_MAX: float = 60.0
    # Timeout (seconds) for every pactl subprocess call.  If PipeWire hangs,
    # the call raises subprocess.TimeoutExpired instead of blocking forever.
    _SUBPROCESS_TIMEOUT: int = 5

    def __init__(self, config: ConfigManager | None = None, parent=None) -> None:
        super().__init__(parent)
        self._config: ConfigManager = config if config is not None else ConfigManager()
        self._thread: _AudioListenerThread | None = None
        self._running: bool = False
        self._restart_count: int = 0
        # Latest poti volumes received from ArduinoThread: channel → volume
        self._poti_volumes: dict[int, float] = {}
        # Currently active audio streams, keyed by sink_input index
        self._active_streams: dict[int, StreamInfo] = {}
        # Stores the explicit mute state per channel (from IPC hotkeys)
        self._channel_muted: dict[int, bool] = {}
        # Stores the physical slider volume at the moment the channel was muted
        self._muted_at_volume: dict[int, float] = {}
        # Previous app name lists per channel, used to diff add/remove events
        self._prev_app_names: dict[int, list[str]] = {}
        # Track hardware sink for hotplug to avoid redundant re-links
        self._last_hardware_sink: str | None = None
        # Guard: hotplug re-links are only allowed after the initial audit has
        # settled.  Set to True 2 s after audit_finished to absorb any sink
        # events that PipeWire emits as a side-effect of the audit itself.
        self._initial_audit_complete: bool = False
        # Reentrant lock protecting shared mutable dicts (_poti_volumes,
        # _channel_muted, _active_streams).  RLock is required because
        # apply_poti_volumes / apply_midi_volumes call toggle_mute, which
        # also acquires the lock on the same thread.
        self._state_lock = threading.RLock()
        # Channels whose V-Sink is currently being created. Slider volume
        # updates are suppressed for these channels until creation completes
        # to prevent stray writes hitting the system sink instead of the V-Sink.
        self._vsink_creating: set[int] = set()
        # Sink poller: polls default sink volume/name from a dedicated thread
        # instead of doing blocking IPC inside the PipeWire event callback.
        self._sink_poll_thread = SinkPollThread()

    # ------------------------------------------------------------------
    # Public stream access (for GUI)
    # ------------------------------------------------------------------

    def is_valid_app_stream(self, stream_info: StreamInfo | None) -> bool:
        """
        Standardized filter for manageable apps.
        Returns True if the app is valid/controllable.
        Returns False if it's an internal loopback, monitor, or system stream.
        """
        if stream_info is None:
            return False

        # 1. Base filter via proplist
        if _is_internal_stream(stream_info.props):
            return False

        # 2. Add-on check: Is it perhaps a NativMix virtual sink being listed?
        # (Though _is_internal_stream should catch this via keywords, we are extra safe here)
        if stream_info.app_name.lower() == "nativmix":
            return False

        return True

    def get_active_streams(self) -> list[StreamInfo]:
        """
        Return a snapshot of all currently active audio streams.
        """
        result: list[StreamInfo] = []
        try:
            with pulsectl.Pulse("nativmix-lister") as pulse:
                assigned_apps = self._config.get_all_assigned_apps_by_name()
                unmapped_found = []

                for si in pulse.sink_input_list():
                    info = _AudioListenerThread._build_stream_info(si)
                    
                    if not self.is_valid_app_stream(info):
                        continue

                    result.append(info)
                    with self._state_lock:
                        self._active_streams[si.index] = info

                    # Check if unmapped (for Tooltip)
                    res_low = info.app_name.lower()
                    if res_low not in assigned_apps and res_low != "system master":
                        if info.app_name not in unmapped_found:
                            unmapped_found.append(info.app_name)

                # Emit unmapped apps list for the "Other Apps" tooltip
                unmapped_found.sort()
                if getattr(self, "_last_other_apps", None) != unmapped_found:
                    self._last_other_apps = unmapped_found
                    self.other_apps_changed.emit(unmapped_found)

        except pulsectl.PulseError as exc:
            logger.error("Failed to list active streams: %s", exc)
            with self._state_lock:
                return list(self._active_streams.values())

        return result

    def get_v_sinks_debug(self) -> list[dict[str, Any]]:
        """Return detailed info about NativMix V-Sinks for CLI debugging."""
        results = []
        try:
            with pulsectl.Pulse("nativmix-debug-sinks") as pulse:
                sinks = pulse.sink_list()
                for ch in range(self._config.num_channels):
                    name = f"NativMix_CH_{ch}"
                    sink = next((s for s in sinks if s.name == name), None)
                    if sink:
                        results.append({
                            "channel": ch,
                            "name": sink.name,
                            "index": sink.index,
                            "volume": round(sum(sink.volume.values) / len(sink.volume.values), 2),
                            "muted": bool(sink.mute),
                            "description": sink.description
                        })
        except Exception as e:
            logger.error("Debug sinks failed: %s", e)
        return results

    def get_active_streams_debug(self) -> dict[str, Any]:
        """Return comprehensive info about detected apps and config for CLI debugging."""
        try:
            active = self.get_active_streams()
            assigned_apps = self._get_all_assigned_apps()
            
            # 1. Total active streams
            streams_list = []
            for info in active:
                streams_list.append({
                    "index": info.index,
                    "app_name": info.app_name,
                    "pid": info.pid,
                    "volume": round(info.volume, 2),
                    "muted": info.muted,
                    "binary": info.props.get("application.process.binary", "N/A"),
                    "class": info.props.get("media.class", "N/A"),
                    "is_unmapped": (info.app_name.lower() not in assigned_apps and info.app_name.lower() != "system master")
                })

            # 2. Configured apps vs Running status
            config_report = {}
            active_names = {s.app_name.lower() for s in active}
            for ch in range(self._config.num_channels):
                apps = self._config.get_app_names(ch)
                if not apps: continue
                
                ch_apps = []
                for a in apps:
                    ch_apps.append({
                        "name": a,
                        "is_running": (a.lower() in active_names or a.lower() == "system master")
                    })
                config_report[f"Channel_{ch}"] = ch_apps

            return {
                "active_streams": streams_list,
                "configured_channels": config_report,
                "unmapped_summary": [s["app_name"] for s in streams_list if s["is_unmapped"]]
            }
        except Exception as e:
            logger.error("Debug apps failed: %s", e)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Public API (AudioBackendBase)
    # ------------------------------------------------------------------

    def _update_thread_states(self) -> None:
        """Pushes current config/volumes to the Listener thread safely."""
        if not self._thread:
            return

        with self._state_lock:
            states = {
                ch: {
                    'vol': self._poti_volumes.get(ch, 0.5),
                    'v_sink': self._config.is_v_sink_enabled(ch),
                    'v_sink_busy': ch in self._vsink_creating,
                    'apps': self._config.get_app_names(ch),
                    'mode': self._config.get_channel_mode(ch),
                }
                for ch in range(self._config.num_channels)
            }
        self._thread.channel_states = states

    def start(self) -> None:
        """Start the background audio event listener thread."""
        if self._thread is not None and self._thread.isRunning():
            logger.warning("PipeWireManager.start() called but thread is already running")
            return

        self._running = True

        # Pre-populate _prev_app_names so the first mapping change doesn't
        # incorrectly treat all configured apps as "newly added".
        for ch in range(self._config.num_channels):
            self._prev_app_names[ch] = list(self._config.get_app_names(ch))

        self._thread = _AudioListenerThread(config=self._config)
        self._wire_thread_signals(self._thread)
        self._thread.start()
        self._update_thread_states()  # Initial push of states

        self._sink_poll_thread.master_volume_changed.connect(self._on_master_volume_changed)
        self._sink_poll_thread.default_sink_changed.connect(self._on_default_sink_changed)
        self._sink_poll_thread.start()

        # Audit and fix loopbacks / apps routing (replaces _adopt_existing_v_sinks)
        self.perform_initial_audio_audit()
        logger.info("PipeWireManager started")

    def _wire_thread_signals(self, thread: _AudioListenerThread) -> None:
        """Connect all signals from a listener thread to our slots."""
        thread.stream_added.connect(self._on_stream_added)
        thread.stream_removed.connect(self._on_stream_removed)
        thread.stream_changed.connect(self._on_stream_changed)
        thread.status_changed.connect(self._on_thread_status_changed)
        thread.finished.connect(self._on_thread_finished)

    def _unwire_thread_signals(self, thread: _AudioListenerThread) -> None:
        """
        Disconnect all signals from a finished listener thread.

        Must be called before replacing self._thread to prevent duplicate
        slot invocations after a restart.  RuntimeError is silently ignored
        because pulsectl may have already cleaned up the Qt object.
        """
        try:
            thread.stream_added.disconnect(self._on_stream_added)
            thread.stream_removed.disconnect(self._on_stream_removed)
            thread.stream_changed.disconnect(self._on_stream_changed)
            thread.status_changed.disconnect(self._on_thread_status_changed)
            thread.finished.disconnect(self._on_thread_finished)
        except RuntimeError:
            pass  # Signal already disconnected — safe to ignore

    @pyqtSlot(str, str)
    def _on_thread_status_changed(self, status_type: str, message: str) -> None:
        """Forward status from the listener thread and reset restart counter on stable."""
        if status_type == "stable":
            self._restart_count = 0
        self.status_changed.emit(status_type, message)

    @pyqtSlot()
    def _on_thread_finished(self) -> None:
        """Restart the listener thread with exponential backoff if not intentionally stopped."""
        if not self._running:
            return  # intentional stop — do not restart

        wait = min(self._BACKOFF_BASE * (2 ** self._restart_count), self._BACKOFF_MAX)
        self._restart_count += 1
        logger.warning(
            "AudioListenerThread exited unexpectedly — restarting in %.0fs (attempt %d)",
            wait, self._restart_count,
        )
        self.status_changed.emit(
            "error_temporary",
            f"PipeWire lost — reconnecting in {wait:.0f}s...",
        )
        QTimer.singleShot(int(wait * 1000), self._restart_thread)

    @pyqtSlot()
    def _restart_thread(self) -> None:
        if not self._running:
            return
        logger.info("Restarting AudioListenerThread (attempt %d)", self._restart_count)

        # Disconnect the finished thread's signals BEFORE replacing the reference.
        # Without this, the old Qt object (kept alive as a child of PipeWireManager)
        # would retain live connections, causing duplicate slot invocations on the
        # next restart cycle.
        if self._thread is not None:
            self._unwire_thread_signals(self._thread)
            self._thread.deleteLater()

        self._thread = _AudioListenerThread(self._config)
        self._wire_thread_signals(self._thread)
        self._update_thread_states()
        self._thread.start()

    def stop(self) -> None:
        """Stop the listener thread gracefully."""
        self._running = False
        self._sink_poll_thread.stop()
        if self._thread is not None:
            # Disconnect first so no signals fire during or after stop().
            self._unwire_thread_signals(self._thread)
            self._thread.stop()
            self._thread.deleteLater()
            self._thread = None
        logger.debug("PipeWireManager stopped")

    def _check_tools(self) -> dict[str, bool]:
        """Check availability of required system tools (pactl, pw-link)."""
        tools = {"pactl": False, "pw-link": False}
        for tool in tools:
            try:
                subprocess.run(["which", tool], capture_output=True, check=True,
                               timeout=self._SUBPROCESS_TIMEOUT)
                tools[tool] = True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        return tools

    def perform_initial_audio_audit(self) -> None:
        """
        1. Auto-Correction on Startup: Check all running apps and route them.
        2. Sink-to-Device Verification: Ensure all V-Sinks have valid loopbacks.
        3. Forced Re-Link: Refresh links to ensure audio is audible.
        """
        logger.debug("Performing critical audio audit & V-Sink re-validation...")
        tools = self._check_tools()
        if not tools["pactl"]:
            logger.error("CRITICAL: 'pactl' not found! Audio routing may fail.")
            # We continue anyway and try with pulsectl, but pactl is preferred for re-links

        try:
            with pulsectl.Pulse("nativmix-audit") as pulse:
                # 1. Map current modules for lookup
                modules = pulse.module_list()
                loopbacks = [m for m in modules if m.name == "module-loopback"]
                null_sinks = [m for m in modules if m.name == "module-null-sink"]

                # 0. Identify the real hardware sink FIRST (to restore it later).
                # Prime _last_hardware_sink so the first default_sink_changed event
                # from _restore_hardware_default_sink (end of audit) does not
                # incorrectly trigger a Hotplug re-link.
                hw_sink_node = self._get_master_hardware_sink(pulse)
                self._last_hardware_sink = hw_sink_node

                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        sink_name = f"NativMix_CH_{ch}"
                        logger.debug("Re-validating V-Sink: %s", sink_name)

                        # A. Ensure Sink exists & Metadata is current (Non-Destructive Reuse)
                        self.enable_v_sink(ch)
                        continue

                # D. Ensure the hardware remains the default sink (OSD BYPASS)
                self._restore_hardware_default_sink(pulse)
        except pulsectl.PulseError as exc:
            logger.error("Initial audio audit failed (V-Sink verification): %s", exc)
            
        # 4. Trigger "move-sink-input" for all mapped apps to force refresh
        self._sync_v_sink_routing()
        
        # 5. Signal completion of the audit
        self.audit_finished.emit()
        logger.debug("Audio audit completed.")
        # Allow hotplug handling only after a 2 s settling window.
        # PipeWire emits multiple sink-change events during V-Sink creation and
        # _restore_hardware_default_sink; the cooldown prevents those from
        # triggering a premature re-link.
        QTimer.singleShot(2000, self._mark_audit_complete)

    def set_volume(self, stream_index: int, volume: float) -> None:
        """
        Set the linear volume [0.0–1.0] for a specific sink input.

        Safe to call from the main thread; opens its own short-lived
        Pulse connection so we don't share state with the listener thread.
        """
        volume = max(0.0, min(1.0, volume))
        try:
            with pulsectl.Pulse("nativmix-volume-setter") as pulse:
                inputs = pulse.sink_input_list()
                target = next((si for si in inputs if si.index == stream_index), None)
                if target is not None:
                    pulse.volume_set_all_chans(target, volume)
        except pulsectl.PulseError as exc:
            logger.error("set_volume(%d, %.2f) failed: %s", stream_index, volume, exc)

    def set_mute(self, stream_index: int, muted: bool) -> None:
        """Toggle the mute state of a specific sink input."""
        try:
            with pulsectl.Pulse("nativmix-mute-setter") as pulse:
                pulse.sink_input_mute(stream_index, mute=muted)
        except pulsectl.PulseError as exc:
            logger.error("set_mute(%d, %s) failed: %s", stream_index, muted, exc)

    # ------------------------------------------------------------------
    # Signal handlers (called on the main/GUI thread by Qt's signal dispatch)
    # ------------------------------------------------------------------

    def _on_stream_added(self, info: StreamInfo) -> None:
        """Slot: track stream."""
        with self._state_lock:
            self._active_streams[info.index] = info
        logger.debug("Stream added: [%d] %s (pid=%d, vol=%.2f)", info.index, info.app_name, info.pid, info.volume)
        # Recalculate unmapped apps for tooltips
        self.get_active_streams()

    def _on_stream_removed(self, index: int) -> None:
        """Slot: remove stream and clear cache."""
        with self._state_lock:
            self._active_streams.pop(index, None)
        invalidate_cache()
        logger.debug("Stream removed: [%d]", index)
        # Recalculate unmapped apps for tooltips
        self.get_active_streams()

    def _on_stream_changed(self, info: StreamInfo) -> None:
        """Slot: update cached stream info on change."""
        self._active_streams[info.index] = info
        logger.debug("Stream changed: [%d] %s vol=%.2f muted=%s", info.index, info.app_name, info.volume, info.muted)

    def _on_mapping_changed(self, channel_index: int, app_names: list[str]) -> None:
        """
        Slot: called when the GUI updates a channel mapping via ConfigManager.

        Invalidates the PID cache so the new mapping takes effect immediately
        for already running streams without delay.
        """
        old_names: set[str] = {n.lower() for n in self._prev_app_names.get(channel_index, [])}
        new_names: set[str] = {n.lower() for n in app_names}
        
        removed = old_names - new_names
        added = new_names - old_names
        
        # Store current state for next diff
        self._prev_app_names[channel_index] = list(app_names)
        
        # Clear cache so we rescan and pick up the new app assignment instantly
        invalidate_cache()

        current_volume = self._poti_volumes.get(channel_index, 0.5)
        logger.debug(
            "Mapping changed: channel %d → %s (applying vol=%.2f)",
            channel_index, app_names, current_volume,
        )
        
        # ── CRITICAL: update thread state FIRST ──────────────────────────────
        # The listener thread reacts to PipeWire change events.
        # If we move a stream first and update thread state later, the listener
        # sees the old mapping (app still mapped to V-Sink channel) on the
        # change event triggered by our own move, and immediately moves the
        # stream BACK into the V-Sink.  Pushing the new state before the move
        # prevents this race condition.
        self._update_thread_states()

        v_sink_enabled = self._config.is_v_sink_enabled(channel_index)
        sink_name = f"NativMix_CH_{channel_index}"
        # Handle explicitly removed apps: evacuate from any NativMix sink
        if removed:
            try:
                with pulsectl.Pulse("nativmix-evac-removed") as pulse:
                    default_sink_name = pulse.server_info().default_sink_name
                    try:
                        default_sink = pulse.get_sink_by_name(default_sink_name)
                    except pulsectl.PulseError:
                        default_sink = None

                    # Safety: never evacuate back into another NativMix V-Sink
                    if default_sink and default_sink.name.startswith("NativMix_"):
                        for s in pulse.sink_list():
                            if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                                default_sink = s
                                break

                    if default_sink:
                        # Scan ALL sinks (not just the current V-Sink) – the
                        # app may still be in *any* NativMix virtual sink.
                        nativmix_sink_indices = {
                            s.index for s in pulse.sink_list()
                            if s.name.startswith("NativMix_")
                        }
                        for si in pulse.sink_input_list():
                            if si.sink not in nativmix_sink_indices:
                                continue  # Not in any V-Sink, nothing to do

                            props = dict(si.proplist)
                            pid_str = props.get("application.process.id", "0")
                            try:
                                pid = int(pid_str)
                            except ValueError:
                                pid = 0
                            pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                            resolved = resolve_app_name(pid, fallback=pa_fallback)
                            if resolved.lower() not in removed:
                                continue

                            logger.debug(
                                "App '%s' removed from CH%d – evacuating to '%s'",
                                resolved, channel_index, default_sink.name
                            )

                            # _seamless_move: volume on old sink → pactl move → unmute
                            try:
                                pulse.volume_set_all_chans(si, current_volume)
                            except pulsectl.PulseError:
                                pass
                            self._seamless_move(pulse, si.index, default_sink.index, volume=None)
                            logger.debug(
                                "Stream %d moved back to Main Sink (vol=%.2f).",
                                si.index, current_volume
                            )
            except pulsectl.PulseError as exc:
                logger.error("Failed to evacuate removed apps from V-Sink %s: %s", sink_name, exc)



                
        # Handle explicitly added apps: if V-Sink is on, route them into it
        if v_sink_enabled and added:
            try:
                with pulsectl.Pulse("nativmix-route-added") as pulse:
                    try:
                        target_sink = pulse.get_sink_by_name(sink_name)
                    except pulsectl.PulseError:
                        target_sink = None
                        
                    if target_sink:
                        for si in pulse.sink_input_list():
                            if si.sink == target_sink.index:
                                continue  # Already in the V-Sink
                            props = dict(si.proplist)
                            pid_str = props.get("application.process.id", "0")
                            try:
                                pid = int(pid_str)
                            except ValueError:
                                pid = 0
                            pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                            resolved = resolve_app_name(pid, fallback=pa_fallback)
                            if resolved.lower() not in added:
                                continue

                            logger.debug(
                                "App '%s' added to CH%d – routing into V-Sink '%s'",
                                resolved, channel_index, sink_name
                            )
                            # Move via pactl first, then set Unity Gain.
                            # Setting volume before the move would affect the old sink.
                            try:
                                result = subprocess.run(
                                    ["pactl", "move-sink-input", str(si.index), str(target_sink.index)],
                                    capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
                                )
                                if result.returncode != 0:
                                    logger.warning(
                                        "pactl move-sink-input to V-Sink failed (rc=%d): %s",
                                        result.returncode, result.stderr.decode(errors="replace").strip(),
                                    )
                            except subprocess.TimeoutExpired:
                                logger.warning("pactl move-sink-input timed out after %ds (stream %d)",
                                               self._SUBPROCESS_TIMEOUT, si.index)

                            # Apply unity gain AFTER the stream is on the V-Sink
                            try:
                                si_fresh = next(
                                    (s for s in pulse.sink_input_list() if s.index == si.index),
                                    None
                                )
                                if si_fresh is not None:
                                    pulse.volume_set_all_chans(si_fresh, 1.0)
                            except pulsectl.PulseError:
                                pass
            except pulsectl.PulseError as exc:
                logger.error("Failed to route added apps into V-Sink %s: %s", sink_name, exc)


        # Apply volumes for still-mapped apps (apps that were neither added nor removed)
        for name in app_names:
            self._apply_volume_by_name(name, current_volume)

        # Do NOT call _sync_v_sink_routing() here: it would re-process every
        # stream and may double-move or un-cork streams that are mid-transition.
        self._update_thread_states()

    def _sync_v_sink_routing(self) -> None:
        """
        Scan all active sink_inputs.
        If an app is in an active V-Sink channel, ensure it is routed there.
        If an app is in a V-Sink but no longer mapped to a V-Sink channel, evacuate it to default.
        """
        try:
            with pulsectl.Pulse("nativmix-vsink-sync") as pulse:
                default_sink_name = pulse.server_info().default_sink_name
                try:
                    default_sink = pulse.get_sink_by_name(default_sink_name)
                except pulsectl.PulseError:
                    return

                # Map of configured V-Sink channels to their sink index (if they exist)
                v_sinks: dict[int, int] = {} 
                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        try:
                            s = pulse.get_sink_by_name(f"NativMix_CH_{ch}")
                            v_sinks[ch] = s.index
                        except pulsectl.PulseError:
                            pass
                
                # Active V-Sink indices
                active_v_sink_indices = set(v_sinks.values())
                
                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                    resolved = resolve_app_name(pid, fallback=pa_fallback)
                    
                    target_ch = self._config.find_channel_for_app(resolved)
                    # Ignore channels in hardware mode
                    if target_ch is not None and self._config.get_channel_mode(target_ch) == "hardware":
                        target_ch = None
                        
                    # Case A: App mapped to a V-Sink channel
                    if target_ch is not None and target_ch in v_sinks:
                        target_sink_index = v_sinks[target_ch]
                        if si.sink != target_sink_index:
                            logger.debug("Routing %s (idx: %d) into V-Sink CH_%d", resolved, si.index, target_ch)
                            # Move then unmute (PipeWire may cork during move)
                            self._seamless_move(pulse, si.index, target_sink_index, volume=1.0)

                    # Case B: App is in a V-Sink but shouldn't be
                    elif si.sink in active_v_sink_indices:
                        # Unmapped or mapped to a non-vsink channel → evacuate to default sink
                        logger.debug("Evacuating %s (idx: %d) out of V-Sink to Default", resolved, si.index)
                        vol = self._poti_volumes.get(target_ch, 0.5) if target_ch is not None else 0.5
                        self._seamless_move(pulse, si.index, default_sink.index, volume=vol)
                            
        except pulsectl.PulseError as exc:
            logger.error("V-Sink Routing Sync failed: %s", exc)

    @pyqtSlot(list)
    def apply_poti_volumes(self, volumes: list[float]) -> None:
        """
        Called when the Arduino pushed new raw hardware sliding values.
        Opens a single PulseAudio connection shared across all channels for the tick.
        """
        if self._config.input_mode == "midi_only":
            return
            
        try:
            with pulsectl.Pulse("nativmix-poti-tick") as shared_pulse:
                for channel, volume in enumerate(volumes):
                    # Auto-unmute if the hardware slider moves significantly (>5% since muted)
                    with self._state_lock:
                        is_muted = self._channel_muted.get(channel, False)
                        muted_vol = self._muted_at_volume.get(channel, volume)
                    if is_muted and abs(volume - muted_vol) > 0.05:
                        self.toggle_mute(channel)
                        with self._state_lock:
                            # Update reference so we don't spam toggle_mute
                            self._muted_at_volume[channel] = volume

                    with self._state_lock:
                        self._poti_volumes[channel] = volume
                        creating = channel in self._vsink_creating

                    if creating:
                        continue

                    mode = self._config.get_channel_mode(channel)
                    if mode == "hardware":
                        hw_id = self._config.get_hardware_id(channel)
                        if hw_id:
                            self._apply_hardware_volume(hw_id, volume)
                    else:
                        if self._config.is_v_sink_enabled(channel):
                            self._set_v_sink_volume(channel, volume, pulse=shared_pulse)
                        else:
                            app_names = self._config.get_app_names(channel)
                            for name in app_names:
                                self._apply_volume_by_name(name, volume, pulse=shared_pulse)
        except pulsectl.PulseError as exc:
            logger.error("apply_poti_volumes: PulseAudio connection lost: %s", exc)
                    
        self._update_thread_states()

    @pyqtSlot(list)
    def apply_midi_volumes(self, mappings: list[tuple[int, float]]) -> None:
        """
        Called when the MidiThread pushes new CC values.
        Args:
            mappings: list of (channel_index, volume)
        """
        try:
            with pulsectl.Pulse("nativmix-midi-tick") as shared_pulse:
                for channel, volume in mappings:
                    if channel < 0 or channel >= self._config.num_channels:
                        continue

                    # Auto-unmute if the MIDI CC moves significantly
                    with self._state_lock:
                        is_muted = self._channel_muted.get(channel, False)
                        muted_vol = self._muted_at_volume.get(channel, volume)
                    if is_muted and abs(volume - muted_vol) > 0.05:
                        self.toggle_mute(channel)
                        with self._state_lock:
                            self._muted_at_volume[channel] = volume

                    with self._state_lock:
                        self._poti_volumes[channel] = volume
                        creating = channel in self._vsink_creating

                    if creating:
                        continue

                    if self._config.is_v_sink_enabled(channel):
                        self._set_v_sink_volume(channel, volume, pulse=shared_pulse)
                    else:
                        app_names = self._config.get_app_names(channel)
                        for name in app_names:
                            self._apply_volume_by_name(name, volume, pulse=shared_pulse)
        except pulsectl.PulseError as exc:
            logger.error("apply_midi_volumes: PulseAudio connection lost: %s", exc)

        self._update_thread_states()

    def set_channel_volume(self, channel_index: int, volume: float) -> None:
        """Called directly by the GUI slider to override volume."""
        if channel_index < 0 or channel_index >= self._config.num_channels:
            return

        with self._state_lock:
            self._poti_volumes[channel_index] = volume
            if channel_index in self._vsink_creating:
                return

        # GUI slide -> auto unmute
        with self._state_lock:
            is_muted = self._channel_muted.get(channel_index, False)
        if is_muted:
            self.toggle_mute(channel_index)

        mode = self._config.get_channel_mode(channel_index)
        
        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index)
            if hw_id:
                self._apply_hardware_volume(hw_id, volume)
        else:
            if self._config.is_v_sink_enabled(channel_index):
                self._set_v_sink_volume(channel_index, volume) # Slider can open its own connection
            else:
                app_names = self._config.get_app_names(channel_index)
                for name in app_names:
                    self._apply_volume_by_name(name, volume)
                
        self._update_thread_states()

    def _set_v_sink_volume(self, channel_index: int, volume: float, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Set the hardware volume for a virtual sink directly.
        This ensures apps stay at 100% (Unity Gain) relative to the sink.
        """
        sink_name = f"NativMix_CH_{channel_index}"
        
        def _do_apply(p: pulsectl.Pulse) -> None:
            try:
                sink = p.get_sink_by_name(sink_name)
                p.volume_set_all_chans(sink, volume)
            except pulsectl.PulseError:
                return # V-sink might not exist yet

        try:
            if pulse is not None:
                _do_apply(pulse)
            else:
                with pulsectl.Pulse("nativmix-vsink-vol") as p:
                    _do_apply(p)
        except pulsectl.PulseError as exc:
            logger.error("Failed to apply V-Sink volume for CH %d: %s", channel_index, exc)

    def _seamless_move(
        self,
        pulse: pulsectl.Pulse,
        stream_index: int,
        target_sink_index: int,
        volume: float | None = None,
    ) -> None:
        """
        Move a sink-input to a new sink without stopping playback.

        Sequence:
          1. pactl move-sink-input  (most reliable PipeWire PA-layer move)
          2. sink_input_mute(False) (clear any cork PipeWire set during the move)
          3. optional: re-fetch stream and set volume on the new sink
        """
        try:
            result = subprocess.run(
                ["pactl", "move-sink-input", str(stream_index), str(target_sink_index)],
                capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
            )
            if result.returncode != 0:
                logger.warning(
                    "_seamless_move: pactl failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
        except subprocess.TimeoutExpired:
            logger.warning("_seamless_move: pactl timed out after %ds (stream %d)",
                           self._SUBPROCESS_TIMEOUT, stream_index)

        # PipeWire may cork the stream during the sink switch → explicitly unmute.
        try:
            pulse.sink_input_mute(stream_index, False)
        except pulsectl.PulseError:
            pass

        if volume is not None:
            try:
                si_fresh = next(
                    (s for s in pulse.sink_input_list() if s.index == stream_index),
                    None,
                )
                if si_fresh is not None:
                    pulse.volume_set_all_chans(si_fresh, volume)
            except pulsectl.PulseError:
                pass

    def _get_all_assigned_apps(self) -> set[str]:
        """Return a set of all app names explicitly assigned to any fader."""
        assigned = set()
        for ch in range(self._config.num_channels):
            if self._config.get_channel_mode(ch) != "hardware":
                assigned.update(n.lower() for n in self._config.get_app_names(ch))
        assigned.discard("system master")
        assigned.discard("other apps")
        return assigned

    def _apply_volume_by_name(self, app_name: str, volume: float, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Set the volume of all active streams matching app_name.
        Only called when V-Sink is INACTIVE for this channel.
        Accepts an optional shared Pulse connection to avoid repeated reconnects.
        """
        def _do_apply(p: pulsectl.Pulse) -> None:
            if app_name.lower() == "system master":
                default_sink_name = p.server_info().default_sink_name
                sink = p.get_sink_by_name(default_sink_name)
                p.volume_set_all_chans(sink, volume)
                return

            other_apps_mode = (app_name.lower() == "other apps")
            assigned_apps = self._get_all_assigned_apps() if other_apps_mode else set()
            found_other_apps = []

            for si in p.sink_input_list():
                props = dict(si.proplist)
                if _is_internal_stream(props):
                    continue

                pid_str = props.get("application.process.id", "0")
                try:
                    pid = int(pid_str)
                except ValueError:
                    pid = 0
                pa_fallback = (
                    props.get("application.name")
                    or props.get("application.process.binary")
                    or "Unknown"
                )
                resolved = resolve_app_name(pid, fallback=pa_fallback)
                
                if other_apps_mode:
                    if resolved.lower() not in assigned_apps and resolved.lower() != "system master":
                        p.volume_set_all_chans(si, volume)
                elif resolved.lower() == app_name.lower():
                    p.volume_set_all_chans(si, volume)

        try:
            if pulse is not None:
                _do_apply(pulse)
            else:
                with pulsectl.Pulse("nativmix-poti-apply") as p:
                    _do_apply(p)
        except pulsectl.PulseError as exc:
            logger.error("apply_volume_by_name('%s', %.2f) failed: %s", app_name, volume, exc)

    def _apply_hardware_volume(self, hw_id: str, volume: float) -> None:
        """Apply hardware volume directly to a specific sink or source."""
        try:
            parts = hw_id.split(':', 1)
            if len(parts) != 2:
                return
            kind, name = parts
            
            with pulsectl.Pulse("nativmix-hw-vol") as pulse:
                if kind == "sink":
                    dev = pulse.get_sink_by_name(name)
                    pulse.volume_set_all_chans(dev, volume)
                elif kind == "source":
                    dev = pulse.get_source_by_name(name)
                    pulse.volume_set_all_chans(dev, volume)
        except pulsectl.PulseError as exc:
            logger.error("Failed to apply hardware volume to %s: %s", hw_id, exc)

    def toggle_mute(self, channel_index: int) -> None:
        """
        Toggle the mute state of an entire channel (all apps assigned to it).
        Called by the CLI IPC server.
        """
        if channel_index < 0 or channel_index >= self._config.num_channels:
            logger.warning("toggle_mute requested for invalid channel %d", channel_index)
            return

        with self._state_lock:
            is_currently_muted = self._channel_muted.get(channel_index, False)
            new_mute_state = not is_currently_muted
            self._channel_muted[channel_index] = new_mute_state
            if new_mute_state:
                self._muted_at_volume[channel_index] = self._poti_volumes.get(channel_index, 0.0)

        logger.debug("IPC: Toggling mute for channel %d -> %s", channel_index, new_mute_state)
        
        self.mute_state_changed.emit(channel_index, new_mute_state)

        mode = self._config.get_channel_mode(channel_index)
        if mode == "hardware":
            hw_id = self._config.get_hardware_id(channel_index)
            if hw_id:
                try:
                    parts = hw_id.split(':', 1)
                    if len(parts) == 2:
                        kind, name = parts
                        with pulsectl.Pulse("nativmix-ipc-mute") as pulse:
                            if kind == "sink":
                                dev = pulse.get_sink_by_name(name)
                                pulse.mute(dev, new_mute_state)
                            elif kind == "source":
                                dev = pulse.get_source_by_name(name)
                                pulse.mute(dev, new_mute_state)
                except pulsectl.PulseError as exc:
                    logger.error("toggle_mute for HW %s failed: %s", hw_id, exc)
            return

        app_names = [n.lower() for n in self._config.get_app_names(channel_index)]
        if not app_names:
            return

        # Find all currently active streams that map to those apps and mute them
        try:
            with pulsectl.Pulse("nativmix-ipc-mute") as pulse:
                if "system master" in app_names:
                    default_sink_name = pulse.server_info().default_sink_name
                    sink = pulse.get_sink_by_name(default_sink_name)
                    pulse.mute(sink, new_mute_state)

                other_apps_mode = ("other apps" in app_names)
                assigned_apps = self._get_all_assigned_apps() if other_apps_mode else set()

                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    if _is_internal_stream(props):
                        continue
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = (
                        props.get("application.name")
                        or props.get("application.process.binary")
                        or "Unknown"
                    )
                    resolved = resolve_app_name(pid, fallback=pa_fallback)
                    
                    if other_apps_mode:
                        if resolved.lower() not in assigned_apps and resolved.lower() != "system master":
                            pulse.sink_input_mute(si.index, mute=new_mute_state)
                    elif resolved.lower() in app_names:
                        pulse.sink_input_mute(si.index, mute=new_mute_state)
        except pulsectl.PulseError as exc:
            logger.error("toggle_mute for channel %d failed: %s", channel_index, exc)

    def _on_master_volume_changed(self, volume: float, muted: bool) -> None:
        """
        Slot: Called when the System Master volume changes.
        Updates faders for any channel assigned to 'System Master'.
        """
        for ch in range(self._config.num_channels):
            if "system master" in [n.lower() for n in self._config.get_app_names(ch)]:
                with self._state_lock:
                    self._poti_volumes[ch] = volume
                    self._channel_muted[ch] = muted
                self.channel_volume_changed.emit(ch, volume)
                self.mute_state_changed.emit(ch, muted)

    @pyqtSlot()
    def _mark_audit_complete(self) -> None:
        """Called 2 s after run_audio_audit() finishes to allow hotplug handling."""
        self._initial_audit_complete = True
        logger.debug("Hotplug handling enabled (audit settled)")

    def _on_default_sink_changed(self, new_default_sink: str) -> None:
        """
        Slot: Called when the physical hardware target changes (Hotplug).
        Re-links active V-Sinks via Smart Linker using the loopback module ID
        (same lookup as enable_v_sink) so the node regex is always correct.
        Ignored for the first 2 s after startup to absorb audit-triggered events.
        """
        if not self._initial_audit_complete:
            logger.debug("Hotplug event suppressed (audit cooldown): %s", new_default_sink)
            return

        if new_default_sink.startswith("NativMix_"):
            return

        logger.debug("Hotplug detected! Default sink is now: %s. Re-linking...", new_default_sink)

        try:
            with pulsectl.Pulse("nativmix-hotplug") as pulse:
                hw_sink = self._get_master_hardware_sink(pulse)
                if hw_sink == self._last_hardware_sink:
                    return
                self._last_hardware_sink = hw_sink

                # Re-audit active V-Sinks.  Identify each loopback by its pactl
                # module ID (pulsectl module_list), then resolve the PipeWire node
                # name via pw-dump (resolve_loopback_node).  smart_link also accepts
                # the module ID directly for its own pw-dump lookup in find_ports.
                modules = pulse.module_list()
                for ch in range(self._config.num_channels):
                    if self._config.is_v_sink_enabled(ch):
                        sink_name = f"NativMix_CH_{ch}"
                        mod_id = None
                        for m in modules:
                            if m.name == "module-loopback" and m.argument:
                                if f"source={sink_name}.monitor" in m.argument:
                                    mod_id = str(m.index)
                                    break
                        if mod_id is None:
                            logger.debug("Hotplug: no loopback module found for %s, skipping",
                                         sink_name)
                            continue
                        loopback_node = routing.resolve_loopback_node(mod_id)
                        if loopback_node:
                            routing.clean_links(source_node=re.escape(loopback_node),
                                                target_node=re.escape(hw_sink))
                        routing.smart_link(
                            source_pattern=mod_id,
                            target_pattern=hw_sink,
                            source_port_pattern="output_"
                        )
        except pulsectl.PulseError as e:
            logger.error("Hotplug re-link failed: %s", e)

    # ------------------------------------------------------------------
    # Virtual Sinks (Pro-Routing)
    # ------------------------------------------------------------------

    def _get_master_hardware_sink(self, pulse: pulsectl.Pulse) -> str:
        """
        Identify the 'real' physical output device node name.
        Avoids picking NativMix Virtual Sinks or monitor sources.
        """
        try:
            default_sink_name = pulse.server_info().default_sink_name
            # If the default is a NativMix sink, we must find a real one
            if default_sink_name.startswith("NativMix_"):
                logger.debug("System default is a NativMix sink (%s). Finding hardware...", default_sink_name)
                for s in pulse.sink_list():
                    if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                        return s.name
            return default_sink_name
        except pulsectl.PulseError:
            # Absolute fallback: find the first non-nativmix sink
            sinks = pulse.sink_list()
            for s in sinks:
                if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                    return s.name
            return sinks[0].name if sinks else "auto_null"

    def _update_sink_metadata(self, sink_name: str) -> bool:
        """
        Inject/Update OSD-Bypass metadata on an existing sink without unloading it.
        Returns True if at least one property was set successfully.
        Each property is set independently so a rejected property does not abort the rest.
        """
        props = {
            "device.intended-roles": "internal",
            "device.class": "abstract",
            "node.passive": "true",
            "device.icon-name": "audio-card-virtual",
            "priority.driver": "1",
            "priority.session": "1"
        }
        any_success = False
        for key, val in props.items():
            if not val:
                continue
            try:
                subprocess.run(
                    ["pactl", "set-sink-property", sink_name, key, val],
                    check=True, capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
                )
                any_success = True
            except subprocess.TimeoutExpired:
                logger.warning("pactl set-sink-property timed out after %ds for %s (%s)",
                               self._SUBPROCESS_TIMEOUT, sink_name, key)
                return False  # Timeout means PipeWire may be stuck — abort
            except subprocess.CalledProcessError:
                # pactl set-sink-property is not supported for all property
                # names on every PipeWire/PulseAudio version — silently skip.
                pass
        if any_success:
            logger.debug("Updated OSD-Bypass metadata for %s", sink_name)
        return any_success

    def enable_v_sink(self, channel_index: int) -> None:
        """Create or Update a Virtual Sink and move mapped streams to it."""
        sink_name = f"NativMix_CH_{channel_index}"
        logger.debug("Enabling/Updating V-Sink for channel %d: %s", channel_index, sink_name)

        with self._state_lock:
            self._vsink_creating.add(channel_index)
        self._update_thread_states()  # propagate v_sink_busy=True to listener

        # 1. Create Sink or Update Metadata
        exists = False
        try:
            with pulsectl.Pulse("nativmix-exists-check") as pulse:
                pulse.get_sink_by_name(sink_name)
                exists = True
        except pulsectl.PulseError:
            pass

        if exists:
            logger.debug("Reusing existing V-Sink %s, updating properties...", sink_name)
            self._update_sink_metadata(sink_name)
        else:
            try:
                # Load null-sink with STRICT OSD-Bypass properties
                # - device.intended-roles=internal (Core system role)
                # - device.class=abstract (Signals non-UI component)
                # - node.passive=true (Prevents auto-management triggers)
                # - node.description (Clear identification in Helvum/pavucontrol)
                # - priority.*=1 (Stay out of default device logic)
                props = (
                    f"device.description={sink_name},"
                    f"node.description={sink_name},"
                    "media.class=Audio/Sink,"
                    "device.intended-roles=internal,"
                    "device.class=abstract,"
                    "node.passive=true,"
                    "device.icon-name=audio-card-virtual,"
                    "priority.driver=1,"
                    "priority.session=1"
                )
                subprocess.run(
                    [
                        "pactl", "load-module", "module-null-sink",
                        f"sink_name={sink_name}",
                        f"sink_properties={props}",
                    ],
                    check=True, capture_output=True, timeout=self._SUBPROCESS_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                logger.warning("pactl load-module null-sink timed out after %ds for %s",
                               self._SUBPROCESS_TIMEOUT, sink_name)
                return  # Sink state unknown — abort to avoid orphaned loopback modules
            except subprocess.CalledProcessError as e:
                # Usually means it already exists (e.g., from a crash)
                logger.warning("pactl load-module returned error or existed: %s", e.stderr)

        # 1b. Create or Reuse Loopback to Hardware (Isolated & Smart Linked)
        try:
            mod_id = None
            with pulsectl.Pulse("nativmix-loopback-check") as pulse:
                # Search for existing loopback module for this sink
                for m in pulse.module_list():
                    if m.name == "module-loopback" and m.argument:
                        if f"source={sink_name}.monitor" in m.argument:
                            mod_id = str(m.index)
                            logger.debug("Reusing existing loopback module %s for %s", mod_id, sink_name)
                            break
            
            if mod_id is None:
                # 1. Load loopback without auto-linking (Not found, so creation is required)
                res = subprocess.run(
                    ["pactl", "load-module", "module-loopback",
                     f"source={sink_name}.monitor", "dont-link=1"],
                    check=True, capture_output=True, text=True,
                    timeout=self._SUBPROCESS_TIMEOUT,
                )
                mod_id = res.stdout.strip()
                logger.debug("Created NEW loopback module %s for %s", mod_id, sink_name)
            
            # 2. Establish/Verify precise routing.
            # Resolve the PipeWire node name from the pactl module ID via pw-dump.
            # PipeWire node names (e.g. "output.loopback-<pid>-<pipewire-id>") do
            # NOT embed the pactl module index, so we look up the node whose
            # pulse.module.id property matches mod_id.  smart_link also accepts
            # the module ID directly (find_ports has the same pw-dump path).
            loopback_node = routing.resolve_loopback_node(mod_id)
            with pulsectl.Pulse("nativmix-vsink-setup") as pulse:
                hw_sink_node = self._get_master_hardware_sink(pulse)
                if loopback_node:
                    # Use exact node name for clean_links to avoid regex ambiguity
                    routing.clean_links(source_node=re.escape(loopback_node),
                                        target_node=re.escape(hw_sink_node))
                    routing.smart_link(
                        source_pattern=mod_id,   # find_ports resolves via pw-dump
                        target_pattern=hw_sink_node,
                        source_port_pattern="output_"
                    )
                else:
                    logger.warning("V-Sink %s: loopback node not found, routing may be incomplete",
                                   sink_name)
                # Explicitly unmute the loopback stream (safety layer)
                self._unmute_module_streams(mod_id, pulse)

            logger.debug("Smart Linker: V-Sink %s established -> %s", sink_name, hw_sink_node)
        except subprocess.TimeoutExpired:
            logger.warning("pactl load-module loopback timed out after %ds for %s",
                           self._SUBPROCESS_TIMEOUT, sink_name)
        except (subprocess.CalledProcessError, pulsectl.PulseError) as e:
            logger.warning("Smart Linker failed for V-Sink %s: %s", sink_name, e)

        # 2. Wait for PipeWire to actually register the Sink
        current_volume = self._poti_volumes.get(channel_index, 0.5)
        import time
        verified = False
        for _ in range(10):
            time.sleep(0.05)
            try:
                with pulsectl.Pulse("nativmix-vsink-poll") as p_poll:
                    if sink_name in [s.name for s in p_poll.sink_list()]:
                        verified = True
                        break
            except pulsectl.PulseError:
                pass
                
        if not verified:
            logger.warning("V-Sink %s did not appear in Pulse list after 500ms.", sink_name)
            with self._state_lock:
                self._vsink_creating.discard(channel_index)
            self._update_thread_states()
            return

        if exists:
            # Reuse path: V-Sink already has apps and a valid volume from the
            # previous session.  Do NOT override the volume and do NOT re-inject
            # apps — the AudioListenerThread re-routes any stream that isn't
            # already inside the sink via change events.  Touching the volume
            # here would cause a spike if _poti_volumes still holds the 1.0
            # initialisation default (first real Arduino tick is queued but not
            # yet processed).  apply_poti_volumes() will correct the level on
            # the next hardware tick (~20 ms).
            logger.debug("V-Sink %s reused – skipping volume override and re-injection", sink_name)
        else:
            # New sink: lock slider updates for this channel so no stray
            # apply_poti_volumes / set_channel_volume call can write to the
            # system sink while PipeWire is still registering the V-Sink.
            with self._state_lock:
                self._vsink_creating.add(channel_index)
            try:
                # Set sink to 0 directly before injecting apps so there is no
                # blast if _poti_volumes is still at the 1.0 default.
                # Use _set_v_sink_volume directly — set_channel_volume would
                # return early because the channel is in _vsink_creating.
                self._set_v_sink_volume(channel_index, 0.0)
                logger.debug("V-Sink %s created – zeroed before app injection (real vol=%.2f follows)",
                             sink_name, current_volume)
                # Move Apps and set Unity Gain (1.0 inside V-Sink)
                self._move_apps_to_sink(channel_index, sink_name, target_volume=1.0)
                # Give PipeWire a moment to settle the new routing before
                # applying the real fader volume.
                time.sleep(0.05)
                self._set_v_sink_volume(channel_index, current_volume)
            finally:
                with self._state_lock:
                    self._vsink_creating.discard(channel_index)

        with self._state_lock:
            self._vsink_creating.discard(channel_index)
        self._update_thread_states()  # propagate v_sink_busy=False
        self._restore_hardware_default_sink()

    def _unmute_module_streams(self, module_id: str | int, pulse: pulsectl.Pulse | None = None) -> None:
        """Explicitly unmute all sink-inputs belonging to a specific module."""
        try:
            mod_idx = int(module_id)
            if pulse:
                for si in pulse.sink_input_list():
                    if si.owner_module == mod_idx:
                        pulse.sink_input_mute(si.index, mute=False)
            else:
                with pulsectl.Pulse("nativmix-unmute-mod") as p:
                    for si in p.sink_input_list():
                        if si.owner_module == mod_idx:
                            p.sink_input_mute(si.index, mute=False)
        except (ValueError, pulsectl.PulseError):
            pass

    def _restore_hardware_default_sink(self, pulse: pulsectl.Pulse | None = None) -> None:
        """
        Force the default PulseAudio sink back to hardware.
        This prevents NativMix V-Sinks from triggering System OSDs.
        """
        def _do_restore(p: pulsectl.Pulse) -> None:
            hw_sink = self._get_master_hardware_sink(p)
            current_def = p.server_info().default_sink_name
            if current_def != hw_sink:
                logger.debug("Restoring default sink to hardware: %s (was %s)", hw_sink, current_def)
                try:
                    target = p.get_sink_by_name(hw_sink)
                    p.default_set(target)
                except pulsectl.PulseError as e:
                    logger.warning("Failed to restore default sink: %s", e)

        try:
            if pulse:
                _do_restore(pulse)
            else:
                with pulsectl.Pulse("nativmix-osd-bypass") as p:
                    _do_restore(p)
        except Exception:
            pass

    def disable_v_sink(self, channel_index: int) -> None:
        """
        Destroy the Virtual Sink and evacuate streams to the default real sink.

        Sequence (no-gap design):
          1. Move every stream from the V-Sink to the hardware sink.
          2. Immediately unmute / un-cork each moved stream.
          3. Apply the correct fader volume.
          4. Wait 150 ms so PipeWire can stabilise the stream on the new device.
          5. Unload the null-sink and loopback modules.
        """
        sink_name = f"NativMix_CH_{channel_index}"
        logger.debug("Disabling V-Sink for channel %d: %s", channel_index, sink_name)

        current_volume = self._poti_volumes.get(channel_index, 0.5)

        # ── CRITICAL: push new state to listener BEFORE any moves ────────────
        # Same race condition as _on_mapping_changed: if the listener thread
        # still has v_sink=True when it processes the change event triggered
        # by our pactl move, it immediately routes the stream back into the
        # V-Sink.  Pushing the updated state first (V-Sink is now disabled)
        # prevents this.  ConfigManager.set_v_sink_enabled(False) was already
        # called by the GUI toggle before disable_v_sink() is invoked.
        self._update_thread_states()

        # ── Step 1-3: Evacuate streams BEFORE destroying the device ──────────
        try:

            with pulsectl.Pulse("nativmix-vsink-evac") as pulse:
                # Resolve the real hardware target sink
                default_sink_name = pulse.server_info().default_sink_name
                try:
                    target_sink = pulse.get_sink_by_name(default_sink_name)
                except pulsectl.PulseError:
                    sinks = pulse.sink_list()
                    target_sink = sinks[0] if sinks else None

                if not target_sink:
                    logger.error("No target sink found for V-Sink evacuation!")
                    return

                # Safety: never route into another NativMix virtual sink
                if target_sink.name.startswith("NativMix_"):
                    for s in pulse.sink_list():
                        if not s.name.startswith("NativMix_") and "dummy" not in s.name.lower():
                            target_sink = s
                            break

                # Locate the virtual sink by name
                try:
                    v_sink = pulse.get_sink_by_name(sink_name)
                    v_sink_index = v_sink.index
                except pulsectl.PulseError:
                    v_sink_index = None

                if v_sink_index is not None:

                    for si in pulse.sink_input_list():
                        if si.sink != v_sink_index:
                            continue
                        logger.debug(
                            "Stream %d evacuating from V-Sink '%s' → Main Sink '%s'",
                            si.index, sink_name, target_sink.name
                        )
                        # _seamless_move: pactl move → unmute → set fader volume
                        self._seamless_move(pulse, si.index, target_sink.index, volume=current_volume)
                        logger.debug(
                            "Stream %d moved back to Main Sink and forced to resume (vol=%.2f).",
                            si.index, current_volume
                        )

        except pulsectl.PulseError as e:
            logger.error("Failed to evacuate V-Sink %s apps: %s", sink_name, e)

        # ── Step 4: Safety delay ─────────────────────────────────────────────
        # Give PipeWire ~150 ms to stabilise streams on the new sink before
        # the V-Sink device disappears.  Browsers (Chromium) are especially
        # sensitive to the device vanishing too early.
        import time
        time.sleep(0.15)

        # ── Step 5: Destroy null-sink + loopback modules ─────────────────────
        try:
            pid_out = subprocess.run(
                ["pactl", "list", "short", "modules"],
                capture_output=True, text=True, check=True,
                timeout=self._SUBPROCESS_TIMEOUT,
            )
            for line in pid_out.stdout.splitlines():
                if f"sink_name={sink_name}" in line or f"source={sink_name}.monitor" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True,
                                   timeout=self._SUBPROCESS_TIMEOUT)
                    logger.info("Unloaded module ID %s for %s", mod_id, sink_name)
        except subprocess.TimeoutExpired:
            logger.warning("pactl timed out after %ds while disabling V-Sink %s",
                           self._SUBPROCESS_TIMEOUT, sink_name)
        except (subprocess.CalledProcessError, IndexError) as e:
            logger.error("pactl unload-module failed: %s", e)

        # Fallback: re-apply fader volumes directly in case streams were missed above
        app_names = self._config.get_app_names(channel_index)
        for name in app_names:
            self._apply_volume_by_name(name, current_volume)

        self._update_thread_states()

    def _move_apps_to_sink(self, channel_index: int, target_sink_name: str, target_volume: float | None = None) -> None:
        """
        Move all apps belonging to a given channel to `target_sink_name`.
        If target_volume is provided, set it on all moved streams.
        """
        app_names = [n.lower() for n in self._config.get_app_names(channel_index)]
        if not app_names:
            return
            
        try:
            with pulsectl.Pulse("nativmix-vsink-router") as pulse:
                try:
                    target_sink = pulse.get_sink_by_name(target_sink_name)
                except pulsectl.PulseError:
                    logger.warning("Sink %s not found, cannot route yet.", target_sink_name)
                    return
                
                for si in pulse.sink_input_list():
                    props = dict(si.proplist)
                    pid_str = props.get("application.process.id", "0")
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        pid = 0
                    pa_fallback = props.get("application.name") or props.get("application.process.binary") or "Unknown"
                    resolved = resolve_app_name(pid, fallback=pa_fallback)
                    
                    if resolved.lower() not in app_names:
                        continue

                    if si.sink != target_sink.index:
                        self._seamless_move(pulse, si.index, target_sink.index, volume=target_volume)
                    elif target_volume is not None:
                        # Already on correct sink, just update volume
                        try:
                            pulse.volume_set_all_chans(si, target_volume)
                        except pulsectl.PulseError:
                            pass
        except pulsectl.PulseError as exc:
            logger.error("Routing apps to %s failed: %s", target_sink_name, exc)


    # ------------------------------------------------------------------
    # Debug / Status Helpers
    # ------------------------------------------------------------------



    def get_real_sinks(self) -> list[tuple[str, str]]:
        """Return a list of (description, name) of all real hardware sinks, excluding V-Sinks and monitors."""
        sinks: list[tuple[str, str]] = []
        try:
            with pulsectl.Pulse("nativmix-getsinks") as pulse:
                for s in pulse.sink_list():
                    # Skip NativMix virtual sinks and PipeWire dummy sinks
                    if s.name.startswith("NativMix_"):
                        continue
                    if "dummy" in s.name.lower():
                        continue
                    desc = s.description or s.name
                    sinks.append((desc, s.name))
        except pulsectl.PulseError as exc:
            logger.error("Failed to get real sinks: %s", exc)
        return sinks

    def get_real_sources(self) -> list[tuple[str, str]]:
        """Return a list of (description, name) of all real hardware sources (Inputs)."""
        sources: list[tuple[str, str]] = []
        try:
            with pulsectl.Pulse("nativmix-getsources") as pulse:
                for s in pulse.source_list():
                    # PipeWire monitors are often Sources for Outputs. Filter them optionally if needed,
                    # but usually, we want physical inputs. We keep all for flexibility, except internal Monitors if needed.
                    if "monitor" in s.name.lower():
                        continue
                    desc = s.description or s.name
                    sources.append((desc, s.name))
        except pulsectl.PulseError as exc:
            logger.error("Failed to get real sources: %s", exc)
        return sources

    def get_default_sink_name(self) -> str:
        """Return the current system default sink name."""
        try:
            with pulsectl.Pulse("nativmix-getdef") as pulse:
                return pulse.server_info().default_sink_name
        except pulsectl.PulseError:
            return ""

    def set_default_sink_and_move_loopbacks(self, sink_name: str) -> None:
        """Change the default system sink and move all loopback modules to it."""
        try:
            with pulsectl.Pulse("nativmix-setdef") as pulse:
                # 1. Set the new default sink
                target_sink = pulse.get_sink_by_name(sink_name)
                pulse.default_set(target_sink)
                logger.info("Set default system sink to %s", sink_name)

                # 2. Find and move loopback modules
                loopback_module_ids = {m.index for m in pulse.module_list() if m.name == "module-loopback"}
                
                for si in pulse.sink_input_list():
                    # Check if the sink input belongs to a module-loopback
                    is_loopback = si.owner_module in loopback_module_ids
                    if is_loopback:
                        if si.sink != target_sink.index:
                            logger.debug("Moving loopback stream [%d] to new Master Output", si.index)
                            pulse.sink_input_move(si.index, target_sink.index)
                            
        except pulsectl.PulseError as exc:
            logger.error("Failed to set default sink and route loopbacks: %s", exc)

    def panic_reset(self) -> None:
        """
        Emergency reset: Evacuate all streams to default sink and destroy all modules.
        This must be called synchronously from the main thread GUI trigger.
        """
        logger.warning("Panic Reset triggered!")
        
        # 1. Evacuate all apps
        try:
            with pulsectl.Pulse("nativmix-panic-evac") as pulse:
                default_sink_name = pulse.server_info().default_sink_name
                default_sink = pulse.get_sink_by_name(default_sink_name)
                
                for si in pulse.sink_input_list():
                    try:
                        pulse.sink_input_move(si.index, default_sink.index)
                    except pulsectl.PulseError as e:
                        logger.warning("Could not evacuate %d: %s", si.index, e)
        except pulsectl.PulseError as e:
            logger.error("Panic Evac failed: %s", e)
            
        # 2. Destroy all NativMix Sinks and their Loopbacks
        try:
            pid_out = subprocess.run(
                ["pactl", "list", "short", "modules"],
                capture_output=True, text=True, check=True,
                timeout=self._SUBPROCESS_TIMEOUT,
            )
            for line in pid_out.stdout.splitlines():
                if "sink_name=NativMix_CH_" in line or "source=NativMix_CH_" in line:
                    mod_id = line.split()[0]
                    subprocess.run(["pactl", "unload-module", mod_id], check=True,
                                   timeout=self._SUBPROCESS_TIMEOUT)
                    logger.info("Panic unloaded module ID %s", mod_id)
        except subprocess.TimeoutExpired:
            logger.warning("pactl timed out after %ds during Panic Reset",
                           self._SUBPROCESS_TIMEOUT)
        except subprocess.CalledProcessError as e:
            logger.error("pactl unload-module failed during Panic: %s", e.stderr)
