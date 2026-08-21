"""
Async GitHub release check for Windows and Flatpak installs.

Shows a hint dialog with a link to the release page (changelog lives there).
Does not download or install updates.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QUrl, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QCheckBox, QMessageBox, QWidget

from nativmix.metadata import __github_url__, __version__
from nativmix.utils.paths import is_flatpak, is_windows
from nativmix.utils.qt_utils import _slot_guard
from nativmix.utils.update_check import normalize_version, should_notify

if TYPE_CHECKING:
    from nativmix.utils.config_manager import ConfigManager

logger = logging.getLogger(__name__)

_API_URL = "https://api.github.com/repos/knoelliX/NativMix/releases/latest"
_USER_AGENT = f"NativMix/{__version__} (+{__github_url__})"


def update_check_supported() -> bool:
    """True on channels without a distro package manager for NativMix."""
    return is_windows() or is_flatpak()


class UpdateChecker(QObject):
    """Fetches ``releases/latest`` once and optionally shows a hint dialog."""

    def __init__(
        self,
        config: ConfigManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._parent_widget = parent
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_finished)

    def start(self) -> None:
        """Kick off a single async check (no-op when unsupported or disabled)."""
        if not update_check_supported():
            return
        if not self._config.check_for_updates:
            logger.debug("Update check disabled in settings")
            return
        request = QNetworkRequest(QUrl(_API_URL))
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader,
            _USER_AGENT,
        )
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        logger.debug("Checking GitHub for newer release")
        self._nam.get(request)

    @_slot_guard
    @pyqtSlot(QNetworkReply)
    def _on_finished(self, reply: QNetworkReply) -> None:
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                logger.debug(
                    "Update check failed: %s",
                    reply.errorString(),
                )
                return
            raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
            data = json.loads(raw)
            tag = str(data.get("tag_name") or "")
            html_url = str(data.get("html_url") or __github_url__ + "/releases/latest")
            remote = normalize_version(tag)
            if not should_notify(
                remote=remote,
                local=__version__,
                dismissed=self._config.update_dismissed_version,
                checks_enabled=self._config.check_for_updates,
            ):
                logger.debug("No update notification for remote=%s", remote or "<empty>")
                return
            self._show_dialog(remote, html_url)
        except Exception:
            logger.debug("Update check parse/UI error", exc_info=True)
        finally:
            reply.deleteLater()

    def _show_dialog(self, remote_version: str, release_url: str) -> None:
        box = QMessageBox(self._parent_widget)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Update available")
        box.setText(f"NativMix {remote_version} is available.")
        box.setInformativeText(f"You are running {__version__}. Open the GitHub release page for notes and downloads.")
        silence = QCheckBox(f"Don't remind me about {remote_version} again")
        silence.setToolTip(
            "Skip reminders for this version only. You will be notified again when a newer release appears."
        )
        box.setCheckBox(silence)
        open_btn = box.addButton(
            "Open release page",
            QMessageBox.ButtonRole.AcceptRole,
        )
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if silence.isChecked():
            self._config.update_dismissed_version = remote_version
            self._config.save()
            logger.info("Update reminder dismissed for %s", remote_version)
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(release_url))
