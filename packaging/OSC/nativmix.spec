Name:           nativmix
Version:        1.0.19
Release:        0
Summary:        Hardware-based PipeWire volume & MIDI mixer for Wayland/X11
License:        GPL-3.0-or-later
URL:            https://github.com/knoelliX/NativMix

Source0:        %{name}-%{version}.tar.gz
Source1:        mido.tar.gz.bundle

BuildArch:      noarch
BuildRequires:  hicolor-icon-theme
BuildRequires:  desktop-file-utils
BuildRequires:  fdupes

# Prevent auto-dependency scanner from generating python3dist(mido)
# for the bundled lib — mido is not in distro repos and is shipped inline.
%global __requires_exclude_from ^%{_datadir}/%{name}/.*$
%global __provides_exclude_from ^%{_datadir}/%{name}/.*$
%global __requires_exclude      python3dist(mido)

%if 0%{?fedora} || 0%{?nobara}
# Fedora/Nobara — mido.backends.portmidi uses ctypes to load libportmidi.so directly;
# no Python portmidi binding needed, only the C library package.
Requires:       python3-pyqt6
Requires:       python3-pyserial
Requires:       portmidi
Requires:       python3-setproctitle
Requires:       python3-packaging
Requires:       python3-pulsectl
Requires:       qt6-qtwayland
%endif

%if 0%{?suse_version}
Requires:       python3-qt6, python3-pyserial, python3-python-rtmidi, python3-setproctitle, python3-packaging, python3-pulsectl
Requires:       qt6-wayland
%endif

%description
Hardware-assisted volume mixer with Arduino and MIDI support.

%prep
%setup -q -n %{name}-%{version}
# Extract only mido
tar -xf %{SOURCE1}

%build
# Empty build section

%check
exit 0

%install
# 1. Create main application directory
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -r * %{buildroot}%{_datadir}/%{name}/

# 2. Bundle mido into the lib directory (Pure Python)
mkdir -p %{buildroot}%{_datadir}/%{name}/lib
if [ -d "%{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido" ]; then
    cp -r %{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido %{buildroot}%{_datadir}/%{name}/lib/
fi

# 2a. Patch bundled portmidi_init.py to use find_library instead of hardcoded
#     'libportmidi.so' — Fedora/Nobara only install the versioned .so at runtime.
_PM_INIT="%{buildroot}%{_datadir}/%{name}/lib/mido/backends/portmidi_init.py"
if [ -f "$_PM_INIT" ]; then
    sed -i "s|dll_name = 'libportmidi.so'|import ctypes.util as _cu; dll_name = _cu.find_library('portmidi') or 'libportmidi.so'|" "$_PM_INIT"
fi

# 3. Clean up build artifacts (Fedora Byte-Compiler fix)
rm -rf %{buildroot}%{_datadir}/%{name}/mido-1.3.2
rm -rf %{buildroot}%{_datadir}/%{name}/packaging
rm -rf %{buildroot}%{_datadir}/%{name}/pkg
rm -rf %{buildroot}%{_datadir}/%{name}/src
# Remove files that are installed to correct system paths to avoid RPMLint duplicate-file warnings:
# LICENSE → %%{_datadir}/licenses/, README.md → %%doc, data/ → applications/ + autostart/
rm -f  %{buildroot}%{_datadir}/%{name}/LICENSE
rm -f  %{buildroot}%{_datadir}/%{name}/README.md
rm -rf %{buildroot}%{_datadir}/%{name}/data

# 4. Desktop, Icons, Udev & Service
mkdir -p %{buildroot}%{_datadir}/applications
install -m 0644 data/nativmix.desktop %{buildroot}%{_datadir}/applications/

mkdir -p %{buildroot}%{_sysconfdir}/xdg/autostart
install -m 0644 data/nativmix.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/

mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -m 0644 assets/icon.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/nativmix.svg
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/256x256/apps
install -m 0644 assets/icon.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/%{name}.png

mkdir -p %{buildroot}%{_udevrulesdir}
install -m 0644 data/udev/99-nativmix-arduino.rules %{buildroot}%{_udevrulesdir}/

mkdir -p %{buildroot}%{_userunitdir}
if [ -f "packaging/app-nativmix.service" ]; then
    install -m 0644 packaging/app-nativmix.service %{buildroot}%{_userunitdir}/
fi

# 5. Binary Wrapper
mkdir -p %{buildroot}%{_bindir}
cat <<EOF > %{buildroot}%{_bindir}/%{name}
#!/bin/bash
export PYTHONPATH="%{_datadir}/%{name}:%{_datadir}/%{name}/lib:\${PYTHONPATH}"
exec python3 -m nativmix.main "\$@"
EOF
chmod 755 %{buildroot}%{_bindir}/%{name}
find %{buildroot}%{_bindir} -type f -exec sed -i '1s|#!.*python.*|#!/usr/bin/python3|' {} +
find %{buildroot}%{_datadir}/%{name} -type f -name "*.py" -exec sed -i '1s|#!.*python.*|#!/usr/bin/python3|' {} +
# Make Python scripts with shebangs executable (RPMLint requires 755 for shebang files)
find %{buildroot}%{_datadir}/%{name} -type f -name "*.py" -exec sh -c 'head -1 "$1" | grep -q "^#!" && chmod 755 "$1"' _ {} \;

# 6. Deduplicate files (icons copied via cp -r * are also installed to system icon paths)
%fdupes %{buildroot}%{_datadir}

# 7. Post Install
%post
if [ $1 -eq 1 ] || [ $1 -eq 2 ]; then
    /usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
    /usr/bin/udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || :
fi
/usr/bin/update-desktop-database -q %{_datadir}/applications || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :

%postun
if [ $1 -eq 0 ]; then
    /usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
fi
/usr/bin/update-desktop-database -q %{_datadir}/applications || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :

%files
%defattr(-,root,root)
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/nativmix.desktop
%config %{_sysconfdir}/xdg/autostart/nativmix.desktop
%{_datadir}/icons/hicolor/scalable/apps/nativmix.svg
%{_datadir}/icons/hicolor/256x256/apps/%{name}.png
%{_userunitdir}/app-nativmix.service
%{_udevrulesdir}/99-nativmix-arduino.rules
%license LICENSE
%doc README.md

%changelog
* Thu Aug 27 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.19-1
- Feat: drag-and-drop reorder of channel strips (profile channel_order, #28)
- Feat: Easy Effects coexistence — leave streams on EE sinks; volume/mute still apply
- Feat: per-app routing pause via right-click (routing_paused_apps in profile)
- Fix: V-Sink no longer double-applies fader volume on stream and null-sink
* Wed Aug 19 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.18-1
- Flatpak: stabilize theming baseline by using a controlled light/dark fallback path in sandboxed runtime
- Flatpak: improve tooltip readability and startup theme consistency under Fusion-only style availability
* Tue Aug 18 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.17-1
- Fix: V-Sink loopback passes explicit sink= to hardware output — stops WirePlumber retry loop (No input node for loopback-*) that broke V-Sink/Twitch audio during gaming
- Fix: quiet expected TypeError when Arduino serial is closed mid-readline during PrepareForSleep
* Tue Aug 11 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.16-1
- Fix: release Arduino USB serial before system suspend (logind PrepareForSleep) so xHCI is not held busy; reconnect after resume
* Sat Aug 08 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.15-1
- Fix: V-Sink routing for native PipeWire apps (e.g. Strawberry) — include media.name/node.name in stream name fallback so streams are not left as Unknown in the wrong sink
* Fri May 30 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.14-1
- Fix: skip redundant Pulse volume writes to prevent GNOME Shell FIFO accumulation (fixes #19, thanks AdityaHebballe)
- Fix: reuse persistent Pulse connection for hardware volume during Arduino/MIDI ticks
- Fix: discard transient Arduino reconnect frames with mismatched channel counts
- Fix: volume sliders show saved positions after tray close and reopen (fixes #17)
- Feat: optional MIDI fader feedback — outbound CC sync to physical controllers (opt-in via settings)
* Wed May 07 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.13-1
- Feat: Profile system — per-profile channel config in ~/.config/nativmix/profiles/; switch via dropdown, IPC or MIDI CC
- Feat: Config v6->v7 migration — channels move out of config.json into profile files
- Feat: Fader takeover — on profile load with restore_fader_positions=true, hardware input suppressed per channel until first movement
- Feat: MIDI CC profile switching — next/prev/direct per-profile CC with Learn; global next/prev in settings
- Feat: IPC --profile next/prev/name command
- Feat: Profile dropdown in top bar with rename (debounced) and add button
- Feat: Settings panel profile section with fader-position checkbox and MIDI learn
- Fix: Channel count stability counter (3 consecutive frames) prevents oscillation on USB reconnect
- Fix: _inv_flags sync in _adapt_channels prevents IndexError on channel resize
- Fix: MIDI mute-CC bindings preserved across channel count changes
* Mon Apr 21 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.12-1
- Feat: auto_search_device flag (config v5->v6) -- disable auto port scanning when a specific port is configured
- Feat: port selector is now editable -- supports manual entry and symlinked device paths (e.g. /dev/deej)
- Feat: v5->v6 migration -- users with a port set get auto_search_device=False automatically
- Fix: debounce port text input (500 ms QTimer) to avoid reconnect storm on every keystroke
- Fix: hardware_port setter no longer overwrites auto_search_device; checkbox is the sole owner of that flag
- Based on implementation by DrKartoffel1 (PR #9, fixes #8)

* Fri Apr 11 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.11-1
- Fix: --list-sinks / --list-apps IPC now correctly returns data (shutdown(SHUT_WR) race condition resolved)
- Fix: mapped apps no longer controlled by Other Apps channel (media.name added to pa_fallback)
- Fix: IPC readyRead race condition on new connection (bytesAvailable guard added)
- Fix: AUR deploy workflow permissions moved to job level (Principle of Least Privilege)
- Feat: GENERIC_PA_NAMES -- detect and label anonymous virtual streams (pid=0)
- Feat: spotify-bin and brave-bin added to binary resolver map (AUR package names)
- Feat: stream picker shows [no process -- map by name] hint for anonymous streams
- Feat: anonymous field in --list-apps output

* Fri Apr 04 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.10-1
- Refactor: replace private-method signal connections with public API (on_midi_connection_changed, open_settings, on_mapping_changed)
- Refactor: arduino.connection_changed handler extracted to named function with error guard
- Refactor: MIDI status colors extracted to module-level constant _MIDI_STATUS_COLORS
- Refactor: backend_instance lambdas replaced with named functions (removed noqa: E731)
- Refactor: trigger_panic() alias removed from MidiThread; midi_panic_triggered connects to restart_midi directly
- Fix: mute button lambda accepts checked=False to match PyQt6 clicked(bool) signal
- Fix: _on_add_midi_clicked slot decorated with @pyqtSlot(bool) to match clicked signal
- Fix: sort PyQt6 QWidgets imports in main_window.py (ruff I001)
- Fix: stale /tmp reference in paths.py docstring corrected to XDG_RUNTIME_DIR

* Thu Apr 03 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.9-1
- Feat: Compact Mode — title bar toggle collapses mixer to fader-only view
- Feat: MIDI Mute-CC — assign any MIDI button/switch to a channel mute toggle
- Feat: Edit MIDI Channel toggle — show/hide per-channel buttons without cluttering mixer
- Feat: nativmix --restart IPC command — fully restarts the running instance
- Feat: Auto-restart after package update — checks installed version every 60 s
- Feat: PipeWire reconnect + V-Sink recovery — audio audit after PipeWire restart recreates all V-Sinks
- Perf: event deduplication in PipeWire listener — 20+ redundant callbacks per app start reduced to only real volume/mute changes
- Perf: persistent PulseAudio connection for volume ops reduces RAM growth
- Perf: debounce window geometry saves (500 ms) to avoid QSettings spam
- Fix: V-Sink display name no longer shows full flags string in pavucontrol/Helvum
- Fix: SPDX license string format in pyproject.toml (setuptools deprecation)
- Fix: MIDI channel Delete button was silently blocked by a TypeError (missing bool parameter on slot)
- Fix: Edit MIDI mode now stays active after adding or deleting a MIDI channel
- Fix: Learn buttons show Cancel while waiting; Escape or re-click cancels MIDI learn
- Fix: IPC socket moved to XDG_RUNTIME_DIR (was /tmp)
- Fix: Windows — System Master volume caused AttributeError on first access
- Fix: Windows — audio thread could become a zombie after stop() timeout

* Sun Mar 22 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.8-1
- Fix: MIDI input now correctly applies volume on hardware-mode channels
- Fix: Garbage serial frames after Arduino reconnect no longer trigger spurious channel count reset

* Fri Mar 20 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.7-1
- Windows WASAPI backend (initial support via pycaw)
- Windows Electron/Chromium app resolver via psutil (same logic as Linux)
- GNOME X11: window position no longer jumps after Mutter smart placement
- MIDI: Virtual Port grayed out on Windows with tooltip (WinMM limitation)
- V-Sink checkbox hidden on Windows
- About section shows version number
- Desktop environment status table in README

* Tue Mar 18 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.6-1
- Fix portmidi backend detection: use ctypes.util.find_library instead of import portmidi
- Patch bundled mido portmidi_init.py to use versioned libportmidi.so at runtime
- Fix mido backend not set in settings panel (MIDI port list was empty)
- Add __requires_exclude_from/__provides_exclude_from to prevent OBS auto-dep scanning bundled mido
- Remove python3-devel from Fedora BuildRequires
- Fix Fedora Requires: python3-portmidi -> portmidi (C library)
- Remove legacy XDG autostart entry on startup to prevent double instance
- Log Virtual Port / rtmidi warning only once instead of every 5 seconds