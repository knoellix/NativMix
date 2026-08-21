"""Unit tests for GitHub release update-hint helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils.update_check import is_newer, normalize_version, should_notify


def test_normalize_version_strips_v_prefix() -> None:
    assert normalize_version("v1.0.19") == "1.0.19"
    assert normalize_version("1.0.19") == "1.0.19"
    assert normalize_version("V2.0.0") == "2.0.0"


def test_is_newer() -> None:
    assert is_newer("1.0.19", "1.0.18")
    assert not is_newer("1.0.18", "1.0.18")
    assert not is_newer("1.0.17", "1.0.18")
    assert is_newer("v1.0.19", "1.0.18")


def test_should_notify_master_off() -> None:
    assert not should_notify(
        remote="1.0.19",
        local="1.0.18",
        dismissed=None,
        checks_enabled=False,
    )


def test_should_notify_same_or_older() -> None:
    assert not should_notify(
        remote="1.0.18",
        local="1.0.18",
        dismissed=None,
        checks_enabled=True,
    )


def test_should_notify_when_newer() -> None:
    assert should_notify(
        remote="1.0.19",
        local="1.0.18",
        dismissed=None,
        checks_enabled=True,
    )


def test_should_notify_silenced_for_this_version_only() -> None:
    assert not should_notify(
        remote="1.0.19",
        local="1.0.18",
        dismissed="1.0.19",
        checks_enabled=True,
    )
    # Next release still notifies
    assert should_notify(
        remote="1.0.20",
        local="1.0.18",
        dismissed="1.0.19",
        checks_enabled=True,
    )


def test_later_without_silence_still_notifies() -> None:
    """Closing with Later and no checkbox leaves dismissed unset → remind again."""
    assert should_notify(
        remote="1.0.19",
        local="1.0.18",
        dismissed=None,
        checks_enabled=True,
    )
