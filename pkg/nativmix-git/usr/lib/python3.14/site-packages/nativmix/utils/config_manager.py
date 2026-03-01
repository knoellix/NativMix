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
import os
import platform
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

CONFIG_VERSION = 4

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
        # GUI: Glass-look translucent background toggle
        "glass_look": True,
    }


def _default_config(num_channels: int = 5) -> dict[str, Any]:
    """Return a freshly-built default configuration dictionary."""
    return {
        "version": CONFIG_VERSION,
        "hardware": {
            "port": None,         # None → auto-detect
            "num_channels": num_channels,
            "baud_rate": 9600,
        },
        "settings": _default_settings(num_channels),
        "channels": [
            {
                "index": i,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "hardware_id": None,
                "app_names": [],
            }
            for i in range(num_channels)
        ],
    }


# ---------------------------------------------------------------------------
# Platform path resolution
# ---------------------------------------------------------------------------

def _get_config_dir() -> Path:
    """
    Return the platform-appropriate configuration directory.

    Linux  → $XDG_CONFIG_HOME/nativmix  (default: ~/.config/nativmix)
    Windows → %APPDATA%\\NativMix
    """
    system = platform.system()

    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "NativMix"

    # Linux / BSD / macOS: honour XDG_CONFIG_HOME
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "nativmix"


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
                logger.info("Config loaded from %s", self._path)
                return
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read config (%s), using defaults: %s", self._path, exc)

        # No file or broken file → create defaults
        num_ch = 5  # sensible default; updated later if hardware section differs
        self._data = _default_config(num_ch)
        self.save()

    def save(self) -> None:
        """Persist the current configuration to disk (atomic write via temp file)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
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
        logger.info("Migrating config v%d → v%d", version, CONFIG_VERSION)
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
            
        self._data["settings"].setdefault("glass_look", True)
        
        # v3 → v4: add active_theme setting
        self._data["settings"].setdefault("active_theme", "System (Default)")
        
        for ch in self._data["channels"]:
            ch.setdefault("mode", "app")
            ch.setdefault("hardware_id", None)
            
        self._data["version"] = CONFIG_VERSION
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
    def num_channels(self) -> int:
        """Number of potentiometer channels configured."""
        return int(self._data.get("hardware", {}).get("num_channels", 5))

    @num_channels.setter
    def num_channels(self, value: int) -> None:
        self._data.setdefault("hardware", {})["num_channels"] = value
        self._ensure_channels(value)

    def _ensure_channels(self, n: int) -> None:
        """Expand the channels list to at least *n* entries (never shrinks)."""
        channels = self._data.setdefault("channels", [])
        inv_map = self._data.get("settings", {}).get("invert_map", [])
        v_sink_map = self._data.get("settings", {}).get("v_sink_map", [])
        while len(channels) < n:
            idx = len(channels)
            channels.append({
                "index": idx,
                "inverted": inv_map[idx] if idx < len(inv_map) else False,
                "v_sink": v_sink_map[idx] if idx < len(v_sink_map) else False,
                "mode": "app",
                "hardware_id": None,
                "app_names": [],
            })

    @property
    def baud_rate(self) -> int:
        """Serial baud rate for the Arduino connection."""
        return int(self._data.get("hardware", {}).get("baud_rate", 9600))

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
    def glass_look(self) -> bool:
        """Enable KDE/Wayland Glass-Look translucent window."""
        return bool(self._data.get("settings", {}).get("glass_look", True))
        
    @glass_look.setter
    def glass_look(self, value: bool) -> None:
        self._data.setdefault("settings", {})["glass_look"] = bool(value)
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
            channels.append({
                "index": len(channels),
                "inverted": False,
                "v_sink": False,
                "mode": "app",
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

        # Add it to the target channel
        target_names = self._channel(channel_index).get("app_names", [])
        if app_name not in target_names:
            target_names.append(app_name)
            self._channel(channel_index)["app_names"] = target_names
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
        self._channel(channel)["v_sink"] = enabled
        vm = self._data.setdefault("settings", {}).setdefault(
            "v_sink_map", [False] * self.num_channels
        )
        while len(vm) <= channel:
            vm.append(False)
        vm[channel] = enabled
        self.settings_changed.emit()
        self.v_sink_changed.emit(channel, enabled)

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
    # Theming (active_theme)
    # ------------------------------------------------------------------

    @property
    def active_theme(self) -> str:
        """Get the active KDE .colors theme file path or empty string/fallback name."""
        return str(self._data.get("settings", {}).get("active_theme", "System (Default)"))
        
    def set_active_theme(self, theme_path: str) -> None:
        """Set the active theme file path."""
        if "settings" not in self._data:
            self._data["settings"] = _default_settings(self.num_channels)
        self._data["settings"]["active_theme"] = theme_path
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
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"ConfigManager(path={self._path}, channels={self.num_channels})"
