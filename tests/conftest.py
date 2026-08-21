import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Headless Qt for CI/sandbox environments without a display server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


@pytest.fixture
def pm(tmp_path: Path):
    from nativmix.utils.profile_manager import ProfileManager

    d = tmp_path / "profiles_pm"
    d.mkdir()
    manager = ProfileManager(profiles_dir=d)
    # Create a default profile so there is always an active one
    profile_id = manager.create("Default", channel_count=5)
    manager._active_profile_id = profile_id
    return manager


@pytest.fixture
def tmp_profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    return d


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def make_profile(
    profile_id: str = "profile-1",
    name: str = "Profile 1",
    channel_count: int = 5,
    restore_fader_positions: bool = False,
    midi_switch_cc: int | None = None,
    channels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if channels is None:
        channels = [
            {
                "index": i,
                "label": None,
                "is_midi": False,
                "app_names": [],
                "midi_cc": None,
                "midi_mute_cc": None,
                "midi_channel": 0,
                "midi_mute_channel": 0,
                "inverted": False,
                "v_sink": False,
                "mode": "app",
                "hardware_id": None,
                "volume": 1.0,
            }
            for i in range(channel_count)
        ]
    return {
        "id": profile_id,
        "name": name,
        "channel_count": channel_count,
        "restore_fader_positions": restore_fader_positions,
        "midi_switch_cc": midi_switch_cc,
        "channels": channels,
    }


def write_profile(profiles_dir: Path, profile: dict[str, Any]) -> Path:
    p = profiles_dir / f"{profile['id']}.json"
    p.write_text(json.dumps(profile, indent=2))
    return p
