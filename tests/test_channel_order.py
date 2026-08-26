import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils.channel_order import normalize_channel_order, order_after_remove  # noqa: E402


def test_none_order_is_natural():
    assert normalize_channel_order(None, [0, 1, 2]) == [0, 1, 2]


def test_keeps_permutation():
    assert normalize_channel_order([2, 0, 1], [0, 1, 2]) == [2, 0, 1]


def test_drops_unknown_and_appends_missing():
    assert normalize_channel_order([9, 1, 0], [0, 1, 2]) == [1, 0, 2]


def test_empty_order_is_natural():
    assert normalize_channel_order([], [0, 1]) == [0, 1]


def test_order_after_remove_middle():
    assert order_after_remove([0, 2, 1, 3], 1) == [0, 1, 2]


def test_save_current_persists_channel_order(qtbot, tmp_profiles_dir):
    from conftest import make_profile, write_profile

    from nativmix.utils.profile_manager import ProfileManager

    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=3))
    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pm.set_active_silently("profile-1")
    channels = make_profile(channel_count=3)["channels"]
    pm.save_current(channels, [2, 0, 1])
    loaded = pm.load("profile-1")
    assert loaded["channel_order"] == [2, 0, 1]


def test_config_apply_profile_loads_channel_order(qtbot, tmp_path):
    from conftest import make_profile

    from nativmix.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config.json"
    config = ConfigManager(config_path=cfg_path, profiles_dir=tmp_path / "profiles")
    profile = make_profile(channel_count=3, channel_order=[2, 0, 1])
    config.apply_profile(profile)
    assert config.get_channel_order() == [2, 0, 1]
    config.set_channel_order([1, 2, 0])
    assert config.get_channel_order() == [1, 2, 0]
