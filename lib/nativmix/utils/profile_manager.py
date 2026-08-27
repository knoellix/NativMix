from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

_DEFAULT_CHANNELS_COUNT = 5


def _next_profile_id(profiles_dir: Path) -> str:
    existing = {
        int(p.stem.split("-")[1]) for p in profiles_dir.glob("profile-*.json") if p.stem.split("-")[1].isdigit()
    }
    n = 1
    while n in existing:
        n += 1
    return f"profile-{n}"


def default_channels(count: int) -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "label": None,
            "is_midi": False,
            "app_names": [],
            "midi_cc": None,
            "midi_mute_cc": None,
            "midi_channel": 0,
            "midi_mute_channel": 0,
            "midi_bindings": [{"cc": None, "midi_channel": 0}],
            "inverted": False,
            "v_sink": False,
            "mode": "app",
            "hardware_id": None,
            "volume": 1.0,
            "routing_paused_apps": [],
        }
        for i in range(count)
    ]


class ProfileManager(QObject):
    """
    Manages NativMix profiles stored as individual JSON files.

    Each profile contains channel-level configuration (app assignments,
    MIDI CC, labels, fader positions). Global hardware settings remain
    in config.json and are NOT part of any profile.
    """

    profile_changed = pyqtSignal(str)  # profile_id — emitted after every switch
    profile_list_changed = pyqtSignal()  # emitted after create / rename / delete

    def __init__(
        self,
        profiles_dir: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if profiles_dir is None:
            from nativmix.utils.paths import get_config_dir

            profiles_dir = get_config_dir() / "profiles"
        self._dir = profiles_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._active_profile_id: str = ""
        self._direct_cc_map: dict[int, str] = {}
        self._rebuild_direct_cc_map()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def active_profile_id(self) -> str:
        return self._active_profile_id

    def set_active_silently(self, profile_id: str) -> None:
        """Set the active profile ID without emitting profile_changed."""
        self._active_profile_id = profile_id

    @property
    def direct_cc_map(self) -> dict[int, str]:
        """Mapping of MIDI CC number → profile_id for direct-switch CCs."""
        return dict(self._direct_cc_map)

    @property
    def active_profile(self) -> dict:
        if not self._active_profile_id:
            raise RuntimeError("No active profile set")
        return self.load(self._active_profile_id)

    # ── File I/O ──────────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict]:
        """Return all profiles sorted by numeric ID."""
        profiles = []
        for p in sorted(self._dir.glob("profile-*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                profiles.append(
                    {
                        "id": data.get("id", p.stem),
                        "name": data.get("name", p.stem),
                        "channel_count": data.get("channel_count", 0),
                    }
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read profile %s: %s", p, exc)
        return profiles

    def load(self, profile_id: str) -> dict:
        """Load a profile dict from disk without activating it."""
        path = self._dir / f"{profile_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_profile(self, profile: dict) -> None:
        path = self._dir / f"{profile['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to write profile %s: %s", profile.get("id"), exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _rebuild_direct_cc_map(self) -> None:
        """Rebuild the in-memory CC→profile_id map from all profile files."""
        cc_map: dict[int, str] = {}
        for p in self._dir.glob("profile-*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cc = data.get("midi_switch_cc")
                if cc is not None:
                    cc_map[int(cc)] = data.get("id", p.stem)
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                logger.debug("Could not read profile %s for CC map: %s", p, exc)
        self._direct_cc_map = cc_map

    def save_profile(self, profile: dict) -> None:
        """Write a profile dict to disk. The profile must have a valid 'id' field."""
        self._save_profile(profile)
        self._rebuild_direct_cc_map()

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        channel_count: int = _DEFAULT_CHANNELS_COUNT,
        channels: list[dict] | None = None,
        channel_order: list[int] | None = None,
    ) -> str:
        """Create a new profile and return its ID.

        If *channels* is provided, it is used as-is; otherwise blank defaults
        are generated from *channel_count*.
        """
        new_id = _next_profile_id(self._dir)
        chans = channels if channels is not None else default_channels(channel_count)
        if channel_order is None:
            order = list(range(len(chans)))
        else:
            order = list(channel_order)
        profile = {
            "id": new_id,
            "name": name,
            "channel_count": channel_count,
            "restore_fader_positions": False,
            "midi_switch_cc": None,
            "channels": chans,
            "channel_order": order,
        }
        self._save_profile(profile)
        self._rebuild_direct_cc_map()
        logger.debug("Profile created: %s (%s)", new_id, name)
        self.profile_list_changed.emit()
        return new_id

    def rename(self, profile_id: str, name: str) -> None:
        """Rename a profile (updates name field, ID stays the same)."""
        profile = self.load(profile_id)
        profile["name"] = name
        self._save_profile(profile)
        logger.debug("Profile renamed: %s → %s", profile_id, name)
        self.profile_list_changed.emit()

    def delete(self, profile_id: str) -> None:
        """Delete a profile. Raises ValueError if it is the last one."""
        if len(self.list_profiles()) <= 1:
            raise ValueError("Cannot delete the last profile")
        path = self._dir / f"{profile_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            logger.debug("Profile file already gone: %s", path)
        self._rebuild_direct_cc_map()
        logger.debug("Profile deleted: %s", profile_id)
        self.profile_list_changed.emit()

    def save_current(
        self,
        channels: list[dict],
        channel_order: list[int] | None = None,
    ) -> None:
        """Persist the current channel state back to the active profile file."""
        if not self._active_profile_id:
            return
        profile = self.load(self._active_profile_id)
        profile["channels"] = channels
        if channel_order is not None:
            profile["channel_order"] = list(channel_order)
        elif "channel_order" not in profile:
            profile["channel_order"] = list(range(len(channels)))
        self._save_profile(profile)
        logger.debug("Profile saved: %s", self._active_profile_id)

    # ── Switching ─────────────────────────────────────────────────────────

    def switch(self, profile_id: str) -> None:
        """Activate a profile. Emits profile_changed. No-op if already active."""
        if profile_id == self._active_profile_id:
            logger.info("Already on profile %s — no switch needed", profile_id)
            return
        profile = self.load(profile_id)
        self._active_profile_id = profile_id
        logger.info("Profile switched to: %s (%s)", profile_id, profile.get("name"))
        self.profile_changed.emit(profile_id)

    def switch_next(self) -> None:
        """Switch to the next profile (wraps around). No-op if only one profile."""
        profiles = self.list_profiles()
        if not profiles:
            raise RuntimeError("No profiles available")
        if len(profiles) == 1:
            logger.info("Only one profile available — cannot switch to next")
            return
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(self._active_profile_id)
        except ValueError:
            idx = -1
        self.switch(ids[(idx + 1) % len(ids)])

    def switch_prev(self) -> None:
        """Switch to the previous profile (wraps around). No-op if only one profile."""
        profiles = self.list_profiles()
        if not profiles:
            raise RuntimeError("No profiles available")
        if len(profiles) == 1:
            logger.info("Only one profile available — cannot switch to previous")
            return
        ids = [p["id"] for p in profiles]
        try:
            idx = ids.index(self._active_profile_id)
        except ValueError:
            idx = 0
        self.switch(ids[(idx - 1) % len(ids)])

    # ── Auto-create ────────────────────────────────────────────────────────

    def ensure_profile_for_hw(self, hw_channel_count: int) -> None:
        """
        Ensure an active profile compatible with hw_channel_count exists.

        If hw_channel_count is smaller than the active profile's channel_count,
        auto-creates a new profile sized to hw_channel_count and activates it.
        Called on startup and port change only, never on manual switch.
        """
        if not self._active_profile_id:
            return

        try:
            active = self.load(self._active_profile_id)
        except FileNotFoundError:
            return

        if hw_channel_count < active.get("channel_count", 0):
            names = {p["name"] for p in self.list_profiles()}
            n = len(names) + 1
            candidate = f"Profile {n}"
            while candidate in names:
                n += 1
                candidate = f"Profile {n}"
            new_id = self.create(candidate, channel_count=hw_channel_count)
            logger.info(
                "Hardware has %d channels, active profile needs %d — auto-created %s",
                hw_channel_count,
                active["channel_count"],
                new_id,
            )
            self.switch(new_id)
