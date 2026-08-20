"""Tests for XDG portal theming helpers and Fusion fallback palettes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PyQt6.QtGui import QColor, QPalette

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from nativmix.gui.theme import (
    ColorScheme,
    ThemeWatcher,
    build_fusion_fallback_palette,
    fusion_tooltip_stylesheet,
    resolve_prefer_dark,
)


def _relative_luminance(color: QColor) -> float:
    def _channel(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = color.red(), color.green(), color.blue()
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast_ratio(fg: QColor, bg: QColor) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, ColorScheme.NO_PREFERENCE),
        (1, ColorScheme.DARK),
        (2, ColorScheme.LIGHT),
    ],
)
def test_color_scheme_from_portal_value(value: int, expected: ColorScheme) -> None:
    assert ColorScheme(value) == expected


def test_resolve_prefer_dark_portal_dark() -> None:
    assert resolve_prefer_dark(ColorScheme.DARK) is True


def test_resolve_prefer_dark_portal_light() -> None:
    assert resolve_prefer_dark(ColorScheme.LIGHT) is False


@pytest.mark.parametrize(
    ("scheme", "expected_when_qt_unknown"),
    [
        (ColorScheme.DARK, True),
        (ColorScheme.LIGHT, False),
    ],
)
def test_theme_watcher_is_dark_follows_portal(qtbot, scheme: ColorScheme, expected_when_qt_unknown: bool) -> None:
    watcher = ThemeWatcher()
    watcher._scheme = scheme
    assert watcher.is_dark is expected_when_qt_unknown


def test_theme_watcher_default_accent_is_tuple(qtbot) -> None:
    watcher = ThemeWatcher()
    assert len(watcher.accent) == 3
    assert all(0.0 <= c <= 1.0 for c in watcher.accent)


def test_theme_watcher_accent_hex_format(qtbot) -> None:
    watcher = ThemeWatcher()
    watcher._accent = (0.23, 0.64, 0.91)
    assert watcher.accent_hex() == "#3aa3e8"


@pytest.mark.parametrize("prefer_dark", [True, False])
def test_fusion_palette_tooltip_contrast(prefer_dark: bool) -> None:
    palette = build_fusion_fallback_palette(prefer_dark)
    tip_fg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ToolTipText)
    tip_bg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ToolTipBase)
    # WCAG AA for normal text is 4.5:1; keep a comfortable margin for Fusion.
    assert _contrast_ratio(tip_fg, tip_bg) >= 7.0


@pytest.mark.parametrize("prefer_dark", [True, False])
def test_fusion_palette_window_text_contrast(prefer_dark: bool) -> None:
    palette = build_fusion_fallback_palette(prefer_dark)
    fg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    bg = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window)
    assert _contrast_ratio(fg, bg) >= 7.0


@pytest.mark.parametrize("prefer_dark", [True, False])
def test_fusion_tooltip_stylesheet_contains_colors(prefer_dark: bool) -> None:
    css = fusion_tooltip_stylesheet(prefer_dark)
    assert "QToolTip" in css
    assert "background-color:" in css
    assert "color:" in css
