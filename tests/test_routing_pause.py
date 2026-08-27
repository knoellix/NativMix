"""Tests for per-app NativMix routing pause persistence."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


def test_routing_paused_defaults_false(tmp_path):
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager(config_path=tmp_path / "c.json", profiles_dir=tmp_path / "p")
    cfg.add_app_name(0, "Firefox")
    assert cfg.is_app_routing_paused(0, "Firefox") is False


def test_set_and_clear_routing_paused(tmp_path):
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager(config_path=tmp_path / "c.json", profiles_dir=tmp_path / "p")
    cfg.add_app_name(0, "Firefox")
    cfg.set_app_routing_paused(0, "Firefox", True)
    assert cfg.is_app_routing_paused(0, "Firefox") is True
    assert cfg.is_app_routing_paused(0, "firefox") is True  # case-insensitive
    cfg.set_app_routing_paused(0, "Firefox", False)
    assert cfg.is_app_routing_paused(0, "Firefox") is False


def test_remove_app_clears_routing_pause(tmp_path):
    from nativmix.utils.config_manager import ConfigManager

    cfg = ConfigManager(config_path=tmp_path / "c.json", profiles_dir=tmp_path / "p")
    cfg.add_app_name(0, "Firefox")
    cfg.set_app_routing_paused(0, "Firefox", True)
    cfg.remove_app_name(0, "Firefox")
    assert cfg.get_routing_paused_apps(0) == []
