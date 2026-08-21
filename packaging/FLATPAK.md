# Flatpak bootstrap

This repository includes the Flatpak manifest at
`flatpak/net.knoellix.NativMix.yml`.
The initial profile is intentionally permissive for hardware validation
(audio, MIDI, Arduino/USB) and should be tightened after basic tests pass.

## GitHub Release bundle (CI)

On each `v*` tag, [`.github/workflows/build-flatpak.yml`](../.github/workflows/build-flatpak.yml)
builds a single-file bundle and attaches it to the GitHub Release (same pattern
as the Windows installer):

`NativMix-<version>.flatpak`

Install from a downloaded release asset:

```bash
flatpak install --user ./NativMix-1.0.18.flatpak
flatpak run net.knoellix.NativMix
```

### Updates — not like pacman

| Channel | How updates work |
|---|---|
| **AUR / OBS / pacman / zypper / …** | Distro package manager pulls the new version automatically |
| **GitHub `.flatpak` bundle** | Manual: download the new release asset and `flatpak install` again (or uninstall + install). There is **no** `flatpak update` from GitHub Releases |
| **Flathub / own Flatpak remote** (later) | `flatpak update` — closest to pacman-style updates |

Until Flathub (or another OSTree remote) is set up, treat the release `.flatpak`
like the Windows `.exe`: a versioned download per tag, not a live update channel.

Windows and Flatpak builds can show an **in-app hint** when a newer GitHub release
exists (Settings → “Check for updates”). That is a reminder with a link only —
not `flatpak update`.

## Local test build

```bash
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install -y flathub org.kde.Platform//6.8 org.kde.Sdk//6.8
mkdir -p "$HOME/.cache/nativmix-flatpak/build" "$HOME/.cache/nativmix-flatpak/repo"
mkdir -p "$HOME/.cache/nativmix-flatpak/state"
flatpak-builder --user --install --force-clean \
  --state-dir="$HOME/.cache/nativmix-flatpak/state" \
  "$HOME/.cache/nativmix-flatpak/build" \
  flatpak/net.knoellix.NativMix.yml \
  --repo="$HOME/.cache/nativmix-flatpak/repo"
flatpak run net.knoellix.NativMix
```

## Troubleshooting

### `fchownat: Operation not permitted`

If your repository is on a filesystem like NTFS/exFAT (often mounted under
`/mnt/...`), `flatpak-builder` may fail during build-dir initialization because
ownership changes are not supported there.

Use build/repo directories on a native Linux filesystem (for example in
`$HOME`) as shown above.

### `The state dir (...) is not on the same filesystem as the target dir (...)`

`flatpak-builder` also creates `.flatpak-builder` state by default in the
current working directory. If the project is on `/mnt/...` but the build dir is
in `$HOME`, this mismatch fails.

Pass `--state-dir` explicitly to a path in `$HOME` (same filesystem as build/repo).

## Runtime/SDK choice

- Runtime: `org.kde.Platform//6.8`
- SDK: `org.kde.Sdk//6.8`

Rationale: native Qt runtime for PyQt6 app behavior and smoother desktop
integration. The manifest follows a fork-style split with dedicated files in
`flatpak/` (`*.desktop`, `*.metainfo.xml`, `python3-deps.json`).
Because the PyQt wheels bundle QtNetwork that expects Kerberos runtime symbols,
the manifest also bundles `krb5` to provide `libgssapi_krb5.so.2`.

## Current permission model (phase 1)

- `--socket=pulseaudio`
- `--filesystem=xdg-run/pipewire-0`
- `--device=all`
- `--share=network` (optional GitHub update hint; no auto-download)
- `--filesystem=xdg-config/autostart:create` (detect Flatpak portal autostart desktop)
- `--system-talk-name=org.freedesktop.login1`
- Wayland/X11 + DRI + IPC
- Portal talk: `org.freedesktop.portal.Desktop` (theme + Flatpak autostart)

This is a pragmatic first pass. After validation, reduce to the minimal set,
especially replacing `--device=all` with narrower USB/serial access.

## Appearance (Flatpak vs native)

Flatpak does **not** use the host desktop theme (Breeze/Kvantum). Inside the
sandbox Qt typically only provides Fusion, so NativMix applies a dedicated
light/dark palette. Light vs dark follows the XDG portal
`org.freedesktop.appearance color-scheme` at startup and on live changes.

Native installs keep the system Qt style and do not apply this palette
override unless the active style is Fusion.

## Autostart (Flatpak vs host)

- **Host / native package:** unchanged — `~/.config/autostart/` and/or systemd
  user unit from Settings.
- **Flatpak:** Background portal (`org.freedesktop.portal.Background`) via
  `lib/nativmix/utils/portal_autostart.py`. The manifest allows
  `xdg-config/autostart:create` so Settings can detect the portal-created
  desktop file on the host. If the portal is missing, Settings shows a clear
  warning instead of failing silently.

## Test checklist

1. App starts and window/tray are visible
2. PipeWire/Pulse streams are detected
3. MIDI mapping and CC input works
4. Arduino `/dev/ttyACM*` reconnect works
5. Suspend/resume still closes and restores serial
6. Theme: Flatpak shows dedicated light or dark (matches system preference)
7. Theme: tooltips are readable in both light and dark
8. Theme: native install still uses Breeze/Kvantum (no forced Fusion palette)
9. Flatpak: Settings autostart toggle creates/removes host desktop via portal

## OBS AppImage (separate from Flatpak)

Portable **AppImage** builds on OBS use a different path (Many distros / `OBS:AppImage`),
not this Flatpak manifest. Maintainer steps and `appimage.yml`:
[`OSC/APPIMAGE.md`](OSC/APPIMAGE.md).
