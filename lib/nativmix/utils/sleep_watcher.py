"""
System-sleep watcher via systemd-logind (PrepareForSleep).

Closes hardware resources before suspend so USB controllers (xHCI) are not
kept busy by an open serial handle.  DE-agnostic; uses the system bus only.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection

logger = logging.getLogger(__name__)

_LOGIN1_SERVICE = "org.freedesktop.login1"
_LOGIN1_PATH = "/org/freedesktop/login1"
_LOGIN1_INTERFACE = "org.freedesktop.login1.Manager"
_PREPARE_FOR_SLEEP = "PrepareForSleep"


class SleepWatcher(QObject):
    """
    Emits ``preparing_for_sleep`` / ``resumed_from_sleep`` around system suspend.

    logind sends ``PrepareForSleep(true)`` before sleep and ``false`` after
    resume.  If logind is unavailable the watcher stays idle (CI / non-Linux).
    """

    preparing_for_sleep = pyqtSignal()
    resumed_from_sleep = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._connected = False

    def start(self) -> None:
        """Subscribe to logind PrepareForSleep on the system bus."""
        if self._connected:
            return

        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            logger.warning("SleepWatcher: system D-Bus not available; sleep release disabled")
            return

        ok = bus.connect(
            _LOGIN1_SERVICE,
            _LOGIN1_PATH,
            _LOGIN1_INTERFACE,
            _PREPARE_FOR_SLEEP,
            self._on_prepare_for_sleep,
        )
        if not ok:
            logger.warning(
                "SleepWatcher: failed to connect to %s.%s",
                _LOGIN1_INTERFACE,
                _PREPARE_FOR_SLEEP,
            )
            return

        self._connected = True
        logger.info("SleepWatcher: listening for logind PrepareForSleep")

    def stop(self) -> None:
        """Unsubscribe from PrepareForSleep."""
        if not self._connected:
            return
        QDBusConnection.systemBus().disconnect(
            _LOGIN1_SERVICE,
            _LOGIN1_PATH,
            _LOGIN1_INTERFACE,
            _PREPARE_FOR_SLEEP,
            self._on_prepare_for_sleep,
        )
        self._connected = False
        logger.debug("SleepWatcher: disconnected")

    @pyqtSlot(bool)
    def _on_prepare_for_sleep(self, sleeping: bool) -> None:
        """D-Bus slot: True before suspend, False after resume."""
        try:
            if sleeping:
                logger.info("SleepWatcher: PrepareForSleep(true)")
                self.preparing_for_sleep.emit()
            else:
                logger.info("SleepWatcher: PrepareForSleep(false)")
                self.resumed_from_sleep.emit()
        except Exception:
            logger.exception("SleepWatcher: error handling PrepareForSleep(%s)", sleeping)
