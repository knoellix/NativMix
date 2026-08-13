# NativMix (Deutsch)

NativMix ist ein hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Er verbindet physische Arduino-Potentiometer über USB mit PipeWire/PulseAudio und ermöglicht die Lautstärkeregelung einzelner Apps über echte Regler. Jeder Kanal lässt sich einer oder mehreren Apps, einem Gerät oder dem System-Master zuweisen. **Virtual Sinks** isolieren Apps in einem eigenen PipeWire Null-Sink — seek-bedingte Lautstärke-Spikes erreichen deine Lautsprecher nie mehr, weil der Regler den Sink steuert und die App intern auf Unity Gain läuft. Neue Streams werden sofort stumm geschaltet (Two-Stage Mute-Catch), bevor Metadaten verfügbar sind, und dann auf dem richtigen Fader-Pegel freigegeben. MIDI-CC-Controller werden nativ neben dem Arduino unterstützt, mit MIDI-Learn und einem integrierten virtuellen MIDI-Port. Die GUI passt sich automatisch ans System-Theme an (via XDG Desktop Portal) und funktioniert auf KDE, GNOME und allen XDG-konformen Desktops einschließlich Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| USB-MIDI-Controller |
| ------------------- |
| ![NativMix USB-MIDI-Controller](assets/mixer.jpg) |

| Breeze Theme (Native) | Iridescent Theme |
| --------------------- | ---------------- |
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

| Einstellungen & Mixer (Vollansicht) |
| ----------------------------------- |
| ![NativMix Einstellungen und Mixer](assets/nothing.jpg) |

</div>

---

## Status

> Daten zur MIDI-Stabilität werden noch gesammelt — Rückmeldungen gerne in [Discussions](https://github.com/knoellix/NativMix/discussions).
>
> **Hinweis zu "Stabil":** Sofern nicht anders angegeben bedeutet "Stabil", dass das Paket installiert und beim ersten Start keine offensichtlichen Fehler auftreten. Nur **Arch Linux / CachyOS** wird täglich genutzt und produktiv getestet.


| Betriebssystem           | Status       | Hinweis                                                                                          |
| ------------------------ | ------------ | ------------------------------------------------------------------------------------------------ |
| **Arch Linux / CachyOS** | ✅ Stabil     | AUR-Paket, täglich genutzt                                                                       |
| **Ubuntu 25.04 / 25.10** | ✅ Stabil     | OBS-Paket, getestet auf Pop!_OS                                                                  |
| **Ubuntu 24.04 / 24.10** | ✅ Stabil     | OBS-Paket                                                                                        |
| **Linux Mint 22**        | ✅ Stabil     | Nutzt Ubuntu-24.04-OBS-Paket                                                                     |
| **Pop!_OS**              | ✅ Stabil     | COSMIC Desktop, GUI getestet, keine Log-Fehler                                                   |
| **openSUSE Tumbleweed**  | ✅ Stabil     | OBS-Paket, GUI getestet, keine Log-Fehler                                                        |
| **openSUSE Slowroll**    | ❓ Ungetestet | OBS-Paket                                                                                        |
| **Fedora 42 / 43**       | ✅ Stabil     | OBS-Paket, Grundfunktionen getestet — nutzt portmidi statt rtmidi (kein virtueller MIDI-Port)    |
| **Debian 12 / 13**       | ✅ Stabil     | OBS-Paket — basierend auf Ubuntu-Kompatibilität                                                  |
| **Raspberry Pi OS**      | ❓ Ungetestet | OBS-Paket — keine Pi-Test-Hardware                                                               |
| **Windows 10 / 11**      | ✅ Stabil     | GitHub-Release-Installer — vom Maintainer nicht täglich genutzt (kein V-Sink, kein Virtual MIDI) |


> **Windows — Rückmeldungen willkommen!** Kurzes Feedback (läuft / bricht wo) gerne in [Discussions](https://github.com/knoellix/NativMix/discussions). Konkrete Fehler mit Repro-Schritten bitte als [Issue](https://github.com/knoellix/NativMix/issues).


| Desktop-Umgebung | Status   | Hinweis                                                                                                                                                                                                |
| ---------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **KDE Plasma**   | ✅ Stabil | Wayland + X11, täglich genutzt                                                                                                                                                                         |
| **COSMIC**       | ✅ Stabil | Getestet auf Pop!_OS                                                                                                                                                                                   |
| **GNOME**        | ✅ Stabil | Wayland — stockende Systemlautstärke über NativMix gemeldet und in v1.0.14 behoben ([#19](https://github.com/knoellix/NativMix/issues/19), danke [@AdityaHebballe](https://github.com/AdityaHebballe)) |
| **Hyprland**     | ✅ Stabil | Wayland — Community-bestätigt unter Arch; Suspend mit Arduino in v1.0.16 behoben ([#27](https://github.com/knoellix/NativMix/issues/27), danke [@clombt](https://github.com/clombt))                   |


> **Fedora — Feedback willkommen!** Fedora nutzt portmidi statt rtmidi — der virtuelle MIDI-Port ist dort nicht verfügbar.
> Kurze Rückmeldung in [Discussions](https://github.com/knoellix/NativMix/discussions); Bugs bitte als [Issue](https://github.com/knoellix/NativMix/issues).

---

## Installation

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://software.opensuse.org/download.html?project=home%3AknoelliX&package=nativmix)

→ **[Vollständige Installationsanleitung](https://github.com/knoellix/NativMix/wiki/DE-Installation)**

**Arch Linux / CachyOS:**

```bash
paru -S nativmix
```

---

## Dokumentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

---

## Update-Verlauf

**v1.0.16**

- Fix: Arduino-USB-Serial vor System-Suspend freigeben, damit xHCI nicht blockiert; nach Resume wieder verbinden

**v1.0.15**

- Fix: V-Sink-Routing für native PipeWire-Apps (z. B. Strawberry) — `media.name`/`node.name` im Stream-Namen-Fallback, damit Streams im richtigen Sink landen

→ [Vollständiger Changelog](CHANGELOG.md)

---

## Lizenz

GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
