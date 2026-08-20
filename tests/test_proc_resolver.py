"""Tests for process-name resolution helpers."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.utils.proc_resolver import _extract_flag, _match_user_data_dir


def test_match_user_data_dir_spotify() -> None:
    assert _match_user_data_dir("/home/user/.config/spotify") == "Spotify"


def test_match_user_data_dir_unknown_returns_none() -> None:
    assert _match_user_data_dir("/tmp/random-app-data") is None


def test_extract_flag_app_id() -> None:
    args = ["electron", "--app-id=com.spotify.client", "--some-flag"]
    pattern = re.compile(r"--app-id=([^\s]+)")
    assert _extract_flag(args, pattern) == "com.spotify.client"
