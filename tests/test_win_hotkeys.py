"""Tests for Windows mute hotkey helpers (parse / uniqueness; no WinAPI required)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence

from nativmix.utils.config_manager import ConfigManager
from nativmix.utils.win_hotkeys import hotkey_to_win_mods_vk, normalize_hotkey


def test_normalize_hotkey_f13() -> None:
    assert normalize_hotkey("F13") == "F13"
    assert normalize_hotkey("  f13  ") == "F13"


def test_normalize_hotkey_rejects_modifier_only() -> None:
    assert normalize_hotkey("Ctrl") is None
    assert normalize_hotkey("") is None
    assert normalize_hotkey(None) is None


def test_normalize_hotkey_from_sequence() -> None:
    seq = QKeySequence(Qt.Key.Key_F14)
    assert normalize_hotkey(seq) == "F14"


def test_hotkey_to_win_mods_vk_f13() -> None:
    parsed = hotkey_to_win_mods_vk("F13")
    assert parsed is not None
    mods, vk = parsed
    assert vk == 0x7C  # VK_F13
    assert mods & 0x4000  # MOD_NOREPEAT


def test_hotkey_to_win_mods_vk_ctrl_m() -> None:
    parsed = hotkey_to_win_mods_vk("Ctrl+M")
    assert parsed is not None
    mods, vk = parsed
    assert vk == ord("M")
    assert mods & 0x0002  # MOD_CONTROL


def test_set_mute_hotkey_unique(tmp_path: Path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    # Ensure at least two channels exist in memory
    _ = cfg.get_mute_hotkey(0)
    _ = cfg.get_mute_hotkey(1)
    cfg.set_mute_hotkey(0, "F13")
    cfg.set_mute_hotkey(1, "F13")
    assert cfg.get_mute_hotkey(0) is None
    assert cfg.get_mute_hotkey(1) == "F13"
    mapping = cfg.get_all_mute_hotkeys()
    assert mapping == {"F13": 1}


def test_clear_mute_hotkey(tmp_path: Path) -> None:
    cfg = ConfigManager(config_path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles")
    cfg.set_mute_hotkey(0, "F15")
    cfg.set_mute_hotkey(0, None)
    assert cfg.get_mute_hotkey(0) is None
    assert cfg.get_all_mute_hotkeys() == {}
