"""
Flatpak autostart via XDG Desktop Portal (org.freedesktop.portal.Background).

Host installs keep systemd / XDG ~/.config/autostart — this module is only used
inside Flatpak. Marshalling rules for options a{sv}:
- native bool/str values (do not pre-wrap in QDBusVariant — Qt wraps once)
- commandline must be typed as D-Bus 'as' (string array), not 'av'
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from PyQt6.QtCore import QEventLoop, QMetaType, QObject, QTimer, pyqtSlot
from PyQt6.QtDBus import (
    QDBusArgument,
    QDBusConnection,
    QDBusInterface,
    QDBusMessage,
    QDBusObjectPath,
)

logger = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_BACKGROUND_IFACE = "org.freedesktop.portal.Background"
_REQUEST_IFACE = "org.freedesktop.portal.Request"

_FLATPAK_APP_ID = os.environ.get("FLATPAK_ID", "net.knoellix.NativMix")
_AUTOSTART_DESKTOP_NAME = f"{_FLATPAK_APP_ID}.desktop"
_COMMANDLINE = ["nativmix", "--hidden"]
_REQUEST_TIMEOUT_MS = 30_000


def _host_autostart_desktop() -> Path:
    """Host ~/.config/autostart/<app-id>.desktop (needs xdg-config/autostart access)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "autostart" / _AUTOSTART_DESKTOP_NAME
    return Path.home() / ".config" / "autostart" / _AUTOSTART_DESKTOP_NAME


def is_portal_autostart_enabled() -> bool:
    """Best-effort: True when the portal-created host autostart desktop exists."""
    path = _host_autostart_desktop()
    try:
        return path.is_file()
    except OSError as exc:
        logger.debug("Could not check Flatpak autostart desktop: %s", exc)
        return False


def portal_background_available() -> bool:
    """True when the session exposes org.freedesktop.portal.Background."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    iface = QDBusInterface(_PORTAL_SERVICE, _PORTAL_PATH, _BACKGROUND_IFACE, bus)
    return iface.isValid()


def _string_array(values: list[str]) -> QDBusArgument:
    """Build a D-Bus 'as' argument (required for commandline)."""
    arg = QDBusArgument()
    arg.beginArray(QMetaType.Type.QString.value)
    for value in values:
        arg.add(value)
    arg.endArray()
    return arg


def _unwrap_results(raw) -> dict:
    """Normalize portal results a{sv} into a plain dict."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        out[key] = value.variant() if hasattr(value, "variant") else value
    return out


class _PortalRequestWatcher(QObject):
    """Collects org.freedesktop.portal.Request.Response and quits an event loop."""

    def __init__(self, loop: QEventLoop, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop = loop
        self.response_code: int | None = None
        self.results: dict = {}

    @pyqtSlot(int, object)
    def on_response(self, response: int, results: object) -> None:
        self.response_code = int(response)
        self.results = _unwrap_results(results)
        logger.debug(
            "Portal Background Response: code=%s results=%s",
            self.response_code,
            self.results,
        )
        self._loop.quit()


def request_portal_autostart(enable: bool) -> tuple[bool, bool, str]:
    """
    Ask the Background portal to enable/disable login autostart.

    Returns (ok, autostart_enabled, message).
    """
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False, False, "Session D-Bus is not available."

    iface = QDBusInterface(_PORTAL_SERVICE, _PORTAL_PATH, _BACKGROUND_IFACE, bus)
    if not iface.isValid():
        err = iface.lastError().message() if iface.lastError().isValid() else "unknown"
        logger.warning("Background portal unavailable: %s", err)
        return (
            False,
            False,
            "Desktop Background portal is not available. "
            "Install a portal backend (e.g. xdg-desktop-portal-kde) "
            "or enable autostart from system settings.",
        )

    token = f"nativmix{uuid.uuid4().hex[:8]}"
    options = {
        "handle_token": token,
        "reason": "Start NativMix automatically when you log in",
        "autostart": bool(enable),
        "commandline": _string_array(list(_COMMANDLINE)),
    }

    reply: QDBusMessage = iface.call("RequestBackground", "", options)
    if reply.type() == QDBusMessage.MessageType.ErrorMessage:
        logger.error("RequestBackground failed: %s", reply.errorMessage())
        return False, False, f"Autostart request failed: {reply.errorMessage()}"

    args = reply.arguments()
    if not args:
        return False, False, "Autostart request returned no handle."

    handle = args[0]
    handle_path = handle.path() if isinstance(handle, QDBusObjectPath) else str(handle)

    loop = QEventLoop()
    watcher = _PortalRequestWatcher(loop)
    connected = bus.connect(
        _PORTAL_SERVICE,
        handle_path,
        _REQUEST_IFACE,
        "Response",
        watcher.on_response,
    )
    if not connected:
        logger.debug("Could not subscribe to Request.Response; checking desktop file")
        return True, is_portal_autostart_enabled(), ""

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(_REQUEST_TIMEOUT_MS)
    loop.exec()
    timer.stop()
    bus.disconnect(_PORTAL_SERVICE, handle_path, _REQUEST_IFACE, "Response", watcher.on_response)

    if watcher.response_code is None:
        return False, is_portal_autostart_enabled(), "Autostart request timed out."

    if watcher.response_code == 1:
        return False, is_portal_autostart_enabled(), "Autostart request was cancelled."

    if watcher.response_code != 0:
        return (
            False,
            is_portal_autostart_enabled(),
            f"Autostart request failed (portal code {watcher.response_code}).",
        )

    autostart = bool(watcher.results.get("autostart", enable))
    if enable and not autostart:
        autostart = is_portal_autostart_enabled()
    return True, autostart, ""
