"""Tests for Flatpak runtime detection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils import paths as paths_mod


def test_is_flatpak_true_with_env(monkeypatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "net.knoellix.NativMix")
    assert paths_mod.is_flatpak() is True


def test_is_flatpak_false_without_env_or_marker(monkeypatch) -> None:
    monkeypatch.delenv("FLATPAK_ID", raising=False)

    class _FakePath:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def is_file(self) -> bool:
            return False

    monkeypatch.setattr(paths_mod, "Path", _FakePath)
    assert paths_mod.is_flatpak() is False
