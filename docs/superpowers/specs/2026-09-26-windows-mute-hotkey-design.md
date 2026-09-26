# Windows mute hotkey learn (design)

**Date:** 2026-09-26  
**Branch:** `windows-wasapi-fix`  
**Platform:** Windows only (Linux keeps OS shortcuts + `nativmix --toggle-mute`)

## Problem

USB Arduino sketches often send F13–F24 as a keyboard. Windows users want those keys to toggle a NativMix channel mute without AutoHotkey and without a new settings panel.

## Goals

- Assign one global hotkey per mixer channel to toggle mute.
- UI stays minimal: reuse the existing mute button (right-click), MIDI-learn-like flow.
- Register hotkeys with the OS while NativMix is running.
- Persist bindings across restarts / profile switches.

## Non-goals

- Linux global hotkey grab (desktop shortcuts + CLI already cover this).
- Multimedia keys (Play/Pause/Next) inside NativMix.
- Hotkeys when NativMix is not running.
- A dedicated Hotkeys settings page.

## UI

On **Windows** only, the channel mute `QToolButton`:

- **Left-click:** unchanged — `toggle_mute(ch)`.
- **Right-click:** context menu:
  - **Learn hotkey…** — enter learn mode (wait for next key; Esc cancels).
  - **Clear hotkey** — enabled when a binding exists.
- **Tooltip:**
  - Base: `Toggle mute.`
  - Hint: `Right-click to learn a Windows hotkey.`
  - If set: `Hotkey: F13` (human-readable).
- While learning: tooltip / brief status like MIDI learn (`Press a key…`); Escape cancels.

No new strip controls. MIDI mute-learn buttons stay as they are.

## Data model

Per channel (profile channels[], same place as `midi_mute_cc`):

```json
"mute_hotkey": null
```

or a compact string, e.g.:

```json
"mute_hotkey": "F13"
"mute_hotkey": "Ctrl+Shift+M"
```

Canonical form: modifiers (optional) + key name, Qt-friendly (`QKeySequence` round-trip). Empty / `null` = none.

**Uniqueness:** one hotkey → one channel. Learning a key already used by another channel clears or replaces the old binding (prefer replace + log; no modal spam).

Config/profile migration: `setdefault("mute_hotkey", None)` on load.

## Windows registration

1. Small helper (e.g. `lib/nativmix/utils/win_hotkeys.py`) wrapping `RegisterHotKey` / `UnregisterHotKey` via `ctypes`.
2. Hook `WM_HOTKEY` through a `QAbstractNativeEventFilter` on the `QApplication` (or main window `winId()` as HWND).
3. Lifecycle:
   - After main window is shown / has a valid `winId()`.
   - Rebuild on profile switch, channel count change, learn, clear.
   - Unregister all on shutdown.
4. Emit / call `backend.toggle_mute(channel_index)` on match (GUI thread).

**Learn capture:** temporary application (or widget) key event filter while learning — record `QKeySequence`, do not use `RegisterHotKey` until learn completes. Reject pure modifier-only presses.

## Wiring

| Piece | Role |
|-------|------|
| `ChannelWidget` mute button | Context menu + tooltip + learn UX |
| `ConfigManager` / profile channel | Persist `mute_hotkey` |
| `WinHotkeyManager` (new) | Register / unregister / dispatch |
| `main.py` | Create manager on Windows, connect, rebuild on profile/config |

Linux: no manager, no context menu items (or menu omitted entirely).

## Testing

- Unit: parse/format hotkey strings; uniqueness replace.
- Unit/mock: manager register map (skip real WinAPI on Linux CI).
- Manual (Windows): F13 from Arduino → mute; learn/clear; profile switch; no double-fire with left-click.

## Docs

- Short note on Windows wiki AutoHotkey page: optional — in-app mute hotkeys via right-click mute button (still recommend AHK for media keys).
- CHANGELOG under Windows test-branch bullets.

## Open decisions (fixed)

- **Platform:** Windows only.
- **Scope:** mute toggle only.
- **UI:** right-click mute button, not a settings grid.
