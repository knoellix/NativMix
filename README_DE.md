# NativMix (Deutsch)

NativMix ist ein hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Er verbindet physische Arduino-Potentiometer über USB mit PipeWire/PulseAudio und ermöglicht die Lautstärkeregelung einzelner Apps über echte Regler. Jeder Kanal lässt sich einer oder mehreren Apps, einem Gerät oder dem System-Master zuweisen. **Virtual Sinks** isolieren Apps in einem eigenen PipeWire Null-Sink — seek-bedingte Lautstärke-Spikes erreichen deine Lautsprecher nie mehr, weil der Regler den Sink steuert und die App intern auf Unity Gain läuft. Neue Streams werden sofort stumm geschaltet (Two-Stage Mute-Catch), bevor Metadaten verfügbar sind, und dann auf dem richtigen Fader-Pegel freigegeben. MIDI-CC-Controller werden nativ neben dem Arduino unterstützt, mit MIDI-Learn und einem integrierten virtuellen MIDI-Port. Die GUI passt sich automatisch ans System-Theme an (via XDG Desktop Portal) und funktioniert auf KDE, GNOME und allen XDG-konformen Desktops einschließlich Wayland.

![NativMix Icon](assets/icon.png)

<div align="center">

| Breeze Theme (Native) | Iridescent Theme |
|:---:|:---:|
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

![Nothing](assets/nothing.jpg)

</div>

---

## Status

> Daten zur MIDI-Stabilität werden noch gesammelt — Feedback von MIDI-Nutzern ist sehr willkommen.
>
> **Hinweis zu "Stabil":** Sofern nicht anders angegeben bedeutet "Stabil", dass das Paket installiert und beim ersten Start keine offensichtlichen Fehler auftreten. Nur **Arch Linux / CachyOS** wird täglich genutzt und produktiv getestet.

| Betriebssystem | Status | Hinweis |
| :--- | :---: | :--- |
| **Arch Linux / CachyOS** | ✅ Stabil | AUR-Paket, täglich genutzt |
| **Ubuntu 25.04 / 25.10** | ✅ Stabil | OBS-Paket, getestet auf Pop!_OS |
| **Pop!_OS** | ✅ Stabil | COSMIC Desktop, GUI getestet, keine Log-Fehler |
| **openSUSE Tumbleweed** | ✅ Stabil | OBS-Paket, GUI getestet, keine Log-Fehler |
| **openSUSE Slowroll** | ❓ Ungetestet | OBS-Paket |
| **Fedora 42 / 43** | 🔧 In Arbeit | OBS-Paket, wird aktuell bearbeitet |
| **Debian 12 / 13** | 🔧 In Arbeit | OBS-Paket, nicht getestet |
| **Raspberry Pi OS** | ❓ Ungetestet | Kann nicht verifiziert werden — Hardware nicht verfügbar |
| **Windows 10 / 11** | 🔧 In Arbeit | Frühe Alpha — Installer verfügbar, wird aktiv entwickelt |

| Desktop-Umgebung | Status | Hinweis |
| :--- | :---: | :--- |
| **KDE Plasma** | ✅ Stabil | Wayland + X11, täglich genutzt |
| **COSMIC** | ✅ Stabil | Getestet auf Pop!_OS |
| **GNOME** | 🔧 In Arbeit | Grundfunktionen laufen, kleinere Eigenheiten |

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)

---

## Installation

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

**v1.0.7**
- VU-Meter: Echtzeit-Pegelbalken pro Kanal mit Peak-Hold-Marker (experimentell, stabil bestätigt auf CachyOS)
- VU-Meter: Windows-WASAPI-Unterstützung via IAudioMeterInformation (Echtzeit-Pegelabtastung pro Session)
- VU-Meter: isolierter Peak-Worker-Subprocess für V-Sink-Kanäle — Haupt-App bleibt stabil wenn libpulse abstürzt
- VU-Meter: automatischer Fallback auf Fader-Proxy-Modus nach 3 Subprocess-Abstürzen
- Fix: Speicherwachstum in PipeWireManager — Stream-Add/Remove öffnet keine neue pulsectl-Verbindung mehr; Sink-Change-Events auf 500 ms gedrosselt
- Fix: WASAPI-Poll-Intervall von 150 ms auf 250 ms erhöht; pw-dump-Ausgabe für 500 ms gecacht beim V-Sink-Setup
- Windows: Installer (PyInstaller + Inno Setup), frühe Alpha
- Windows: WASAPI-Audio-Backend implementiert (pycaw), Stabilität wird evaluiert
- Windows: App-Lautstärkeregelung via Arduino implementiert (frühe Alpha)
- Windows: System-Master-Lautstärke via WASAPI (IAudioEndpointVolume)
- Windows: Kanal auf Hardware-Ausgabegerät gemappt nicht unterstützt
- Windows: Virtueller MIDI-Port ausgeblendet — nicht geplant (WinMM hat keine virtuellen Ports)
- Windows: Virtual Sinks nicht geplant
- KDE X11 + GNOME X11: Fensterposition springt nicht mehr zur Mitte
- Fedora/Nobara: Virtueller MIDI-Port deaktiviert — Plattform-Einschränkung (portmidi, kein ALSA Virtual Port)
- MIDI: Circuit Breaker — GUI wird vor wiederholten MIDI-Backend-Abstürzen geschützt (nach 3 aufeinanderfolgenden Fehlern deaktiviert, manueller Neustart möglich)
- MIDI: automatische Wiederherstellung mit Cooldown bei kurzzeitigen Fehlern
- Konfiguration: korrupte config.json wird automatisch als config.json.bak gesichert statt still überschrieben
- Stabilität: diverse Fixes für Resource Leaks und Fehlerbehandlung (Windows IPC, MIDI Port, Null-Sink Timeout)
- About-Bereich zeigt Versionsnummer

**v1.0.6**
- App-Pinning und Kanal-Umbenennung
- systemd-Autostart + XDG-Konfigurationsmigration
- portmidi-Fix für Fedora/Nobara
- Abgerundete Ecken immer aktiv
- Wayland: Systemherunterfahren wird nicht mehr vom Fenster blockiert

**v1.0.5**
- V-Sink-Neustart-Stabilitätsfix
- Verbesserte Wayland/COSMIC-Integration
- MIDI-Wiederherstellung bei Gerätetrennung

**v1.0.4**
- PipeWire-Update-Behandlung, Autostart-Fix, verbesserte Fehlerbehandlung

**v1.0.3**
- openSUSE-Paketierung
- AUR-Automatisierung
- App-Filterung und V-Sink-Routing-Verbesserungen

**v1.0.2**
- MIDI-Sync- und Moduswechsel-Fixes
- UI-Stabilitätsverbesserungen

**v1.0.1**
- Tray-Icon-Fix
- "Andere Apps"-Kanal-Sichtbarkeit

---

## Lizenz
GPL-3.0 – siehe [LICENSE](LICENSE) für Details.
