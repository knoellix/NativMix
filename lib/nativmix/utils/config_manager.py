"""
Configuration manager for NativMix.

Implements Rule 14: XDG-standard config path on Linux
(~/.config/nativmix/config.json), AppData on Windows (future).

Schema (v6)
-----------
{
    "version": 6,
    "hardware": {
        "port": "/dev/ttyACM0",   // null = auto-detect
        "auto_search_device": true, // if false, use port exclusively (no auto-discovery)
        "num_channels": 5,
        "input_mode": "usb",      // "usb" | "hybrid" | "midi_only"
        "midi_device": "",
        "midi_channel_count": 0
    },
    "settings": {
        "threshold": 0.01,        // minimum volume delta to trigger a PW call (1%)
        "refresh_rate": 30,       // target UI refresh rate in Hz
        "invert_map": [false, ...],
        "transparency": true,
        "show_invert_option": false,
        "stay_open": false,
        "compact_mode": false,
        "debug_logging": false,
        "midi_fader_feedback": false
    },
    "channels": [
        {
            "index": 0,           // zero-based channel index
            "inverted": false,
            "mode": "app",        // "app" | "hardware"
            "is_midi": false,     // true for MIDI-only channels
            "hardware_id": null,
            "app_names": ["Spotify", "Firefox"],
            "label": null,        // custom display name
            "v_sink": false,
            "volume": 1.0,        // last known fader position
            "midi_cc": null,      // legacy alias for midi_bindings[0].cc
            "midi_channel": 0,    // legacy alias for midi_bindings[0].midi_channel
            "midi_bindings": [{"cc": null, "midi_channel": 0}],  // one volume Learn slot
            "midi_mute_cc": null, // assigned CC number for mute toggle
            "midi_mute_channel": 0
        },
        ...
    ]
}

Migration history:
  v0→v1: added channels array
  v1→v2: added hardware.port, settings.invert_map
  v2→v3: added settings.transparency, settings.stay_open
  v3→v4: added settings.debug_logging
  v4→v5: added MIDI fields, v_sink, volume, label, compact_mode
  v5→v6: added hardware.auto_search_device flag
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from nativmix.utils.paths import get_config_dir as _get_config_dir_from_paths

logger = logging.getLogger(__name__)

CONFIG_VERSION = 8

# App names that have special routing semantics and cannot be mixed with regular apps.
SPECIAL_APPS: frozenset[str] = frozenset({"system master", "other apps"})

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
        "transparency": True,
        # GUI: Show 'Invert' checkbox in main mixer channels
        "show_invert_option": False,
        # GUI: Stay open on close (true = quit, false = hide to tray)
        "stay_open": False,
        # GUI: Compact mode — hide app list and controls below channel label
        "compact_mode": False,
        # MIDI: send outbound CC to sync physical fader positions (opt-in, default off)
        "midi_fader_feedback": False,
        # Windows/Flatpak: check GitHub releases for a newer version (hint only)
        "check_for_updates": True,
        # Normalized version string the user chose not to be reminded about again
        "update_dismissed_version": None,
    }


def _default_config(num_channels: int = 5) -> dict[str, Any]:
    """Return a freshly-built default configuration dictionary."""
    return {
        "version": CONFIG_VERSION,
        "hardware": {
            "port": None,  # None → auto-detect
            "auto_search_device": True,  # True → try to auto-detect even with port set
            "num_channels": num_channels,
            "input_mode": "usb",  # "usb", "hybrid", "midi_only"
            "midi_device": "",
            "midi_channel_count": 5,
            "baud_rate": 9600,
        },
        "settings": _default_settings(num_channels),
        "channels": [
            {
                "index": i,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "midi_cc": None,  # Legacy alias for midi_bindings[0].cc
                "midi_mute_cc": None,
                "midi_channel": 0,  # Legacy alias for midi_bindings[0].midi_channel
                "midi_mute_channel": 0,
                "midi_bindings": [{"cc": None, "midi_channel": 0}],
                "hardware_id": None,
                "app_names": [],
                "volume": 1.0,  # Last known volume [0.0, 1.0]
            }
            for i in range(num_channels)
        ],
    }


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

    mapping_changed = pyqtSignal(int, list)  # channel_index, new app_names list
    settings_changed = pyqtSignal()  # any global setting changed

    def __init__(
        self,
        config_path: Path | None = None,
        profiles_dir: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            config_path:  Override the default config file path (useful for tests).
            profiles_dir: Override the default profiles directory (useful for tests).
            parent:       Optional Qt parent object.
        """
        super().__init__(parent)

        if config_path is not None:
            self._path = config_path
        else:
            self._path = _get_config_dir_from_paths() / "config.json"

        if profiles_dir is not None:
            self._profiles_dir: Path = profiles_dir
        else:
            self._profiles_dir = _get_config_dir_from_paths() / "profiles"

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
        # Clean up any leftover .tmp file from a previous SIGKILL during save().
        _tmp = self._path.with_suffix(".json.tmp")
        if _tmp.exists():
            try:
                _tmp.unlink()
                logger.debug("Removed stale config tmp file: %s", _tmp)
            except OSError as exc:
                logger.debug("Could not remove stale tmp file %s: %s", _tmp, exc)

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
            # No file → create defaults, then trigger migration so profile-1.json is created
            num_ch = 5  # sensible default; updated later if hardware section differs
            self._data = _default_config(num_ch)
            # Trigger v6→v7 migration so profile-1.json gets created on fresh install
            self._data["version"] = 6
            self._migrate()
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

            data_to_write = {k: v for k, v in self._data.items() if k != "channels"}
            # Also strip legacy maps from settings — these are now per-channel in the profile
            if "settings" in data_to_write:
                settings = dict(data_to_write["settings"])
                settings.pop("invert_map", None)
                settings.pop("v_sink_map", None)
                data_to_write["settings"] = settings
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data_to_write, f, indent=2, ensure_ascii=False)
                f.write("\n")  # POSIX convention: newline at EOF
            tmp.replace(self._path)  # atomic rename
            logger.debug("Config saved to %s", self._path)
        except OSError as exc:
            logger.error("Failed to save config to %s: %s", self._path, exc)

    def apply_profile(self, profile: dict[str, Any]) -> None:
        """
        Load channel data from a profile dict into memory.

        Rebuilds invert_map and v_sink_map from per-channel flags so
        existing code that reads config.invert_map continues to work.
        Does NOT call save() — the profile file is the source of truth.
        """
        # Deep copy so the config owns its own data. Without this, set_channel_volume()
        # would mutate the caller's profile dict (they share the same list), which
        # corrupts the volume values before the restore branch can read them.
        channels = copy.deepcopy(profile.get("channels", []))
        self._data["channels"] = channels
        # Rebuild legacy mirror lists that ArduinoThread and backend read
        settings = self._data.setdefault("settings", {})
        settings["invert_map"] = [bool(ch.get("inverted", False)) for ch in channels]
        settings["v_sink_map"] = [bool(ch.get("v_sink", False)) for ch in channels]
        from nativmix.utils.channel_order import normalize_channel_order

        channel_ids = [int(ch.get("index", i)) for i, ch in enumerate(channels)]
        raw_order = profile.get("channel_order")
        self._data["channel_order"] = normalize_channel_order(
            list(raw_order) if isinstance(raw_order, list) else None,
            channel_ids,
        )
        self.settings_changed.emit()
        logger.debug("Profile applied: %s (%d channels)", profile.get("id"), len(channels))

    @property
    def active_profile_id(self) -> str:
        """ID of the currently active profile."""
        return str(self._data.get("active_profile", ""))

    @active_profile_id.setter
    def active_profile_id(self, profile_id: str) -> None:
        # Caller saves explicitly via config.save() after updating active profile
        self._data["active_profile"] = profile_id

    @property
    def profile_midi_next_cc(self) -> int | None:
        """Global CC number that switches to the next profile."""
        val = self._data.get("settings", {}).get("profile_midi_next_cc")
        return int(val) if val is not None else None

    @profile_midi_next_cc.setter
    def profile_midi_next_cc(self, cc: int | None) -> None:
        if cc is not None and not (0 <= cc <= 127):
            logger.warning("Invalid MIDI CC value %d (must be 0-127), ignoring", cc)
            return
        self._data.setdefault("settings", {})["profile_midi_next_cc"] = cc
        self.settings_changed.emit()

    @property
    def profile_midi_prev_cc(self) -> int | None:
        """Global CC number that switches to the previous profile."""
        val = self._data.get("settings", {}).get("profile_midi_prev_cc")
        return int(val) if val is not None else None

    @profile_midi_prev_cc.setter
    def profile_midi_prev_cc(self, cc: int | None) -> None:
        if cc is not None and not (0 <= cc <= 127):
            logger.warning("Invalid MIDI CC value %d (must be 0-127), ignoring", cc)
            return
        self._data.setdefault("settings", {})["profile_midi_prev_cc"] = cc
        self.settings_changed.emit()

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

        self._data["settings"].setdefault("transparency", True)
        self._data["settings"].setdefault("show_invert_option", False)

        # v3 → v4: add debug_logging
        self._data["settings"].setdefault("debug_logging", False)

        # v4 → v5 (implicit): add stay_open, add MIDI config
        self._data["settings"].setdefault("stay_open", False)
        self._data["settings"].setdefault("compact_mode", False)
        self._data["settings"].setdefault("midi_fader_feedback", False)
        self._data["settings"].setdefault("check_for_updates", True)
        self._data["settings"].setdefault("update_dismissed_version", None)
        hw = self._data.setdefault("hardware", {})
        hw.setdefault("input_mode", "usb")
        hw.setdefault("midi_device", "")
        hw.setdefault("baud_rate", 9600)
        # Default to 0 MIDI channels so hybrid mode starts clean.
        # If the old migration wrote 5 here but the user never set up any MIDI
        # (no midi_cc values, no is_midi flags), silently reset to 0 so that
        # hybrid mode doesn't suddenly show 5 empty MIDI channel columns.
        hw.setdefault("midi_channel_count", 0)
        if hw.get("midi_channel_count", 0) == 5:
            has_midi_usage = any(
                ch.get("midi_cc") is not None or ch.get("is_midi", False) for ch in self._data.get("channels", [])
            )
            if not has_midi_usage:
                hw["midi_channel_count"] = 0

        for ch in self._data["channels"]:
            ch.setdefault("mode", "app")
            ch.setdefault("hardware_id", None)

        # v5 → v6: add auto_search_device flag
        # If a port is already configured, disable auto-search by default (preserve existing
        # device setting). If no port is configured, keep auto-search enabled (auto-detect mode).
        hw = self._data.setdefault("hardware", {})
        if "auto_search_device" not in hw:
            # If port is set, disable auto-search to prevent overriding the configured device
            hw["auto_search_device"] = hw.get("port") is None
            logger.debug("Migrated auto_search_device: %s (port=%s)", hw["auto_search_device"], hw.get("port"))

        # v6 → v7: move channels[] out of config.json into a profile file
        if version < 7:
            from nativmix.utils.profile_manager import ProfileManager, default_channels

            pm = ProfileManager(profiles_dir=self._profiles_dir)
            old_channels = self._data.pop("channels", [])
            if not old_channels:
                num_ch = self._data.get("hardware", {}).get("num_channels", 5)
                old_channels = default_channels(num_ch)
            num_ch = len(old_channels)
            profile = {
                "id": "profile-1",
                "name": "Profile 1",
                "channel_count": num_ch,
                "restore_fader_positions": False,
                "midi_switch_cc": None,
                "channels": old_channels,
            }
            profile_path = self._profiles_dir / "profile-1.json"
            if profile_path.exists():
                logger.debug("v6→v7: profile-1.json already exists, skipping creation")
            else:
                try:
                    pm.save_profile(profile)
                except OSError as exc:
                    logger.error("v6→v7 migration: could not write profile-1.json: %s", exc)
            self._data["active_profile"] = "profile-1"
            self._data.get("settings", {}).pop("invert_map", None)
            self._data.get("settings", {}).pop("v_sink_map", None)
            logger.debug("Migrated config v6→v7: channels moved to profile-1")

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
        logger.debug("Port set to %s", port)

    @property
    def auto_search_device(self) -> bool:
        """
        If True, nativmix performs auto-discovery at startup even when a port is configured.
        If False, nativmix uses the configured port exclusively without auto-discovery.
        Default: True (auto-detect behavior for backwards compatibility).
        """
        return bool(self._data.get("hardware", {}).get("auto_search_device", True))

    @auto_search_device.setter
    def auto_search_device(self, value: bool) -> None:
        self._data.setdefault("hardware", {})["auto_search_device"] = bool(value)
        self.settings_changed.emit()

    @property
    def baud_rate(self) -> int:
        """Serial baud rate for the Arduino connection. Default: 9600."""
        return int(self._data.get("hardware", {}).get("baud_rate", 9600))

    @baud_rate.setter
    def baud_rate(self, rate: int) -> None:
        self._data.setdefault("hardware", {})["baud_rate"] = int(rate)
        self.settings_changed.emit()

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
        added_ids: list[int] = []
        while len(channels) < n:
            idx = len(channels)
            channels.append(
                {
                    "index": idx,
                    "is_midi": False,
                    "inverted": inv_map[idx] if idx < len(inv_map) else False,
                    "v_sink": v_sink_map[idx] if idx < len(v_sink_map) else False,
                    "mode": "app",
                    "hardware_id": None,
                    "midi_cc": None,
                    "midi_mute_cc": None,
                    "midi_channel": 0,
                    "midi_mute_channel": 0,
                    "midi_bindings": [{"cc": None, "midi_channel": 0}],
                    "app_names": [],
                    "volume": 1.0,
                }
            )
            added_ids.append(idx)

        # Retroactively apply is_midi flag strictly based on index vs hw_count.
        # Channels 0 to (hw_count-1) are ALWAYS USB/Hardware.
        # Channels >= hw_count are ALWAYS MIDI.
        # This stability is CRITICAL for the UI to correctly add/remove buttons.
        for idx, ch in enumerate(channels):
            ch["is_midi"] = idx >= hw_count
            ch["index"] = idx

        from nativmix.utils.channel_order import normalize_channel_order

        channel_ids = list(range(len(channels)))
        current = self._data.get("channel_order")
        if not isinstance(current, list):
            current = list(channel_ids)
        else:
            current = list(current) + [i for i in added_ids if i not in current]
        self._data["channel_order"] = normalize_channel_order(current, channel_ids)

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

        from nativmix.utils.channel_order import normalize_channel_order, order_after_remove

        prev_order = self._data.get("channel_order")
        if isinstance(prev_order, list):
            remapped = order_after_remove([int(x) for x in prev_order], index)
        else:
            remapped = list(range(len(channels)))
        self._data["channel_order"] = normalize_channel_order(remapped, list(range(len(channels))))

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
        return bool(self._data.get("settings", {}).get("transparency", True))

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
    def stay_open(self) -> bool:
        """If True, closing the main window quits the app. If False, it hides to tray."""
        return bool(self._data.get("settings", {}).get("stay_open", False))

    @stay_open.setter
    def stay_open(self, value: bool) -> None:
        self._data.setdefault("settings", {})["stay_open"] = bool(value)
        self.save()
        # We do NOT emit settings_changed here, because stay_open
        # only affects window closing behavior. Emitting it would cause
        # the main window to re-apply all settings and send current hardware
        # volumes to PulseAudio, causing an audible and visual fader "jump".

    @property
    def compact_mode(self) -> bool:
        """If True, hide app list and controls below the channel label."""
        return bool(self._data.get("settings", {}).get("compact_mode", False))

    @compact_mode.setter
    def compact_mode(self, value: bool) -> None:
        self._data.setdefault("settings", {})["compact_mode"] = bool(value)
        self.save()

    @property
    def midi_fader_feedback(self) -> bool:
        """If True, NativMix sends outbound MIDI CC to sync controller fader positions."""
        return bool(self._data.get("settings", {}).get("midi_fader_feedback", False))

    @midi_fader_feedback.setter
    def midi_fader_feedback(self, value: bool) -> None:
        self._data.setdefault("settings", {})["midi_fader_feedback"] = bool(value)
        self.settings_changed.emit()

    @property
    def check_for_updates(self) -> bool:
        """Whether Windows/Flatpak may query GitHub for newer releases."""
        return bool(self._data.get("settings", {}).get("check_for_updates", True))

    @check_for_updates.setter
    def check_for_updates(self, value: bool) -> None:
        self._data.setdefault("settings", {})["check_for_updates"] = bool(value)

    @property
    def update_dismissed_version(self) -> str | None:
        """Normalized remote version the user silenced until a newer one appears."""
        raw = self._data.get("settings", {}).get("update_dismissed_version")
        if raw is None or raw == "":
            return None
        return str(raw)

    @update_dismissed_version.setter
    def update_dismissed_version(self, value: str | None) -> None:
        self._data.setdefault("settings", {})["update_dismissed_version"] = (
            None if value is None or value == "" else str(value)
        )

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

            channels.append(
                {
                    "index": idx,
                    "is_midi": is_midi,
                    "inverted": False,
                    "v_sink": False,
                    "mode": "app",
                    "midi_cc": None,
                    "midi_mute_cc": None,
                    "midi_channel": 0,
                    "midi_mute_channel": 0,
                    "midi_bindings": [{"cc": None, "midi_channel": 0}],
                    "hardware_id": None,
                    "app_names": [],
                }
            )
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

        is_special = app_name.lower() in SPECIAL_APPS
        has_special = any(n.lower() in SPECIAL_APPS for n in target_names)

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

    def set_inverted(self, channel: int, inverted: bool) -> None:
        """
        Set the inversion flag for *channel* and emit settings_changed.

        Args:
            channel:  Zero-based channel index.
            inverted: True → 0 ADC = 100% volume.
        """
        self._channel(channel)["inverted"] = inverted
        # Keep invert_map in sync
        inv = self._data.setdefault("settings", {}).setdefault("invert_map", [False] * self.num_channels)
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
        vm = self._data.setdefault("settings", {}).setdefault("v_sink_map", [False] * self.num_channels)
        while len(vm) <= channel:
            vm.append(False)
        vm[channel] = enabled
        self.settings_changed.emit()

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
        return self.get_all_assigned_apps_by_name().get(app_name.lower())

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
        return copy.deepcopy(self._data.get("channels", []))

    def get_channel_order(self) -> list[int]:
        """Left-to-right GUI order of stable channel indices."""
        from nativmix.utils.channel_order import normalize_channel_order

        channels = self._data.get("channels", [])
        channel_ids = [int(ch.get("index", i)) for i, ch in enumerate(channels)]
        raw = self._data.get("channel_order")
        return normalize_channel_order(list(raw) if isinstance(raw, list) else None, channel_ids)

    def set_channel_order(self, order: list[int]) -> None:
        """Set GUI strip order (does not emit settings_changed — caller rebuilds UI)."""
        from nativmix.utils.channel_order import normalize_channel_order

        channels = self._data.get("channels", [])
        channel_ids = [int(ch.get("index", i)) for i, ch in enumerate(channels)]
        self._data["channel_order"] = normalize_channel_order(list(order), channel_ids)

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

    _MAX_MIDI_BINDINGS = 1  # Single volume Learn per strip (multi-slot UI removed)

    @staticmethod
    def _empty_midi_binding() -> dict[str, Any]:
        return {"cc": None, "midi_channel": 0}

    def _normalize_midi_binding(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return self._empty_midi_binding()
        cc_raw = raw.get("cc")
        cc = int(cc_raw) if cc_raw is not None else None
        try:
            midi_ch = int(raw.get("midi_channel", 0))
        except (TypeError, ValueError):
            midi_ch = 0
        return {"cc": cc, "midi_channel": max(0, min(15, midi_ch))}

    def _sync_legacy_midi_volume_fields(self, ch: dict[str, Any]) -> None:
        """Keep midi_cc / midi_channel mirrors in sync with bindings[0]."""
        bindings = ch.get("midi_bindings") or [self._empty_midi_binding()]
        first = self._normalize_midi_binding(bindings[0] if bindings else None)
        ch["midi_cc"] = first["cc"]
        ch["midi_channel"] = first["midi_channel"]

    def _ensure_midi_bindings(self, ch: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Ensure midi_bindings exists (migrate legacy midi_cc / midi_channel).

        Always stores a single binding (length 1). Extra slots from older
        multi-learn configs are discarded.
        """
        raw = ch.get("midi_bindings")
        if isinstance(raw, list) and raw:
            bindings = [self._normalize_midi_binding(raw[0])]
        else:
            cc_raw = ch.get("midi_cc")
            cc = int(cc_raw) if cc_raw is not None else None
            try:
                midi_ch = int(ch.get("midi_channel", 0))
            except (TypeError, ValueError):
                midi_ch = 0
            bindings = [{"cc": cc, "midi_channel": max(0, min(15, midi_ch))}]
        ch["midi_bindings"] = bindings
        self._sync_legacy_midi_volume_fields(ch)
        return bindings

    def get_midi_binding_count(self, channel: int) -> int:
        """Number of volume Learn slots (always 1)."""
        self._ensure_midi_bindings(self._channel(channel))
        return 1

    def set_midi_binding_count(self, channel: int, count: int) -> None:
        """Kept for API compatibility; always forces a single Learn slot."""
        del count
        ch = self._channel(channel)
        bindings = self._ensure_midi_bindings(ch)
        ch["midi_bindings"] = bindings[:1]
        self._sync_legacy_midi_volume_fields(ch)
        self.save()
        self.settings_changed.emit()

    def get_midi_binding(self, channel: int, slot: int = 0) -> dict[str, Any]:
        """Return ``{cc, midi_channel}`` for volume Learn *slot*."""
        bindings = self._ensure_midi_bindings(self._channel(channel))
        if slot < 0 or slot >= len(bindings):
            return self._empty_midi_binding()
        return dict(bindings[slot])

    def set_midi_binding(
        self,
        channel: int,
        slot: int,
        cc: int | None,
        midi_channel: int | None = None,
    ) -> None:
        """Set volume Learn *slot* to *cc* / *midi_channel* (0–15)."""
        ch = self._channel(channel)
        bindings = self._ensure_midi_bindings(ch)
        if slot < 0 or slot >= len(bindings):
            logger.warning("set_midi_binding: invalid slot %d for channel %d", slot, channel)
            return
        entry = dict(bindings[slot])
        entry["cc"] = int(cc) if cc is not None else None
        if midi_channel is not None:
            entry["midi_channel"] = max(0, min(15, int(midi_channel)))
        elif cc is None:
            entry["midi_channel"] = 0
        bindings[slot] = entry
        ch["midi_bindings"] = bindings
        self._sync_legacy_midi_volume_fields(ch)
        self.save()
        self.settings_changed.emit()

    def get_midi_cc(self, channel: int) -> int | None:
        """Return volume CC for slot 0 (legacy alias)."""
        return self.get_midi_binding(channel, 0).get("cc")

    def get_midi_channel(self, channel: int) -> int:
        """Return MIDI channel (0–15) for volume slot 0 (legacy alias)."""
        return int(self.get_midi_binding(channel, 0).get("midi_channel", 0))

    def set_midi_cc(
        self,
        channel: int,
        cc: int | None,
        midi_channel: int | None = None,
    ) -> None:
        """Assign volume CC on slot 0 (legacy alias)."""
        self.set_midi_binding(channel, 0, cc, midi_channel=midi_channel)

    def set_midi_channel(self, channel: int, midi_channel: int) -> None:
        """Set MIDI channel for volume slot 0 without changing the CC."""
        binding = self.get_midi_binding(channel, 0)
        self.set_midi_binding(channel, 0, binding.get("cc"), midi_channel=midi_channel)

    def get_all_midi_mappings(self) -> dict[tuple[int, int], int]:
        """Return (midi_channel, cc) -> NativMix channel index for all volume slots."""
        mappings: dict[tuple[int, int], int] = {}
        for ch in self._data.get("channels", []):
            if not ch.get("is_midi", False):
                continue
            bindings = self._ensure_midi_bindings(ch)
            for binding in bindings:
                cc = binding.get("cc")
                if cc is None:
                    continue
                midi_ch = int(binding.get("midi_channel", 0))
                mappings[(midi_ch, int(cc))] = int(ch["index"])
        return mappings

    def get_midi_fader_feedback_targets(self) -> list[tuple[int, float]]:
        """Return (channel_index, volume) for channels with any volume CC binding."""
        targets: list[tuple[int, float]] = []
        for idx in range(self.num_channels):
            bindings = self._ensure_midi_bindings(self._channel(idx))
            if any(b.get("cc") is not None for b in bindings):
                targets.append((idx, self.get_channel_volume(idx)))
        return targets

    def get_midi_mute_cc(self, channel: int) -> int | None:
        """Return the assigned MIDI CC number for mute-toggle on *channel*."""
        val = self._channel(channel).get("midi_mute_cc")
        return int(val) if val is not None else None

    def get_midi_mute_channel(self, channel: int) -> int:
        """Return MIDI channel (0-15) for the mute CC binding; default 0."""
        val = self._channel(channel).get("midi_mute_channel", 0)
        try:
            ch = int(val)
        except (TypeError, ValueError):
            return 0
        return max(0, min(15, ch))

    def set_midi_mute_cc(
        self,
        channel: int,
        cc: int | None,
        midi_channel: int | None = None,
    ) -> None:
        """Assign a mute-toggle MIDI CC (and optional MIDI channel 0-15)."""
        ch = self._channel(channel)
        ch["midi_mute_cc"] = cc
        if midi_channel is not None:
            ch["midi_mute_channel"] = max(0, min(15, int(midi_channel)))
        elif cc is None:
            ch["midi_mute_channel"] = 0
        self.save()
        self.settings_changed.emit()

    def set_midi_mute_channel(self, channel: int, midi_channel: int) -> None:
        """Set MIDI channel (0-15) for the mute CC without changing the CC number."""
        self._channel(channel)["midi_mute_channel"] = max(0, min(15, int(midi_channel)))
        self.save()
        self.settings_changed.emit()

    def get_all_midi_mute_mappings(self) -> dict[tuple[int, int], int]:
        """Return (midi_channel, cc) -> channel index for mute-CC assignments.

        Intentionally does not filter by is_midi: if a channel's is_midi flag
        is temporarily wrong (e.g. after hw channel-count oscillation), the
        binding must still survive.  Only MIDI widgets can set midi_mute_cc,
        so non-MIDI channels will never have a value here in practice.
        """
        mappings: dict[tuple[int, int], int] = {}
        for ch in self._data.get("channels", []):
            cc = ch.get("midi_mute_cc")
            if cc is not None:
                try:
                    midi_ch = int(ch.get("midi_mute_channel", 0))
                except (TypeError, ValueError):
                    midi_ch = 0
                midi_ch = max(0, min(15, midi_ch))
                mappings[(midi_ch, int(cc))] = int(ch["index"])
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

    def set_channel_volume(self, index: int, volume: float) -> None:
        """Update the last known volume for a channel in memory.

        Volumes are persisted on clean shutdown via save(). Callers that need
        immediate persistence (e.g. on infrequent user actions) should call
        save() explicitly afterwards.
        """
        channels = self._data.get("channels", [])
        if 0 <= index < len(channels):
            channels[index]["volume"] = volume

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
