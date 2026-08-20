"""
XDG Desktop Portal theming helper for NativMix.

Reads the system color scheme and accent color via the
org.freedesktop.portal.Settings D-Bus interface so the app adapts to
CachyOS / KDE / GNOME theming without hard-coded DE-specific paths.

When Qt only exposes Fusion (typical Flatpak sandbox), NativMix applies a
dedicated light/dark fallback palette — not the host desktop theme.
"""

from __future__ import annotations

import logging
from enum import IntEnum

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage, QDBusVariant
from PyQt6.QtGui import QColor, QGuiApplication, QPalette

logger = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_PORTAL_INTERFACE = "org.freedesktop.portal.Settings"
_APPEARANCE_NS = "org.freedesktop.appearance"
_COLOR_SCHEME_KEY = "color-scheme"
_ACCENT_COLOR_KEY = "accent-color"

# Dedicated Fusion fallback colors (cool-blue accent, readable tooltips).
_DARK = {
    "window": "#171A1F",
    "window_text": "#D9E0EA",
    "base": "#262C36",
    "alternate": "#1F242C",
    "text": "#D9E0EA",
    "button": "#1F242C",
    "button_text": "#D9E0EA",
    "tooltip_base": "#1B222C",
    "tooltip_text": "#F0F5FB",
    "tooltip_border": "#3A4656",
    "highlight": "#3BA4E8",
    "highlighted_text": "#0F141B",
    "bright_text": "#D65C5C",
    # Cool blue-grey (matches window_text family — not warm charcoal)
    "disabled_text": "#8B97A8",
    "placeholder": "#8B97A8",
    "link": "#5BB8F0",
    "mid": "#2A313C",
    "midlight": "#343C48",
    "dark": "#10141A",
    "light": "#3E4754",
}
_LIGHT = {
    "window": "#EEF2F7",
    "window_text": "#1F2937",
    "base": "#F7FAFD",
    "alternate": "#E4EAF2",
    "text": "#1F2937",
    "button": "#E4EAF2",
    "button_text": "#1F2937",
    "tooltip_base": "#FFFFFF",
    "tooltip_text": "#111827",
    "tooltip_border": "#C5D0DD",
    "highlight": "#2F8FCF",
    "highlighted_text": "#FFFFFF",
    "bright_text": "#B84A4A",
    "disabled_text": "#8B97A8",
    "placeholder": "#8B97A8",
    "link": "#1D6FA8",
    "mid": "#D5DCE6",
    "midlight": "#E4EAF2",
    "dark": "#9AA5B4",
    "light": "#F7FAFD",
}


class ColorScheme(IntEnum):
    """
    XDG color-scheme values as specified by the portal spec.

    0 = no preference
    1 = prefer dark
    2 = prefer light
    """

    NO_PREFERENCE = 0
    DARK = 1
    LIGHT = 2


def qt_system_prefers_dark() -> bool | None:
    """Return Qt styleHints color preference, or None if unknown/unavailable."""
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception as exc:
        logger.debug("Qt styleHints colorScheme unavailable: %s", exc)
    return None


def resolve_prefer_dark(scheme: ColorScheme) -> bool:
    """Map portal ColorScheme to a concrete dark/light choice for Fusion fallback."""
    if scheme == ColorScheme.DARK:
        return True
    if scheme == ColorScheme.LIGHT:
        return False
    qt_pref = qt_system_prefers_dark()
    if qt_pref is not None:
        return qt_pref
    return False


def build_fusion_fallback_palette(prefer_dark: bool) -> QPalette:
    """Build the dedicated Flatpak/Fusion light or dark palette."""
    colors = _DARK if prefer_dark else _LIGHT
    palette = QPalette()

    def _set(role: QPalette.ColorRole, hex_color: str) -> None:
        color = QColor(hex_color)
        palette.setColor(QPalette.ColorGroup.Active, role, color)
        palette.setColor(QPalette.ColorGroup.Inactive, role, color)

    _set(QPalette.ColorRole.Window, colors["window"])
    _set(QPalette.ColorRole.WindowText, colors["window_text"])
    _set(QPalette.ColorRole.Base, colors["base"])
    _set(QPalette.ColorRole.AlternateBase, colors["alternate"])
    _set(QPalette.ColorRole.Text, colors["text"])
    _set(QPalette.ColorRole.Button, colors["button"])
    _set(QPalette.ColorRole.ButtonText, colors["button_text"])
    _set(QPalette.ColorRole.ToolTipBase, colors["tooltip_base"])
    _set(QPalette.ColorRole.ToolTipText, colors["tooltip_text"])
    _set(QPalette.ColorRole.Highlight, colors["highlight"])
    _set(QPalette.ColorRole.HighlightedText, colors["highlighted_text"])
    _set(QPalette.ColorRole.BrightText, colors["bright_text"])
    _set(QPalette.ColorRole.PlaceholderText, colors["placeholder"])
    _set(QPalette.ColorRole.Link, colors["link"])
    _set(QPalette.ColorRole.LinkVisited, colors["link"])
    _set(QPalette.ColorRole.Mid, colors["mid"])
    _set(QPalette.ColorRole.Midlight, colors["midlight"])
    _set(QPalette.ColorRole.Dark, colors["dark"])
    _set(QPalette.ColorRole.Light, colors["light"])
    _set(QPalette.ColorRole.Shadow, colors["dark"])

    disabled_text = QColor(colors["disabled_text"])
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.ToolTipText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ToolTipBase,
        QColor(colors["tooltip_base"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Button,
        QColor(colors["mid"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Base,
        QColor(colors["alternate"]),
    )
    return palette


def fusion_tooltip_stylesheet(prefer_dark: bool) -> str:
    """Minimal QToolTip stylesheet so Fusion tooltips stay readable in sandbox."""
    colors = _DARK if prefer_dark else _LIGHT
    return (
        "QToolTip {"
        f" color: {colors['tooltip_text']};"
        f" background-color: {colors['tooltip_base']};"
        f" border: 1px solid {colors['tooltip_border']};"
        " padding: 4px;"
        " }"
    )


def apply_fusion_fallback(
    app: QGuiApplication,
    prefer_dark: bool,
) -> None:
    """Apply Fusion fallback palette + tooltip stylesheet to *app*."""
    app.setPalette(build_fusion_fallback_palette(prefer_dark))
    # QApplication.setStyleSheet — keep narrow to QToolTip only.
    set_style = getattr(app, "setStyleSheet", None)
    if callable(set_style):
        set_style(fusion_tooltip_stylesheet(prefer_dark))
    logger.info("Applied Fusion fallback palette (%s)", "dark" if prefer_dark else "light")


class ThemeWatcher(QObject):
    """
    Watches the XDG Desktop Portal for color-scheme and accent-color changes.

    Emits signals when the system theme changes so Fusion fallback can update.

    Signals
    -------
    color_scheme_changed(ColorScheme)
        Fired when the user switches between dark and light mode.
    accent_color_changed(tuple[float, float, float])
        Fired when the system accent color changes.
        The tuple contains (red, green, blue) in the range [0.0, 1.0].
    """

    color_scheme_changed = pyqtSignal(int)  # ColorScheme value
    accent_color_changed = pyqtSignal(object)  # (r, g, b) float tuple

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._scheme = ColorScheme.NO_PREFERENCE
        self._accent: tuple[float, float, float] = (0.38, 0.68, 1.0)  # default blue
        self._iface: QDBusInterface | None = None
        self._connected = False

    def start(self) -> None:
        """Connect to the session bus and read initial values."""
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            logger.warning("ThemeWatcher: session D-Bus not available; using defaults")
            return

        self._iface = QDBusInterface(_PORTAL_SERVICE, _PORTAL_PATH, _PORTAL_INTERFACE, bus)
        if not self._iface.isValid():
            logger.warning(
                "ThemeWatcher: portal interface not available (%s); using defaults",
                self._iface.lastError().message(),
            )
            self._iface = None
            return

        self._scheme = self._read_color_scheme()
        self._accent = self._read_accent_color()

        bus.connect(
            _PORTAL_SERVICE,
            _PORTAL_PATH,
            _PORTAL_INTERFACE,
            "SettingChanged",
            self._on_setting_changed,
        )
        self._connected = True
        logger.debug(
            "ThemeWatcher: connected (scheme=%s, accent=%.2f,%.2f,%.2f)",
            self._scheme.name,
            *self._accent,
        )

    def stop(self) -> None:
        """Disconnect from the session bus."""
        if self._connected:
            QDBusConnection.sessionBus().disconnect(
                _PORTAL_SERVICE,
                _PORTAL_PATH,
                _PORTAL_INTERFACE,
                "SettingChanged",
                self._on_setting_changed,
            )
            self._connected = False

    @property
    def color_scheme(self) -> ColorScheme:
        """Current resolved color scheme."""
        return self._scheme

    @property
    def is_dark(self) -> bool:
        """True when the resolved preference is dark (portal + Qt fallback)."""
        return resolve_prefer_dark(self._scheme)

    @property
    def accent(self) -> tuple[float, float, float]:
        """Current accent color as (r, g, b) floats in [0.0, 1.0]."""
        return self._accent

    def accent_hex(self) -> str:
        """Current accent color as a CSS hex string, e.g. '#6199ff'."""
        r, g, b = (int(c * 255) for c in self._accent)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _read_portal_value(self, namespace: str, key: str):
        """Call org.freedesktop.portal.Settings.Read and return the inner variant."""
        if self._iface is None:
            return None
        reply: QDBusMessage = self._iface.call("Read", namespace, key)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            logger.debug("Portal Read(%s, %s) error: %s", namespace, key, reply.errorMessage())
            return None
        args = reply.arguments()
        if not args:
            return None
        outer = args[0]
        inner = outer.variant() if hasattr(outer, "variant") else outer
        value = inner.variant() if hasattr(inner, "variant") else inner
        return value

    def _read_color_scheme(self) -> ColorScheme:
        value = self._read_portal_value(_APPEARANCE_NS, _COLOR_SCHEME_KEY)
        try:
            return ColorScheme(int(value))
        except (TypeError, ValueError):
            return ColorScheme.NO_PREFERENCE

    def _read_accent_color(self) -> tuple[float, float, float]:
        value = self._read_portal_value(_APPEARANCE_NS, _ACCENT_COLOR_KEY)
        try:
            r, g, b = float(value[0]), float(value[1]), float(value[2])
            return (r, g, b)
        except (TypeError, IndexError, ValueError):
            return (0.38, 0.68, 1.0)

    @pyqtSlot(str, str, QDBusVariant)
    def _on_setting_changed(self, namespace: str, key: str, value: QDBusVariant) -> None:
        """D-Bus slot: fires when any portal setting changes."""
        raw = value.variant()
        if namespace != _APPEARANCE_NS:
            return
        if key == _COLOR_SCHEME_KEY:
            try:
                self._scheme = ColorScheme(int(raw))
            except (TypeError, ValueError):
                return
            logger.debug("Color scheme changed: %s", self._scheme.name)
            self.color_scheme_changed.emit(int(self._scheme))
        elif key == _ACCENT_COLOR_KEY:
            try:
                self._accent = (float(raw[0]), float(raw[1]), float(raw[2]))
            except (TypeError, IndexError, ValueError):
                return
            logger.debug("Accent color changed: %.2f,%.2f,%.2f", *self._accent)
            self.accent_color_changed.emit(self._accent)
