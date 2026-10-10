import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import make_profile, write_profile  # noqa: E402


def _make_manager(profiles_dir: Path):
    from nativmix.utils.profile_manager import ProfileManager

    return ProfileManager(profiles_dir=profiles_dir)


# ── list_profiles ────────────────────────────────────────────────────────────


def test_list_profiles_empty(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    assert pm.list_profiles() == []


def test_list_profiles_returns_sorted(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B", 5))
    write_profile(tmp_profiles_dir, make_profile("profile-1", "A", 5))
    pm = _make_manager(tmp_profiles_dir)
    names = [p["name"] for p in pm.list_profiles()]
    assert names == ["A", "B"]


# ── load ─────────────────────────────────────────────────────────────────────


def test_load_returns_profile_dict(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=7))
    pm = _make_manager(tmp_profiles_dir)
    p = pm.load("profile-1")
    assert p["channel_count"] == 7
    assert p["name"] == "Profile 1"


def test_load_missing_raises(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    with pytest.raises(FileNotFoundError):
        pm.load("profile-99")


# ── create ────────────────────────────────────────────────────────────────────


def test_create_returns_new_id(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("My Profile", channel_count=5)
    assert new_id == "profile-1"
    assert (tmp_profiles_dir / "profile-1.json").exists()


def test_create_increments_id(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("Second", channel_count=5)
    assert new_id == "profile-2"


def test_create_profile_has_correct_fields(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("Test", channel_count=3)
    p = pm.load(new_id)
    assert p["name"] == "Test"
    assert p["channel_count"] == 3
    assert p["restore_fader_positions"] is False
    assert p["midi_switch_cc"] is None
    assert len(p["channels"]) == 3


# ── rename ────────────────────────────────────────────────────────────────────


def test_rename_updates_name(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", "Old"))
    pm = _make_manager(tmp_profiles_dir)
    pm.rename("profile-1", "New Name")
    assert pm.load("profile-1")["name"] == "New Name"


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_file(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B"))
    pm = _make_manager(tmp_profiles_dir)
    pm.delete("profile-1")
    assert not (tmp_profiles_dir / "profile-1.json").exists()


def test_delete_last_profile_raises(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    pm = _make_manager(tmp_profiles_dir)
    with pytest.raises(ValueError, match="last profile"):
        pm.delete("profile-1")


# ── save_current ──────────────────────────────────────────────────────────────


def test_save_current_updates_channels(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=2))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    updated = make_profile("profile-1", channel_count=2)
    updated["channels"][0]["app_names"] = ["spotify"]
    pm.save_current(updated["channels"])
    reloaded = pm.load("profile-1")
    assert reloaded["channels"][0]["app_names"] == ["spotify"]


def test_save_current_persists_channel_label(qtbot, tmp_profiles_dir):
    from nativmix.utils.profile_manager import ProfileManager, default_channels

    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pid = pm.create("Label Test", channel_count=2)
    pm.switch(pid)
    channels = default_channels(2)
    channels[0]["label"] = "Kick"
    pm.save_current(channels)
    loaded = pm.load(pid)
    assert loaded["channels"][0]["label"] == "Kick"


def test_save_current_merges_crossfader_kwarg(qtbot, tmp_profiles_dir):
    """save_current() accepts optional crossfader=... without breaking old call sites."""
    from nativmix.utils.profile_manager import ProfileManager, default_channels

    pm = ProfileManager(profiles_dir=tmp_profiles_dir)
    pid = pm.create("Crossfader Test", channel_count=2)
    pm.switch(pid)
    channels = default_channels(2)

    # Old-style call (positional only) must still work.
    pm.save_current(channels)
    loaded = pm.load(pid)
    assert loaded["crossfader_enabled"] is False

    # New-style call with crossfader kwarg merges root fields.
    pm.save_current(
        channels,
        crossfader={
            "crossfader_enabled": True,
            "crossfader_position": 0.25,
            "crossfader_usb_channel_index": 0,
            "crossfader_midi_cc": 20,
            "crossfader_midi_channel": 2,
        },
    )
    loaded = pm.load(pid)
    assert loaded["crossfader_enabled"] is True
    assert loaded["crossfader_position"] == 0.25
    assert loaded["crossfader_usb_channel_index"] == 0
    assert loaded["crossfader_midi_cc"] == 20
    assert loaded["crossfader_midi_channel"] == 2


# ── crossfader defaults ───────────────────────────────────────────────────────


def test_default_channel_has_cross_side():
    from nativmix.utils.profile_manager import default_channels

    assert default_channels(1)[0]["cross_side"] == "none"


def test_create_profile_has_crossfader_root_defaults(qtbot, tmp_profiles_dir):
    pm = _make_manager(tmp_profiles_dir)
    new_id = pm.create("Test", channel_count=3)
    p = pm.load(new_id)
    assert p["crossfader_enabled"] is False
    assert p["crossfader_position"] == 0.5
    assert p["crossfader_usb_channel_index"] is None
    assert p["crossfader_midi_cc"] is None
    assert p["crossfader_midi_channel"] == 0


def test_load_defaults_crossfader_fields_for_old_profile(qtbot, tmp_profiles_dir):
    """Profiles written before the crossfader feature load with safe defaults."""
    old_profile = make_profile("profile-1", channel_count=2)
    assert "crossfader_enabled" not in old_profile
    assert "cross_side" not in old_profile["channels"][0]
    write_profile(tmp_profiles_dir, old_profile)

    pm = _make_manager(tmp_profiles_dir)
    loaded = pm.load("profile-1")
    assert loaded["crossfader_enabled"] is False
    assert loaded["crossfader_position"] == 0.5
    assert loaded["crossfader_usb_channel_index"] is None
    assert loaded["channels"][0]["cross_side"] == "none"


# ── switch ────────────────────────────────────────────────────────────────────


def test_switch_sets_active_id(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1"))
    pm = _make_manager(tmp_profiles_dir)
    pm.switch("profile-1")
    assert pm.active_profile_id == "profile-1"


def test_switch_next_wraps(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", "A"))
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B"))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-2"
    pm.switch_next()
    assert pm.active_profile_id == "profile-1"


def test_switch_prev_wraps(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", "A"))
    write_profile(tmp_profiles_dir, make_profile("profile-2", "B"))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    pm.switch_prev()
    assert pm.active_profile_id == "profile-2"


# ── ensure_profile_for_hw ─────────────────────────────────────────────────────


def test_ensure_hw_no_new_profile_when_hw_bigger(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=5))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    pm.ensure_profile_for_hw(7)  # hw has MORE channels → no new profile
    assert pm.active_profile_id == "profile-1"
    assert len(pm.list_profiles()) == 1


def test_ensure_hw_creates_profile_when_hw_smaller(qtbot, tmp_profiles_dir):
    write_profile(tmp_profiles_dir, make_profile("profile-1", channel_count=7))
    pm = _make_manager(tmp_profiles_dir)
    pm._active_profile_id = "profile-1"
    pm.ensure_profile_for_hw(5)  # hw has FEWER channels → new profile
    assert pm.active_profile_id == "profile-2"
    assert len(pm.list_profiles()) == 2
    new_p = pm.load("profile-2")
    assert new_p["channel_count"] == 5


# ── Signal emissions ──────────────────────────────────────────────────────────


def test_switch_emits_profile_changed(pm, qtbot):
    pm.create("Second", 7)
    ids = [p["id"] for p in pm.list_profiles()]
    other = next(i for i in ids if i != pm.active_profile_id)
    with qtbot.waitSignal(pm.profile_changed, timeout=1000) as blocker:
        pm.switch(other)
    assert blocker.args == [other]


def test_create_emits_profile_list_changed(pm, qtbot):
    with qtbot.waitSignal(pm.profile_list_changed, timeout=1000):
        pm.create("New Profile", 7)


def test_rename_emits_profile_list_changed(pm, qtbot):
    profile_id = pm.active_profile_id
    with qtbot.waitSignal(pm.profile_list_changed, timeout=1000):
        pm.rename(profile_id, "Renamed")


def test_delete_emits_profile_list_changed(pm, qtbot):
    pm.create("Second", 7)
    ids = [p["id"] for p in pm.list_profiles()]
    other = next(i for i in ids if i != pm.active_profile_id)
    with qtbot.waitSignal(pm.profile_list_changed, timeout=1000):
        pm.delete(other)


def test_ensure_hw_emits_profile_changed(pm, qtbot):
    with qtbot.waitSignal(pm.profile_changed, timeout=1000):
        pm.ensure_profile_for_hw(3)  # active profile has 5 channels, hw has only 3 → new profile
    assert pm.active_profile_id != "profile-1"
