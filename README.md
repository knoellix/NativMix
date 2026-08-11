# NativMix

NativMix is a hardware-based volume mixer for Linux, built with PyQt6. It connects physical Arduino potentiometers via USB to PipeWire/PulseAudio, giving you per-app volume control through real faders. Each channel can be mapped to one or more apps, a hardware device, or the system master. **Virtual Sinks** isolate apps in a dedicated PipeWire null-sink so seek-related volume spikes never reach your ears — the hardware fader controls the sink, the app stays at unity gain inside. New streams are caught and muted instantly (Two-Stage Mute-Catch) before metadata is available, then released at the correct fader level. MIDI CC controllers are supported natively alongside the Arduino, with MIDI-Learn and a built-in virtual MIDI port. The GUI follows your system theme automatically via the XDG Desktop Portal and works on KDE, GNOME, and any XDG-compliant desktop including Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| USB MIDI controller |
| ------------------- |
| ![NativMix USB MIDI controller](assets/mixer.jpg) |

| Breeze Theme (Native) | Iridescent Theme |
| --------------------- | ---------------- |
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

| Settings & mixer (Full UI) |
| ---------------------------- |
| ![NativMix settings and mixer view](assets/nothing.jpg) |

</div>

---

## Status

> MIDI stability data is still being collected — share feedback in [Discussions](https://github.com/knoellix/NativMix/discussions).
>
> **Note on "Stable":** Unless otherwise noted, "Stable" means the package installs and no obvious errors appear on first use. Only **Arch Linux / CachyOS** is daily-driven and tested in production.


| OS                       | Status     | Notes                                                                                       |
| ------------------------ | ---------- | ------------------------------------------------------------------------------------------- |
| **Arch Linux / CachyOS** | ✅ Stable   | AUR package, daily driver                                                                   |
| **Ubuntu 25.04 / 25.10** | ✅ Stable   | OBS package, tested on Pop!_OS                                                              |
| **Ubuntu 24.04 / 24.10** | ✅ Stable   | OBS package                                                                                 |
| **Linux Mint 22**        | ✅ Stable   | Uses Ubuntu 24.04 OBS package                                                               |
| **Pop!_OS**              | ✅ Stable   | COSMIC desktop, GUI tested, no log errors                                                   |
| **openSUSE Tumbleweed**  | ✅ Stable   | OBS package, GUI tested, no log errors                                                      |
| **openSUSE Slowroll**    | ❓ Untested | OBS package                                                                                 |
| **Fedora 42 / 43**       | ✅ Stable   | OBS package, core functions tested — uses portmidi instead of rtmidi (no virtual MIDI port) |
| **Debian 12 / 13**       | ✅ Stable   | OBS package — based on Ubuntu compatibility                                                 |
| **Raspberry Pi OS**      | ❓ Untested | OBS package — no Pi test hardware available                                                 |
| **Windows 10 / 11**      | ✅ Stable   | GitHub Release installer — not daily-driven by the maintainer (no V-Sinks, no virtual MIDI) |


> **Windows — feedback welcome!** Quick notes (works / breaks where) belong in [Discussions](https://github.com/knoellix/NativMix/discussions). Concrete bugs with repro steps please as an [Issue](https://github.com/knoellix/NativMix/issues).


| Desktop Environment | Status   | Notes                                                                                                                                                                                            |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **KDE Plasma**      | ✅ Stable | Wayland + X11, daily driver                                                                                                                                                                      |
| **COSMIC**          | ✅ Stable | Tested on Pop!_OS                                                                                                                                                                                |
| **GNOME**           | ✅ Stable | Wayland — sluggish system volume via NativMix reported and fixed in v1.0.14 ([#19](https://github.com/knoellix/NativMix/issues/19), thanks [@AdityaHebballe](https://github.com/AdityaHebballe)) |


> **Fedora — feedback welcome!** Fedora uses portmidi instead of rtmidi — the virtual MIDI port is not available there.
> Quick notes in [Discussions](https://github.com/knoellix/NativMix/discussions); bugs as an [Issue](https://github.com/knoellix/NativMix/issues).

---

## Installation

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/EN-Installation)**

**Arch Linux / CachyOS:**

```bash
paru -S nativmix
```

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://software.opensuse.org/download.html?project=home%3AknoelliX&package=nativmix)

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/knoellix/NativMix)

---

## Update History

**v1.0.16**

- Fix: Release Arduino USB serial before system suspend so xHCI is not held busy; reconnect after resume

**v1.0.15**

- Fix: V-Sink routing for native PipeWire apps (e.g. Strawberry) — `media.name`/`node.name` included in stream name fallback so streams land in the correct sink

→ [Full changelog](CHANGELOG.md)

---

## License

GPL-3.0 – see [LICENSE](LICENSE) for details.
