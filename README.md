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
| **Fedora 42 / 43 / 44**  | ✅ Stable   | OBS package, core functions tested — uses portmidi instead of rtmidi (no virtual MIDI port) |
| **Debian 12 / 13**       | ✅ Stable   | OBS package — based on Ubuntu compatibility                                                 |
| **Raspberry Pi OS**      | ❓ Untested | OBS package — no Pi test hardware available                                                 |
| **Windows 10 / 11**      | ✅ Stable   | GitHub Release installer — not daily-driven by the maintainer (no V-Sinks, no virtual MIDI) |


> **Windows — feedback welcome!** Quick notes (works / breaks where) belong in [Discussions](https://github.com/knoellix/NativMix/discussions). Concrete bugs with repro steps please as an [Issue](https://github.com/knoellix/NativMix/issues).


| Desktop Environment | Status   | Notes                                                                                                                                                                                            |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **KDE Plasma**      | ✅ Stable | Wayland + X11, daily driver                                                                                                                                                                      |
| **COSMIC**          | ✅ Stable | Tested on Pop!_OS                                                                                                                                                                                |
| **GNOME**           | ✅ Stable | Wayland — sluggish system volume via NativMix reported and fixed in v1.0.14 ([#19](https://github.com/knoellix/NativMix/issues/19), thanks [@AdityaHebballe](https://github.com/AdityaHebballe)) |
| **Hyprland**        | ✅ Stable | Wayland — community-confirmed on Arch; suspend with Arduino fixed in v1.0.16 ([#27](https://github.com/knoellix/NativMix/issues/27), thanks [@clombt](https://github.com/clombt))               |


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

### Flatpak (portable fallback)

NativMix is also available as a Flatpak (`net.knoellix.NativMix`). It is fully functional with PipeWire/PulseAudio, MIDI, and Arduino/USB inside the sandbox.

Use Flatpak when:

- your distribution has no suitable native package, or
- native dependencies are hard to satisfy on your system, or
- you simply prefer installing apps as Flatpak.

If a native package exists for your distro (AUR, OBS, etc.), that remains the preferred install path. Flatpak is the portable alternative — especially useful on immutable or niche setups.

**Requirements:** Linux with PipeWire or PulseAudio; for hardware faders, host access to serial devices (e.g. `/dev/ttyACM*`) must work on your system.

Flatpak is not on Flathub yet. On each `v*` tag, CI attaches
`NativMix-<version>.flatpak` to the [GitHub Release](https://github.com/knoelliX/NativMix/releases)
(same idea as the Windows installer). Install with
`flatpak install --user ./NativMix-<version>.flatpak`.

**Updates:** a release `.flatpak` does **not** auto-update like pacman. For a new
tag, download and install the new bundle again. Pacman-like `flatpak update`
needs Flathub (or another Flatpak remote) later. Local builds: [packaging/FLATPAK.md](packaging/FLATPAK.md)
or the [Flatpak wiki page](https://github.com/knoelliX/NativMix/wiki/EN-Flatpak).

**Appearance:** The Flatpak does not use your native desktop theme — only a dedicated light/dark palette that follows your system light/dark preference (XDG portal, including live changes). Autostart in Flatpak uses the Background portal; native installs keep `~/.config/autostart/` / systemd.

```bash
flatpak run net.knoellix.NativMix
```

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/knoellix/NativMix)

---

## Update History

**v1.0.19**

- Feat: drag-and-drop reorder of channel strips so GUI order can match physical MIDI/Arduino layout (#28)
- Fix: V-Sink routing no longer double-applies fader volume (routed apps sounded quieter)

**v1.0.18**

- Flatpak: local manifest/docs, Fusion light/dark via portal, portal autostart
- Fix: restore channel mute (and late volume/routing) when mapped streams appear, including Other Apps (#29)
- Feat: MIDI channel (1–16) on Learn/Mute; optional mute/LED feedback with fader sync

**v1.0.17**

- Fix: V-Sink loopback passes explicit hardware `sink=` — stops WirePlumber retry loop that silenced/stuttered Twitch and other V-Sink apps during gaming
- Fix: Quiet expected pyserial `TypeError` when Arduino serial closes mid-read during system suspend

**v1.0.16**

- Fix: Release Arduino USB serial before system suspend so xHCI is not held busy; reconnect after resume

**v1.0.15**

- Fix: V-Sink routing for native PipeWire apps (e.g. Strawberry) — `media.name`/`node.name` included in stream name fallback so streams land in the correct sink

→ [Full changelog](CHANGELOG.md)

---

## License

GPL-3.0 – see [LICENSE](LICENSE) for details.
