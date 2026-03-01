# NativMix (Deutsch)

NativMix ist ein moderner, hardwaregestützter Lautstärkemixer für Linux, entwickelt mit PyQt6. Als zeitgemäße und leistungsstarke Alternative zu deej verbindet es physische Arduino-Potentiometer über USB direkt mit dem modernen PipeWire/PulseAudio-Stack.

![NativMix Icon](assets/icon.png)

## Hauptfunktionen

- **Hardware-Audio-Mixing**: Präzise Steuerung der Anwendungslautstärke über physische Schieberegler (Arduino).
- **Intelligente App-Auflösung**: Identifiziert zuverlässig sandboxed Electron- und Chromium-Apps (Discord, Spotify, Chrome) durch Analyse der `/proc`-Metadaten, um den echten App-Namen und das richtige Icon zu finden.
- **Zweistufiger Mute-Catch (Reflex-System)**:
  - **Stufe 1**: Schaltet neue Audio-Streams sofort stumm, um "Audio-Blasts" bei 100% Lautstärke zu verhindern.
  - **Stufe 2**: Identifiziert die App und wendet die eingestellte Regler-Lautstärke an, bevor die Stummschaltung aufgehoben wird.
- **Pro-Routing mit Virtual Sinks (V-Sinks)**: Erstellen Sie dedizierte virtuelle Ausgabegeräte für bestimmte Kanäle, um komplexes internes Audio-Routing oder die unabhängige Steuerung von Apps ohne native Lautstärkeregelung zu ermöglichen.
- **Native Wayland-Integration**:
  - Korrekte Prozessbenennung (`setproctitle`) für Systemmonitore.
  - Native Wayland-Fenster- und Icon-Zuordnung durch `.desktop`-Integration.
- **Dynamisches System-Theming**:
  - Automatische Anpassung an Dark/Light-Mode und Akzentfarben über das XDG Desktop Portal.
  - Unterstützung für KDE Color Schemes (.colors).
  - **Tranzparenz**: Optionaler Transparenz für die GUI.
- **Hardware-Modus**: Direkte Steuerung physischer Ausgabegeräte (Lautsprecher, Kopfhörer) oder Eingabegeräte (Mikrofone).
- **IPC & CLI Steuerung**: Kanäle per Kommandozeile stummschalten (`nativmix --toggle-mute <index>`), ideal für die Zuweisung zu globalen Tastenkombinationen.
- **Hot-Plug Robustheit**: Automatische Erkennung und Wiederverbindung des Arduinos bei Trennung der USB-Verbindung.
- **Intelligente Steuerungsmatrix**:
  - **Exklusivität**: Apps werden genau einem Kanal zugewiesen, um Konflikte zu vermeiden.
  - **Multi-App Gruppierung**: Mehrere Apps auf einen einzigen physischen Regler legen.
  - **Auto-Unmute**: Schaltet einen Kanal automatisch laut, wenn der physische Regler deutlich bewegt wird.
- **Panic Button (Panik-Knopf)**: Ein-Klick-Reset, um alle Apps aus den V-Sinks zu evakuieren und das Standard-Audio-Routing wiederherzustellen.
- **Kubisches Volume-Mapping**: Die physische Mitte des Reglers entspricht der natürlichen menschlichen Gehörwahrnehmung (~50% Lautstärke).

## Installation (Arch Linux / CachyOS)

### 1. Abhängigkeiten installieren
```bash
sudo pacman -S python-pyqt6 pipewire-pulse python-pyserial python-setproctitle
```

### 2. NativMix installieren
Repository klonen und Installationsskript ausführen:
```bash
git clone https://github.com/nativmix/nativmix.git
cd nativmix
./install.sh
```
Das Skript installiert NativMix nach `~/.local/share/nativmix` und erstellt einen Wrapper in `~/.local/bin/`.

### 3. Verwendung
Stellen Sie sicher, dass `~/.local/bin` in Ihrem `$PATH` enthalten ist. Starten Sie dann einfach:
```bash
nativmix
```
Stummschaltung eines Kanals per CLI umschalten (nützlich für Hotkeys):
```bash
nativmix --toggle-mute 0
```

## Hardware-Setup (Arduino)
NativMix ist kompatibel mit Standard deej-Firmware. Der Arduino sollte pipe-separierte ADC-Werte (0-1023) gefolgt von einem Zeilenumbruch senden:
`512|0|1023|256\n`

## Konfiguration
Die Einstellungen werden standardkonform in `~/.config/nativmix/config.json` gespeichert.

## Lizenz
[Hier Lizenz einfügen]
