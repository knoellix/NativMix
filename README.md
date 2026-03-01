# NativMix

NativMix is a modern, hardware-based volume mixer for Linux, built with PyQt6. Designed as a contemporary and powerful alternative to deej, it connects physical Arduino potentiometers via USB directly to the modern PipeWire/PulseAudio stack.

![NativMix Icon](assets/icon.png)

## Key Features

- **Hardware Audio Mixing**: Precise control of application volumes using physical sliders (Arduino).
- **Intelligent App Resolution**: Flawlessly identifies sandboxed Electron and Chromium apps (Discord, Spotify, Chrome) by parsing `/proc` metadata to find the real application name and icon.
- **Two-Stage Mute-Catch (Reflex System)**:
  - **Stage 1**: Immediately mutes new audio streams to prevent 100% volume "blasts".
  - **Stage 2**: Identifies the app and applies your saved slider volume before unmuting.
- **Pro-Routing with Virtual Sinks (V-Sinks)**: Create dedicated virtual sinks for specific channels to enable complex internal audio routing or independent control of apps without native volume support.
- **Native Wayland Integration**:
  - Proper process naming (`setproctitle`) for system monitors.
  - Wayland-native window and icon association via `.desktop` integration.
- **Dynamic System Theming**:
  - Automatically adapts to system dark/light modes and accent colors via XDG Desktop Portal.
  - Support for custom KDE Color Schemes (.colors).
  - **Glass-Look**: Optional translucency and blur effects for the GUI.
- **Hardware Mode**: Directly control physical output devices (speakers, headphones) or input devices (microphones).
- **IPC & CLI Control**: Toggle mute or control channels via command line (`nativmix --toggle-mute <index>`), allowing easy mapping to global keyboard hotkeys.
- **Hot-Plug Support**: Robust serial communication that automatically detects and reconnects your Arduino if unplugged.
- **Smart Control Matrix**:
  - **Exclusivity**: Applications are assigned to exactly one channel to prevent conflicts.
  - **Multi-App Grouping**: Assign multiple apps to a single physical slider.
  - **Auto-Unmute**: Automatically unmutes a channel when the physical slider is moved significantly.
- **Panic Button**: One-click reset to evacuate all apps from V-Sinks and restore standard audio routing.
- **Cubic Volume Mapping**: Physical center of the slider corresponds to natural human hearing perception (~50% loudness).

## Installation (Arch Linux / CachyOS)

### 1. Install Dependencies
```bash
sudo pacman -S python-pyqt6 pipewire-pulse python-pyserial python-setproctitle
```

### 2. Install NativMix
Clone the repository and run the install script:
```bash
git clone https://github.com/nativmix/nativmix.git
cd nativmix
./install.sh
```
The script installs NativMix into `~/.local/share/nativmix` and creates a binary wrapper in `~/.local/bin/`.

### 3. Usage
Ensure `~/.local/bin` is in your `$PATH`. Then run:
```bash
nativmix
```
To toggle mute for a channel via CLI (useful for hotkeys):
```bash
nativmix --toggle-mute 0
```

## Hardware Setup (Arduino)
NativMix is compatible with standard deej-style firmware. The Arduino should send pipe-separated ADC values (0-1023) terminated by a newline:
`512|0|1023|256\n`

## Configuration
Settings are stored in `~/.config/nativmix/config.json` following XDG standards.

## License
[Insert License Here]
