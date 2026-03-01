"""
System tray icon for NativMix.

Implements Rule 3:
- Uses the application icon (nativmix.svg / installed icon theme name).
- Left-click toggles the main window.
- Right-click shows a context menu (Settings, Quit).

app.setQuitOnLastWindowClosed(False) is set in main.py so that closing the
main window only hides it – the tray icon keeps the app alive.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from nativmix.utils.paths import get_icon_path

logger = logging.getLogger(__name__)


class TrayIcon(QSystemTrayIcon):
    """
    System tray icon that controls the main window visibility.

    Parameters
    ----------
    main_window:
        The MainWindow instance to show/hide on left-click.
    parent:
        Optional Qt parent.
    """

    def __init__(self, main_window, parent: QObject | None = None) -> None:
        icon_path = get_icon_path()
        if icon_path:
            icon = QIcon(str(icon_path))
        else:
            icon = QIcon.fromTheme("nativmix", QIcon.fromTheme("audio-volume-high"))
        super().__init__(icon, parent)

        self._window = main_window
        self._build_menu()

        self.setToolTip("NativMix – Volume Mixer")
        self.activated.connect(self._on_activated)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = QMenu()

        show_action = menu.addAction("Show / Hide")
        show_action.triggered.connect(self._toggle_window)

        menu.addSeparator()

        settings_action = menu.addAction("Settings …")
        settings_action.setEnabled(False)   # placeholder until Settings dialog exists
        settings_action.triggered.connect(self._open_settings)

        menu.addSeparator()

        quit_action = menu.addAction("Quit NativMix")
        quit_action.triggered.connect(QApplication.quit)

        self.setContextMenu(menu)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Toggle window visibility on left single-click or double-click."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window()

    def _toggle_window(self) -> None:
        if self._window.isVisible():
            self._window.hide()
        else:
            self._window.showNormal()
            self._window.activateWindow()
            self._window.raise_()

    def _open_settings(self) -> None:
        """Placeholder: open the Settings dialog (Phase 6)."""
        logger.info("Settings dialog not yet implemented")
