"""Unit tests for .github/scripts/release_notes.py (release body merge)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release_notes.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rn():
    return _load()


def test_extract_and_classify_current_changelog(rn) -> None:
    bullets = rn.extract_changelog_bullets("1.1.1")
    assert any("crossfader" in b.lower() for b in bullets)
    classified = rn.classify(bullets)
    # v1.1.1 has no Windows/Flatpak-tagged lines — all linux/general
    assert classified["linux"]
    assert classified["windows"] == []
    assert classified["flatpak"] == []


def test_classify_windows_and_flatpak_lines(rn) -> None:
    bullets = [
        "- Feat (Windows): mute hotkey",
        "- Flatpak: portal autostart",
        "- Fix: general pipewire bug",
    ]
    c = rn.classify(bullets)
    assert c["windows"] == ["- Feat (Windows): mute hotkey"]
    assert c["flatpak"] == ["- Flatpak: portal autostart"]
    assert c["linux"] == ["- Fix: general pipewire bug"]


def test_merge_upserts_changelog_once_and_channel_sections(rn) -> None:
    v = "1.1.1"
    body1 = rn.upsert("", v, "aur")
    assert body1.count(rn.MARKER["changelog"]) == 1
    assert rn.MARKER["aur"] in body1
    assert rn.MARKER["windows"] not in body1

    # Simulate stale auto-generated notes + second channel
    dirty = body1 + "\n\n## What's Changed\n* fake commit by bot\n"
    body2 = rn.upsert(dirty, v, "windows")
    assert body2.count(rn.MARKER["changelog"]) == 1
    assert body2.count(rn.MARKER["aur"]) == 1
    assert body2.count(rn.MARKER["windows"]) == 1
    assert "What's Changed" not in body2
    assert "fake commit" not in body2

    body3 = rn.upsert(body2, v, "flatpak")
    assert body3.count(rn.MARKER["changelog"]) == 1
    assert body3.count(rn.MARKER["aur"]) == 1
    assert body3.count(rn.MARKER["windows"]) == 1
    assert body3.count(rn.MARKER["flatpak"]) == 1
    # Stable order
    assert body3.index(rn.MARKER["changelog"]) < body3.index(rn.MARKER["aur"])
    assert body3.index(rn.MARKER["aur"]) < body3.index(rn.MARKER["windows"])
    assert body3.index(rn.MARKER["windows"]) < body3.index(rn.MARKER["flatpak"])


def test_merge_replaces_channel_section_idempotent(rn) -> None:
    v = "1.1.1"
    once = rn.upsert("", v, "aur")
    twice = rn.upsert(once, v, "aur")
    assert once.count(rn.MARKER["aur"]) == 1
    assert twice.count(rn.MARKER["aur"]) == 1
    assert twice.count(rn.MARKER["changelog"]) == 1
