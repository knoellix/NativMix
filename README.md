# NativMix

NativMix is a hardware-based volume mixer for Linux, built with PyQt6. It connects physical Arduino potentiometers via USB to PipeWire/PulseAudio, giving you per-app volume control through real faders. Each channel can be mapped to one or more apps, a hardware device, or the system master. **Virtual Sinks** isolate apps in a dedicated PipeWire null-sink so seek-related volume spikes never reach your ears — the hardware fader controls the sink, the app stays at unity gain inside. New streams are caught and muted instantly (Two-Stage Mute-Catch) before metadata is available, then released at the correct fader level. MIDI CC controllers are supported natively alongside the Arduino, with MIDI-Learn and a built-in virtual MIDI port. The GUI follows your system theme automatically via the XDG Desktop Portal and works on KDE, GNOME, and any XDG-compliant desktop including Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| Breeze Theme (Native) | Iridescent Theme |
|:---:|:---:|
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

![Nothing](assets/nothing.jpg)

</div>

---

## Status

> MIDI stability data is still being collected — if you use NativMix with a MIDI controller, feedback is very welcome.
>
> **Note on "Stable":** Unless otherwise noted, "Stable" means the package installs and no obvious errors appear on first use. Only **Arch Linux / CachyOS** is daily-driven and tested in production.

| OS | Status | Notes |
| :--- | :---: | :--- |
| **Arch Linux / CachyOS** | ✅ Stable | AUR package, daily driver |
| **Ubuntu 25.04 / 25.10** | ✅ Stable | OBS package, tested on Pop!_OS |
| **Pop!_OS** | ✅ Stable | COSMIC desktop, GUI tested, no log errors |
| **openSUSE Tumbleweed** | ✅ Stable | OBS package, GUI tested, no log errors |
| **openSUSE Slowroll** | ❓ Untested | OBS package |
| **Fedora 42 / 43** | 🔧 In Progress | OBS package, being worked on |
| **Debian 12 / 13** | 🔧 In Progress | OBS package, untested |
| **Raspberry Pi OS** | ❓ Untested | Cannot verify — hardware not available |
| **Windows 10 / 11** | 🔧 In Progress | Early alpha — installer available, being actively worked on |

| Desktop Environment | Status | Notes |
| :--- | :---: | :--- |
| **KDE Plasma** | ✅ Stable | Wayland + X11, daily driver |
| **COSMIC** | ✅ Stable | Tested on Pop!_OS |
| **GNOME** | 🔧 In Progress | Basic functionality works, some quirks |

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)

---

## Installation

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/EN-Installation)**

**Arch Linux / CachyOS:**
```bash
paru -S nativmix
```

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

---

## Update History

**v1.0.7**
- VU meter: real-time per-channel peak level bars with peak-hold marker (experimental, confirmed stable on CachyOS)
- VU meter: Windows WASAPI support via IAudioMeterInformation (per-session peak sampling)
- VU meter: isolated peak-worker subprocess for V-Sink channels — main app stays alive if libpulse crashes
- VU meter: automatic fallback to volume-proxy mode after 3 subprocess crashes
- Fix: memory growth in PipeWireManager — stream add/remove no longer opens a new pulsectl connection; sink change events throttled to 500 ms
- Fix: WASAPI poll interval reduced from 150 ms to 250 ms; pw-dump output cached for 500 ms during V-Sink setup
- Windows: installer (PyInstaller + Inno Setup), early alpha
- Windows: WASAPI audio backend implemented (pycaw), stability being evaluated
- Windows: per-app volume control via Arduino implemented (early alpha)
- Windows: system master volume control via WASAPI (IAudioEndpointVolume)
- Windows: channel mapped to a hardware output device not supported
- Windows: Virtual MIDI Port hidden — not planned (WinMM has no virtual port support)
- Windows: Virtual Sinks not planned
- KDE X11 + GNOME X11: window position no longer jumps to center on show
- Fedora/Nobara: Virtual MIDI Port disabled — platform limitation (portmidi, no ALSA virtual ports)
- MIDI: Circuit Breaker — GUI protected against repeated MIDI backend crashes (disabled after 3 consecutive failures, manual restart available)
- MIDI: automatic recovery with cooldown on transient errors
- Config: corrupted config.json automatically backed up as config.json.bak instead of being silently overwritten
- Stability: various resource leak and error handling fixes (Windows IPC, MIDI port, null-sink timeout)
- About section shows version number

**v1.0.6**
- App pinning and channel renaming
- systemd autostart + XDG config migration
- portmidi fix for Fedora/Nobara
- Rounded corners always active
- Wayland: system shutdown no longer blocked by window

**v1.0.5**
- V-Sink restart stability fix
- Improved Wayland/COSMIC integration
- MIDI auto-recovery on device disconnect

**v1.0.4**
- PipeWire update handling, autostart fix, error handling improvements

**v1.0.3**
- openSUSE packaging
- AUR automation
- App filtering and V-Sink routing improvements

**v1.0.2**
- MIDI sync and mode switching fixes
- UI stability improvements

**v1.0.1**
- Tray icon fix
- "Other Apps" channel visibility

---

## License
GPL-3.0 – see [LICENSE](LICENSE) for details.
