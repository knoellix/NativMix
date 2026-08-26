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
