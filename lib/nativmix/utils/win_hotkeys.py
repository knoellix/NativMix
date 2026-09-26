"""
Windows global mute hotkeys (RegisterHotKey).

Linux CI can import parse helpers; registration is a no-op off Windows.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from PyQt6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence

if TYPE_CHECKING:
    from PyQt6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312

# Qt Key → Win32 VK for F13–F24 and common keys (extend as needed).
_QT_TO_VK: dict[int, int] = {
    int(Qt.Key.Key_F1): 0x70,
    int(Qt.Key.Key_F2): 0x71,
    int(Qt.Key.Key_F3): 0x72,
    int(Qt.Key.Key_F4): 0x73,
    int(Qt.Key.Key_F5): 0x74,
    int(Qt.Key.Key_F6): 0x75,
    int(Qt.Key.Key_F7): 0x76,
    int(Qt.Key.Key_F8): 0x77,
    int(Qt.Key.Key_F9): 0x78,
    int(Qt.Key.Key_F10): 0x79,
    int(Qt.Key.Key_F11): 0x7A,
    int(Qt.Key.Key_F12): 0x7B,
    int(Qt.Key.Key_F13): 0x7C,
    int(Qt.Key.Key_F14): 0x7D,
    int(Qt.Key.Key_F15): 0x7E,
    int(Qt.Key.Key_F16): 0x7F,
    int(Qt.Key.Key_F17): 0x80,
    int(Qt.Key.Key_F18): 0x81,
    int(Qt.Key.Key_F19): 0x82,
    int(Qt.Key.Key_F20): 0x83,
    int(Qt.Key.Key_F21): 0x84,
    int(Qt.Key.Key_F22): 0x85,
    int(Qt.Key.Key_F23): 0x86,
    int(Qt.Key.Key_F24): 0x87,
    int(Qt.Key.Key_Space): 0x20,
    int(Qt.Key.Key_Tab): 0x09,
    int(Qt.Key.Key_Backspace): 0x08,
    int(Qt.Key.Key_Return): 0x0D,
    int(Qt.Key.Key_Enter): 0x0D,
    int(Qt.Key.Key_Escape): 0x1B,
    int(Qt.Key.Key_Insert): 0x2D,
    int(Qt.Key.Key_Delete): 0x2E,
    int(Qt.Key.Key_Home): 0x24,
    int(Qt.Key.Key_End): 0x23,
    int(Qt.Key.Key_PageUp): 0x21,
    int(Qt.Key.Key_PageDown): 0x22,
    int(Qt.Key.Key_Left): 0x25,
    int(Qt.Key.Key_Up): 0x26,
    int(Qt.Key.Key_Right): 0x27,
    int(Qt.Key.Key_Down): 0x28,
    int(Qt.Key.Key_Pause): 0x13,
    int(Qt.Key.Key_Print): 0x2C,
    int(Qt.Key.Key_ScrollLock): 0x91,
    int(Qt.Key.Key_CapsLock): 0x14,
    int(Qt.Key.Key_NumLock): 0x90,
}


def normalize_hotkey(value: str | QKeySequence | None) -> str | None:
    """Return a portable hotkey string, or None if empty / modifier-only / invalid."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        seq = QKeySequence(text)
    else:
        seq = value
    if seq.isEmpty():
        return None
    key = seq[0].key() if hasattr(seq[0], "key") else int(seq[0]) & 0x01FFFFFF
    # Modifier-only
    if key in (
        Qt.Key.Key_Control,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Alt,
        Qt.Key.Key_Meta,
        Qt.Key.Key_AltGr,
        Qt.Key.Key_unknown,
    ):
        return None
    portable = seq.toString(QKeySequence.SequenceFormat.PortableText)
    return portable or None


def hotkey_to_win_mods_vk(hotkey: str) -> tuple[int, int] | None:
    """Map a portable hotkey string to Win32 (modifiers, vk)."""
    seq = QKeySequence(hotkey)
    if seq.isEmpty():
        return None
    combo = seq[0]
    key = combo.key() if hasattr(combo, "key") else int(combo) & 0x01FFFFFF
    mods_qt = combo.keyboardModifiers() if hasattr(combo, "keyboardModifiers") else Qt.KeyboardModifier(0)

    win_mods = _MOD_NOREPEAT
    if mods_qt & Qt.KeyboardModifier.ShiftModifier:
        win_mods |= _MOD_SHIFT
    if mods_qt & Qt.KeyboardModifier.ControlModifier:
        win_mods |= _MOD_CONTROL
    if mods_qt & Qt.KeyboardModifier.AltModifier:
        win_mods |= _MOD_ALT
    if mods_qt & Qt.KeyboardModifier.MetaModifier:
        win_mods |= _MOD_WIN

    vk = _QT_TO_VK.get(int(key))
    if vk is None:
        # ASCII letters / digits
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            vk = ord("A") + (int(key) - int(Qt.Key.Key_A))
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            vk = ord("0") + (int(key) - int(Qt.Key.Key_0))
        else:
            logger.debug("No Win32 VK mapping for Qt key %#x (%s)", int(key), hotkey)
            return None
    return win_mods, vk


class _WinHotkeyNativeFilter(QAbstractNativeEventFilter):
    def __init__(self, manager: MuteHotkeyManager) -> None:
        super().__init__()
        self._manager = manager

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        try:
            et = bytes(eventType) if not isinstance(eventType, bytes | bytearray) else bytes(eventType)
        except Exception:
            return False
        if et not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if int(msg.message) != _WM_HOTKEY:
                return False
            self._manager._dispatch_hotkey_id(int(msg.wParam))
        except Exception:
            logger.debug("WM_HOTKEY dispatch failed", exc_info=True)
        return False


class MuteHotkeyManager(QObject):
    """Register per-channel mute hotkeys on Windows; learn mode via event filter."""

    triggered = pyqtSignal(int)  # channel_index
    learn_finished = pyqtSignal(int, str)  # channel, portable hotkey
    learn_cancelled = pyqtSignal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._hwnd: int = 0
        self._id_to_channel: dict[int, int] = {}
        self._registered_ids: set[int] = set()
        self._learn_channel: int | None = None
        self._filter: _WinHotkeyNativeFilter | None = None
        self._app: QGuiApplication | None = None
        self._user32 = None
        if sys.platform == "win32":
            import ctypes

            self._user32 = ctypes.windll.user32

    @property
    def is_learning(self) -> bool:
        return self._learn_channel is not None

    def attach(self, app: QGuiApplication, hwnd: int) -> None:
        """Install native filter and bind to window handle."""
        self._app = app
        self._hwnd = int(hwnd)
        if sys.platform != "win32":
            return
        if self._filter is None:
            self._filter = _WinHotkeyNativeFilter(self)
            app.installNativeEventFilter(self._filter)
        logger.info("MuteHotkeyManager attached hwnd=%s", self._hwnd)

    def detach(self) -> None:
        self.cancel_learn()
        self.clear_registrations()
        if self._app is not None and self._filter is not None:
            try:
                self._app.removeNativeEventFilter(self._filter)
            except Exception:
                logger.debug("removeNativeEventFilter failed", exc_info=True)
        self._filter = None
        self._app = None
        self._hwnd = 0

    def clear_registrations(self) -> None:
        if self._user32 is None or not self._hwnd:
            self._id_to_channel.clear()
            self._registered_ids.clear()
            return
        for hotkey_id in list(self._registered_ids):
            try:
                self._user32.UnregisterHotKey(self._hwnd, hotkey_id)
            except Exception:
                logger.debug("UnregisterHotKey(%s) failed", hotkey_id, exc_info=True)
        self._registered_ids.clear()
        self._id_to_channel.clear()

    def rebuild(self, hotkey_to_channel: dict[str, int]) -> None:
        """Unregister all, then register *hotkey_to_channel* (portable → channel)."""
        self.clear_registrations()
        if sys.platform != "win32" or not self._hwnd or self._user32 is None:
            return
        for hotkey, channel in hotkey_to_channel.items():
            parsed = hotkey_to_win_mods_vk(hotkey)
            if parsed is None:
                logger.warning("Skipping unmappable mute hotkey %r (ch %d)", hotkey, channel)
                continue
            mods, vk = parsed
            hotkey_id = int(channel) + 1  # RegisterHotKey id must be nonzero
            ok = bool(self._user32.RegisterHotKey(self._hwnd, hotkey_id, mods, vk))
            if not ok:
                logger.warning(
                    "RegisterHotKey failed for %r (ch %d) — key may be in use",
                    hotkey,
                    channel,
                )
                continue
            self._registered_ids.add(hotkey_id)
            self._id_to_channel[hotkey_id] = int(channel)
            logger.debug("Registered mute hotkey %r → ch %d (id=%d)", hotkey, channel, hotkey_id)

    def start_learn(self, channel: int) -> None:
        self.cancel_learn()
        self._learn_channel = int(channel)
        if self._app is not None:
            self._app.installEventFilter(self)
        logger.debug("Mute hotkey learn started for channel %d", channel)

    def cancel_learn(self) -> None:
        ch = self._learn_channel
        if ch is None:
            return
        self._learn_channel = None
        if self._app is not None:
            self._app.removeEventFilter(self)
        self.learn_cancelled.emit(ch)
        logger.debug("Mute hotkey learn cancelled for channel %d", ch)

    def eventFilter(self, obj, event):  # noqa: N802
        if self._learn_channel is None:
            return False
        if event.type() != QEvent.Type.KeyPress:
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:
            ch = self._learn_channel
            self._finish_learn_teardown()
            if ch is not None:
                self.learn_cancelled.emit(ch)
            return True
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
            Qt.Key.Key_AltGr,
        ):
            return True
        try:
            combo = event.keyCombination()
            seq = QKeySequence(combo)
        except Exception:
            seq = QKeySequence(event.modifiers() | Qt.Key(key))
        text = normalize_hotkey(seq)
        ch = self._learn_channel
        self._finish_learn_teardown()
        if ch is None:
            return True
        if text is None:
            self.learn_cancelled.emit(ch)
            return True
        self.learn_finished.emit(ch, text)
        return True

    def _finish_learn_teardown(self) -> None:
        self._learn_channel = None
        if self._app is not None:
            self._app.removeEventFilter(self)

    def _dispatch_hotkey_id(self, hotkey_id: int) -> None:
        ch = self._id_to_channel.get(int(hotkey_id))
        if ch is None:
            return
        self.triggered.emit(ch)
