"""
Configuration manager for NativMix.

Implements Rule 14: XDG-standard config path on Linux
(~/.config/nativmix/config.json), AppData on Windows (future).

Schema (v2)
-----------
{
    "version": 2,
    "hardware": {
        "port": "/dev/ttyACM0",   // null = auto-detect
        "num_channels": 5,
        "baud_rate": 9600
    },
    "settings": {
        "threshold": 0.01,        // minimum volume delta to trigger a PW call (1%)
        "refresh_rate": 30,       // target UI refresh rate in Hz
        "invert_map": [false, false, false, false, false]  // per-channel inversion
    },
    "channels": [
        {
            "index": 0,           // zero-based Arduino channel (poti index)
            "inverted": false,    // kept for per-channel override; synced with invert_map
            "mode": "app",        // "app" or "hardware"
            "hardware_id": null,  // e.g. "sink:alsa_output.pci-0000_00_1f.3.analog-stereo"
            "app_names": ["Spotify", "Firefox"]  // app names this poti controls
        },
        ...
    ]
}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from nativmix.utils.paths import get_config_dir as _get_config_dir_from_paths

logger = logging.getLogger(__name__)

CONFIG_VERSION = 5

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

def _default_settings(num_channels: int = 5) -> dict[str, Any]:
    """Return default global settings."""
    return {
        # Minimum volume delta required before a PipeWire call is made.
        # 0.01 = 1%, tuned for PipeWire on Arch (matches ArduinoThread VOLUME_THRESHOLD).
        "threshold": 0.01,
        # Target GUI refresh rate in Hz (used by future animated widgets).
        "refresh_rate": 30,
        # Per-channel inversion flags – mirrors the per-channel "inverted" field
        # for quick access without iterating the channels list.
        "invert_map": [False] * num_channels,
        # Per-channel V-Sink flags - enables virtual sinks for Pro-Routing
        "v_sink_map": [False] * num_channels,
        # GUI: Transparency translucent background toggle
        "transparency": False,
        # GUI: Show 'Invert' checkbox in main mixer channels
        "show_invert_option": False,
        # GUI: Stay open on close (true = quit, false = hide to tray)
        "stay_open": False,
    }


def _default_config(num_channels: int = 5) -> dict[str, Any]:
    """Return a freshly-built default configuration dictionary."""
    return {
        "version": CONFIG_VERSION,
        "hardware": {
            "port": None,         # None → auto-detect
            "num_channels": num_channels,
            "baud_rate": 9600,
            "input_mode": "usb",  # "usb", "hybrid", "midi_only"
            "midi_device": "",
            "midi_channel_count": 5,
        },
        "settings": _default_settings(num_channels),
        "channels": [
            {
                "index": i,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "midi_cc": None,  # MIDI Control Change number (0-127)
                "hardware_id": None,
                "app_names": [],
                "volume": 1.0,    # Last known volume [0.0, 1.0]
            }
            for i in range(num_channels)
        ],
    }


# ---------------------------------------------------------------------------
# Platform path resolution  (delegated to nativmix.utils.paths)
# ---------------------------------------------------------------------------

def _get_config_dir() -> Path:
    """Return the platform-appropriate configuration directory via paths.py."""
    return _get_config_dir_from_paths()


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager(QObject):
    """
    Reads and writes the NativMix configuration JSON file.

    Inherits QObject so it can emit signals when the config changes.
    The audio backend and GUI connect to these signals for live updates
    without polling or file-system watchers.

    Signals
    -------
    mapping_changed(int, list[str])
        Emitted after update_mapping() or set_app_names() changes a channel.
        Carries the channel index and the new app name list.
    settings_changed()
        Emitted when threshold, refresh_rate, or invert_map changes.

    Usage::

        cfg = ConfigManager()
        cfg.update_mapping("Spotify", channel=0)
        cfg.save()
        # → mapping_changed(0, ["Spotify"]) fires automatically

    Thread safety: All mutations and saves happen on the main thread only.
    The Arduino- and Audio-threads read config values at start-up; runtime
    changes go through this class and are applied via Qt signals.
    """

    mapping_changed = pyqtSignal(int, list)   # channel_index, new app_names list
    settings_changed = pyqtSignal()           # any global setting changed
    v_sink_changed = pyqtSignal(int, bool)    # channel_index, is_enabled

    def __init__(self, config_path: Path | None = None, parent: QObject | None = None) -> None:
        """
        Args:
            config_path: Override the default config file path (useful for tests).
            parent:      Optional Qt parent object.
        """
        super().__init__(parent)

        if config_path is not None:
            self._path = config_path
        else:
            self._path = _get_config_dir() / "config.json"

        self._data: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """
        Load configuration from disk.

        If the file does not exist or is malformed, the default configuration
        is used and immediately written to disk to create the file.
        """
        if self._path.exists():
            try:
                with self._path.open(encoding="utf-8") as f:
                    self._data = json.load(f)
                self._migrate()
                logger.debug("Config loaded from %s", self._path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read config (%s), using defaults: %s", self._path, exc)
                _bak = self._path.with_suffix(".json.bak")
                try:
                    self._path.rename(_bak)
                    logger.warning("Corrupted config saved as backup: %s", _bak)
                except OSError:
                    pass
                num_ch = 5
                self._data = _default_config(num_ch)
                self.save()
        else:
            # No file → create defaults
            num_ch = 5  # sensible default; updated later if hardware section differs
            self._data = _default_config(num_ch)
            self.save()
        # Always ensure is_midi flags match hw_count, even for fresh or very
        # old configs that were written before this field was introduced.
        self._ensure_channels(self.num_channels)

    def save(self) -> None:
        """Persist the current configuration to disk (atomic write via temp file)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            
            # Add current app version to the data before saving
            from nativmix.metadata import __version__
            self._data["app_version"] = __version__
            
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
                f.write("\n")  # POSIX convention: newline at EOF
            tmp.replace(self._path)  # atomic rename
            logger.debug("Config saved to %s", self._path)
        except OSError as exc:
            logger.error("Failed to save config to %s: %s", self._path, exc)

    def _migrate(self) -> None:
        """Apply forward migrations when the config version increases."""
        version = self._data.get("version", 0)
        if version >= CONFIG_VERSION:
            return
        logger.debug("Migrating config v%d → v%d", version, CONFIG_VERSION)
        num_ch = self._data.get("hardware", {}).get("num_channels", 5)
        # v0/v1/v2 → v3: add settings block, ensure hardware mode keys
        self._data.setdefault("hardware", _default_config(num_ch)["hardware"])
        self._data.setdefault("settings", _default_settings(num_ch))
        self._data.setdefault("channels", _default_config(num_ch)["channels"])
        
        # Ensure invert_map length matches num_channels
        inv = self._data["settings"].setdefault("invert_map", [False] * num_ch)
        while len(inv) < num_ch:
            inv.append(False)
            
        vs = self._data["settings"].setdefault("v_sink_map", [False] * num_ch)
        while len(vs) < num_ch:
            vs.append(False)
            
        self._data["settings"].setdefault("transparency", False)
        self._data["settings"].setdefault("show_invert_option", False)
        
        # v3 → v4: add debug_logging
        self._data["settings"].setdefault("debug_logging", False)
        
        # v4 → v5 (implicit): add stay_open, add MIDI config
        self._data["settings"].setdefault("stay_open", False)
        hw = self._data.setdefault("hardware", {})
        hw.setdefault("input_mode", "usb")
        hw.setdefault("midi_device", "")
        # Default to 0 MIDI channels so hybrid mode starts clean.
        # If the old migration wrote 5 here but the user never set up any MIDI
        # (no midi_cc values, no is_midi flags), silently reset to 0 so that
        # hybrid mode doesn't suddenly show 5 empty MIDI channel columns.
        hw.setdefault("midi_channel_count", 0)
        if hw.get("midi_channel_count", 0) == 5:
            has_midi_usage = any(
                ch.get("midi_cc") is not None or ch.get("is_midi", False)
                for ch in self._data.get("channels", [])
            )
            if not has_midi_usage:
                hw["midi_channel_count"] = 0

        for ch in self._data["channels"]:
            ch.setdefault("mode", "app")
            ch.setdefault("hardware_id", None)

        self._data["version"] = CONFIG_VERSION
        # Ensure is_midi flags are correct for all channels after migration.
        self._ensure_channels(self.num_channels)
        self.save()

    # ------------------------------------------------------------------
    # Hardware settings
    # ------------------------------------------------------------------

    @property
    def hardware_port(self) -> str | None:
        """Preferred serial device path, or None for auto-detection."""
        return self._data.get("hardware", {}).get("port", None)

    @hardware_port.setter
    def hardware_port(self, port: str | None) -> None:
        self._data.setdefault("hardware", {})["port"] = port

    @property
    def hw_channel_count(self) -> int:
        """Number of hardware (Arduino/USB) potentiometer channels only."""
        return int(self._data.get("hardware", {}).get("num_channels", 5))

    @property
    def num_channels(self) -> int:
        """Total number of potentiometer & MIDI channels combined."""
        if self.input_mode in ("hybrid", "midi_only"):
            return self.hw_channel_count + self.midi_channel_count
        return self.hw_channel_count

    @num_channels.setter
    def num_channels(self, value: int) -> None:
        self._data.setdefault("hardware", {})["num_channels"] = value
        # Use the total channel count (hw + midi) so MIDI channels are not
        # accidentally dropped when the hardware count changes.
        self._ensure_channels(self.num_channels)

    def _ensure_channels(self, n: int) -> None:
        """Expand the channels list to at least *n* entries (never shrinks automatically unless explicit)."""
        channels = self._data.setdefault("channels", [])
        inv_map = self._data.get("settings", {}).get("invert_map", [])
        v_sink_map = self._data.get("settings", {}).get("v_sink_map", [])
        hw_count = self.hw_channel_count

        # Pad with empty dictionaries if needed
        while len(channels) < n:
            idx = len(channels)
            channels.append({
                "index": idx,
                "is_midi": False,
                "inverted": inv_map[idx] if idx < len(inv_map) else False,
                "v_sink": v_sink_map[idx] if idx < len(v_sink_map) else False,
                "mode": "app",
                "hardware_id": None,
                "app_names": [],
                "volume": 1.0,
            })

        # Retroactively apply is_midi flag strictly based on index vs hw_count.
        # Channels 0 to (hw_count-1) are ALWAYS USB/Hardware.
        # Channels >= hw_count are ALWAYS MIDI.
        # This stability is CRITICAL for the UI to correctly add/remove buttons.
        for idx, ch in enumerate(channels):
            ch["is_midi"] = (idx >= hw_count)
            ch["index"] = idx

    @property
    def baud_rate(self) -> int:
        """Serial baud rate for the Arduino connection."""
        return int(self._data.get("hardware", {}).get("baud_rate", 9600))
        
    @property
    def input_mode(self) -> str:
        """Input mode: 'usb', 'hybrid', or 'midi_only'."""
        return str(self._data.get("hardware", {}).get("input_mode", "usb"))

    @input_mode.setter
    def input_mode(self, mode: str) -> None:
        mode = mode if mode in ("usb", "hybrid", "midi_only") else "usb"
        self._data.setdefault("hardware", {})["input_mode"] = mode
        # Re-evaluate channel count based on new mode
        self._ensure_channels(self.num_channels)
        self.save()  # Force immediate save for robust mode switching
        self.settings_changed.emit()

    @property
    def midi_device(self) -> str:
        """Selected MIDI device name."""
        return str(self._data.get("hardware", {}).get("midi_device", ""))

    @midi_device.setter
    def midi_device(self, device: str) -> None:
        self._data.setdefault("hardware", {})["midi_device"] = device
        self.settings_changed.emit()

    @property
    def midi_channel_count(self) -> int:
        """Number of fader channels to expose in midi_only or hybrid mode."""
        return int(self._data.get("hardware", {}).get("midi_channel_count", 5))

    @midi_channel_count.setter
    def midi_channel_count(self, count: int) -> None:
        count = max(0, count)
        self._data.setdefault("hardware", {})["midi_channel_count"] = count
        if self.input_mode in ("midi_only", "hybrid"):
            self._ensure_channels(self.num_channels)
        self.settings_changed.emit()

    def add_midi_channel(self) -> None:
        """Increment midi_channel_count by 1."""
        self.midi_channel_count += 1
        self.save()

    def remove_midi_channel(self, index: int) -> None:
        """
        Remove the MIDI channel at `index` from the config, decrement
        midi_channel_count, and re-index the remaining channels.
        Raises ValueError if the channel is not a MIDI channel.
        """
        channels: list[dict] = self._data.get("channels", [])
        if index < 0 or index >= len(channels):
            return
            
        target = channels[index]
        if not target.get("is_midi", False):
            raise ValueError(f"Channel {index} is not a MIDI channel and cannot be removed.")
            
        # Remove it
        channels.pop(index)
        
        # Decrement the configured count
        self._data.setdefault("hardware", {})["midi_channel_count"] = max(0, self.midi_channel_count - 1)
        
        # Re-index remaining channels
        for i, ch in enumerate(channels):
            ch["index"] = i
            
        # Keep invert/v_sink maps synced with the new length
        inv = self._data.get("settings", {}).get("invert_map", [])
        if len(inv) > index:
            inv.pop(index)
            self._data.setdefault("settings", {})["invert_map"] = inv
            
        v_sink = self._data.get("settings", {}).get("v_sink_map", [])
        if len(v_sink) > index:
            v_sink.pop(index)
            self._data.setdefault("settings", {})["v_sink_map"] = v_sink
            
        self.save()
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    # Global settings
    # ------------------------------------------------------------------

    @property
    def threshold(self) -> float:
        """
        Minimum volume delta (linear, 0.0–1.0) that triggers a PipeWire
        volume call.  Default: 0.01 (1%).

        Mirrors VOLUME_THRESHOLD in hardware/arduino.py; stored here so
        the GUI can expose it as a user setting.
        """
        return float(self._data.get("settings", {}).get("threshold", 0.01))

    @threshold.setter
    def threshold(self, value: float) -> None:
        value = max(0.001, min(0.1, value))  # clamp to [0.1%, 10%]
        self._data.setdefault("settings", {})["threshold"] = value
        self.settings_changed.emit()

    @property
    def refresh_rate(self) -> int:
        """Target GUI refresh rate in Hz. Default: 30."""
        return int(self._data.get("settings", {}).get("refresh_rate", 30))

    @refresh_rate.setter
    def refresh_rate(self, value: int) -> None:
        self._data.setdefault("settings", {})["refresh_rate"] = max(1, min(120, value))
        self.settings_changed.emit()
        
    @property
    def transparency(self) -> bool:
        """Enable KDE/Wayland translucent window background."""
        return bool(self._data.get("settings", {}).get("transparency", False))

    @transparency.setter
    def transparency(self, value: bool) -> None:
        self._data.setdefault("settings", {})["transparency"] = bool(value)
        self.settings_changed.emit()

    @property
    def show_invert_option(self) -> bool:
        """Show or hide the Invert checkbox in the main mixer."""
        return bool(self._data.get("settings", {}).get("show_invert_option", False))

    @show_invert_option.setter
    def show_invert_option(self, value: bool) -> None:
        self._data.setdefault("settings", {})["show_invert_option"] = bool(value)
        self.settings_changed.emit()

    @property
    def vu_meter_enabled(self) -> bool:
        """Show real-time VU level bars next to each fader."""
        return bool(self._data.get("settings", {}).get("vu_meter_enabled", False))

    @vu_meter_enabled.setter
    def vu_meter_enabled(self, value: bool) -> None:
        self._data.setdefault("settings", {})["vu_meter_enabled"] = bool(value)
        self.settings_changed.emit()

    @property
    def stay_open(self) -> bool:
        """If True, closing the main window quits the app. If False, it hides to tray."""
        return bool(self._data.get("settings", {}).get("stay_open", False))

    @stay_open.setter
    def stay_open(self, value: bool) -> None:
        self._data.setdefault("settings", {})["stay_open"] = bool(value)
        # We do NOT emit settings_changed here, because stay_open
        # only affects window closing behavior. Emitting it would cause
        # the main window to re-apply all settings and send current hardware
        # volumes to PulseAudio, causing an audible and visual fader "jump".


    def get_volume_exponent(self) -> float:
        """
        Power-law exponent for the fader curve (1.0 = linear, 2.0 = quadratic, 3.0 = cubic).

        Controlled by the 'Fader Curve Intensity' slider in the Audio settings tab.
        Default: 2.0
        """
        return float(self._data.get("settings", {}).get("volume_exponent", 2.0))

    def set_volume_exponent(self, value: float) -> None:
        """Set the fader curve exponent and notify the Arduino thread."""
        value = max(1.0, min(3.0, round(value, 2)))
        self._data.setdefault("settings", {})["volume_exponent"] = value
        self.settings_changed.emit()

    @property
    def invert_map(self) -> list[bool]:
        """
        Per-channel inversion flags as a plain list.

        Index corresponds to the zero-based Arduino channel (poti index).
        True → 0 ADC = 100% volume.
        """
        return list(self._data.get("settings", {}).get("invert_map", []))

    @invert_map.setter
    def invert_map(self, flags: list[bool]) -> None:
        self._data.setdefault("settings", {})["invert_map"] = [bool(f) for f in flags]
        # Keep per-channel "inverted" in sync
        for i, flag in enumerate(flags):
            self._channel(i)["inverted"] = flag

    def get_effective_inversion(self, channel: int) -> bool:
        """
        Return the effective inversion flag for *channel*.

        Per-channel ``inverted`` takes precedence over the global
        ``invert_map`` when they differ (allows individual overrides).
        """
        per_ch = self._channel(channel).get("inverted", None)
        if per_ch is not None:
            return bool(per_ch)
        inv_map = self.invert_map
        if channel < len(inv_map):
            return inv_map[channel]
        return False

    @property
    def v_sink_map(self) -> list[bool]:
        """Per-channel Virtual Sink flags."""
        return list(self._data.get("settings", {}).get("v_sink_map", []))

    @v_sink_map.setter
    def v_sink_map(self, flags: list[bool]) -> None:
        self._data.setdefault("settings", {})["v_sink_map"] = [bool(f) for f in flags]
        for i, flag in enumerate(flags):
            self._channel(i)["v_sink"] = flag

    # ------------------------------------------------------------------
    # Channel / mapping API
    # ------------------------------------------------------------------

    def _channel(self, index: int) -> dict[str, Any]:
        """Return the raw channel dict, auto-creating it if missing."""
        channels: list[dict] = self._data.setdefault("channels", [])
        # Ensure enough channel entries exist
        while len(channels) <= index:
            idx = len(channels)
            mode = self.input_mode
            hw_count = int(self._data.get("hardware", {}).get("num_channels", 5))
            
            is_midi = False
            if mode == "midi_only":
                is_midi = True
            elif mode == "hybrid" and idx >= hw_count:
                is_midi = True

            channels.append({
                "index": idx,
                "is_midi": is_midi,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "midi_cc": None,
                "hardware_id": None,
                "app_names": [],
            })
        return channels[index]

    def get_app_names(self, channel: int) -> list[str]:
        """
        Return the list of application names controlled by *channel*.

        Args:
            channel: Zero-based Arduino channel (poti) index.

        Returns:
            List of app name strings, e.g. ["Spotify", "Firefox"].
        """
        return list(self._channel(channel).get("app_names", []))

    def set_app_names(self, channel: int, names: list[str]) -> None:
        """
        Set the application names for *channel* and emit mapping_changed.

        Args:
            channel: Zero-based channel index.
            names:   List of human-readable app name strings.
        """
        self._channel(channel)["app_names"] = list(names)
        self.mapping_changed.emit(channel, list(names))

    def update_mapping(self, app_name: str, channel_index: int) -> None:
        """
        Assign *app_name* to *channel_index*, removing it from any other
        channel it was previously mapped to.

        This guarantees EXCLUSIVITY: an app (including 'System Master' or
        'Other Apps') can only exist on exactly one channel at a time.

        Args:
            app_name:      Human-readable app name, e.g. "Spotify".
            channel_index: Zero-based target channel (poti index).
        """
        # Remove the app from every channel it currently appears in
        for ch in self._data.get("channels", []):
            names: list[str] = ch.get("app_names", [])
            if app_name in names:
                names.remove(app_name)
                ch["app_names"] = names
                self.mapping_changed.emit(int(ch["index"]), list(names))

        # Enforce Isolation for "System Master" and "Other Apps"
        target_names = self._channel(channel_index).get("app_names", [])
        
        is_special = app_name.lower() in ("system master", "other apps")
        has_special = any(n.lower() in ("system master", "other apps") for n in target_names)
        
        if is_special and target_names and not has_special:
            raise ValueError("Not allowed")
        elif has_special and not is_special:
            raise ValueError("Not allowed")
        elif is_special and has_special and app_name.lower() not in [n.lower() for n in target_names]:
            # Can't have *both* System Master and Other Apps together either
            raise ValueError("Not allowed")
            
        if app_name not in target_names:
            target_names.append(app_name)
            self._channel(channel_index)["app_names"] = target_names
            
        # If System Master is on the channel, forcefully disable V-Sink
        if any(n.lower() == "system master" for n in target_names):
            self.set_v_sink_enabled(channel_index, False)
            
        self.mapping_changed.emit(channel_index, list(target_names))
        logger.debug("update_mapping: '%s' → channel %d", app_name, channel_index)


    def add_app_name(self, channel: int, name: str) -> None:
        """Add *name* to the app list of *channel* (no duplicates)."""
        names = self.get_app_names(channel)
        if name not in names:
            names.append(name)
            self.set_app_names(channel, names)

    def remove_app_name(self, channel: int, name: str) -> None:
        """Remove *name* from the app list of *channel* (no-op if absent)."""
        names = self.get_app_names(channel)
        if name in names:
            names.remove(name)
            self.set_app_names(channel, names)

    def is_inverted(self, channel: int) -> bool:
        """Return True if *channel* is configured as inverted."""
        return bool(self._channel(channel).get("inverted", False))

    def set_inverted(self, channel: int, inverted: bool) -> None:
        """
        Set the inversion flag for *channel* and emit settings_changed.

        Args:
            channel:  Zero-based channel index.
            inverted: True → 0 ADC = 100% volume.
        """
        self._channel(channel)["inverted"] = inverted
        # Keep invert_map in sync
        inv = self._data.setdefault("settings", {}).setdefault(
            "invert_map", [False] * self.num_channels
        )
        while len(inv) <= channel:
            inv.append(False)
        inv[channel] = inverted
        self.settings_changed.emit()

    def is_v_sink_enabled(self, channel: int) -> bool:
        """Return True if *channel* has Virtual Sink enabled."""
        per_ch = self._channel(channel).get("v_sink", None)
        if per_ch is not None:
            return bool(per_ch)
        vm = self.v_sink_map
        if channel < len(vm):
            return vm[channel]
        return False

    def set_v_sink_enabled(self, channel: int, enabled: bool) -> None:
        """Toggle Virtual Sink for *channel* and emit signal."""
        # System Master cannot have V-Sink
        if enabled:
            names = self.get_app_names(channel)
            if any(n.lower() == "system master" for n in names):
                raise ValueError("Not allowed")
                
        self._channel(channel)["v_sink"] = enabled
        vm = self._data.setdefault("settings", {}).setdefault(
            "v_sink_map", [False] * self.num_channels
        )
        while len(vm) <= channel:
            vm.append(False)
        vm[channel] = enabled
        self.settings_changed.emit()
        self.v_sink_changed.emit(channel, enabled)
    def get_all_assigned_apps_by_name(self) -> dict[str, int]:
        """
        Return a reverse map of {app_name_lower: channel_index} for all 
        explicitly assigned apps.
        """
        mapping = {}
        for ch in self._data.get("channels", []):
            ch_idx = int(ch["index"])
            for name in ch.get("app_names", []):
                mapping[name.lower()] = ch_idx
        return mapping


    def find_channel_for_app(self, app_name: str) -> int | None:
        """
        Find which channel (poti index) is mapped to *app_name*.

        The comparison is case-insensitive.

        Args:
            app_name: Resolved application name, e.g. "Spotify".

        Returns:
            Zero-based channel index, or None if no mapping exists.
        """
        lower = app_name.lower()
        for ch in self._data.get("channels", []):
            for name in ch.get("app_names", []):
                if name.lower() == lower:
                    return int(ch["index"])
        return None


    # ------------------------------------------------------------------
    # Logging Configuration
    # ------------------------------------------------------------------

    @property
    def debug_logging(self) -> bool:
        """Return True if extensive debug logging is enabled."""
        return bool(self._data.get("settings", {}).get("debug_logging", False))

    @debug_logging.setter
    def debug_logging(self, enabled: bool) -> None:
        """Enable or disable debug logging."""
        if "settings" not in self._data:
            self._data["settings"] = _default_settings(self.num_channels)
        self._data["settings"]["debug_logging"] = enabled
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    # Convenience: full channel snapshot
    # ------------------------------------------------------------------

    def all_channels(self) -> list[dict[str, Any]]:
        """Return a deep copy of all channel configurations."""
        import copy
        return copy.deepcopy(self._data.get("channels", []))

    # ------------------------------------------------------------------
    # Hardware Mode
    # ------------------------------------------------------------------

    def get_channel_mode(self, channel: int) -> str:
        """Return 'app' or 'hardware' for the given channel index."""
        return str(self._channel(channel).get("mode", "app"))

    def set_channel_mode(self, channel: int, mode: str) -> None:
        """Change mode between 'app' and 'hardware' (and emit change)."""
        mode = mode if mode in ("app", "hardware") else "app"
        self._channel(channel)["mode"] = mode
        self.settings_changed.emit()

    def get_hardware_id(self, channel: int) -> str | None:
        """Return the target hardware sink/source string, e.g. 'sink:alsa_output...'."""
        val = self._channel(channel).get("hardware_id")
        return str(val) if val else None

    def set_hardware_id(self, channel: int, hw_id: str | None) -> None:
        """Set the target hardware sink/source string (and emit change)."""
        self._channel(channel)["hardware_id"] = hw_id
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    # MIDI CC Mappings
    # ------------------------------------------------------------------

    def get_midi_cc(self, channel: int) -> int | None:
        """Return the assigned MIDI CC number for *channel*."""
        val = self._channel(channel).get("midi_cc")
        return int(val) if val is not None else None

    def set_midi_cc(self, channel: int, cc: int | None) -> None:
        """Assign a MIDI CC number to *channel* and save."""
        self._channel(channel)["midi_cc"] = cc
        self.save()
        self.settings_changed.emit()

    def get_all_midi_mappings(self) -> dict[int, int]:
        """Return a mapping of CC number -> channel index for all MIDI channels."""
        mappings = {}
        for ch in self._data.get("channels", []):
            if ch.get("is_midi", False):
                cc = ch.get("midi_cc")
                if cc is not None:
                    mappings[int(cc)] = int(ch["index"])
        return mappings

    def clear_usb_channel_mappings(self) -> None:
        """
        Remove all application names from USB (non-MIDI) channels.
        Used when entering MIDI-only mode to prevent 'ghost' assignments.
        """
        for i, ch in enumerate(self._data.get("channels", [])):
            if not ch.get("is_midi", False):
                old_names = ch.get("app_names", [])
                if old_names:
                    ch["app_names"] = []
                    self.mapping_changed.emit(i, [])
        self.save()
        logger.debug("All USB channel mappings cleared.")

    def clear_midi_channel_mappings(self) -> None:
        """
        Remove all application names from MIDI channels.
        Used when purging MIDI channels in USB mode.
        """
        for i, ch in enumerate(self._data.get("channels", [])):
            if ch.get("is_midi", False):
                old_names = ch.get("app_names", [])
                if old_names:
                    ch["app_names"] = []
                    self.mapping_changed.emit(i, [])
        self.save()
        logger.debug("All MIDI channel mappings cleared.")

    def get_channel_volume(self, index: int) -> float:
        """Return the last known volume for a channel."""
        channels = self._data.get("channels", [])
        if 0 <= index < len(channels):
            return float(channels[index].get("volume", 1.0))
        return 1.0

    def set_channel_volume(self, index: int, volume: float, save: bool = False) -> None:
        """Update the last known volume for a channel in memory."""
        channels = self._data.get("channels", [])
        if 0 <= index < len(channels):
            channels[index]["volume"] = volume
            if save:
                self.save()

    def get_channel_label(self, index: int) -> str | None:
        """Return the custom display label for a channel, or None if not set."""
        channels = self._data.get("channels", [])
        if 0 <= index < len(channels):
            return channels[index].get("label") or None
        return None

    def set_channel_label(self, index: int, label: str) -> None:
        """Persist a custom display label for a channel."""
        channels = self._data.get("channels", [])
        if 0 <= index < len(channels):
            channels[index]["label"] = label
            self.settings_changed.emit()

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"ConfigManager(path={self._path}, channels={self.num_channels})"
