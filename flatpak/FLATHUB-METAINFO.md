# Flathub metainfo cheat sheet (NativMix)

Maintainer notes for filling `net.knoellix.NativMix.metainfo.xml` and the
Flathub submission. **You** write the XML / PR / push; this file only collects
the data.

Local Flathub-style test (from [Flathub submission docs](https://docs.flathub.org/docs/for-app-authors/submission)):

```bash
flatpak install -y flathub org.flatpak.Builder
flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo

# From repo root, after adapting the manifest for a git/tag source if needed:
flatpak run --command=flathub-build org.flatpak.Builder --install flatpak/net.knoellix.NativMix.yml
flatpak run net.knoellix.NativMix

flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest flatpak/net.knoellix.NativMix.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo
```

Note: the in-repo manifest currently uses `type: dir` for local builds. For
Flathub you typically switch the app module to a git source at tag `v1.1.1`
(or the release you submit). Keep the **manifest hand-written** (AI policy).

---

## Identity

| Field | Value |
|-------|--------|
| App ID | `net.knoellix.NativMix` |
| Name | NativMix |
| Project license | `GPL-3.0-or-later` |
| Metadata license | `CC0-1.0` (typical for metainfo) |
| Developer name | Christian Möllmann |
| Developer / brand | knoelliX |
| Homepage | https://github.com/knoelliX/NativMix |
| Bugtracker | https://github.com/knoelliX/NativMix/issues |
| Help / wiki | https://github.com/knoelliX/NativMix/wiki |
| VCS browser | https://github.com/knoelliX/NativMix |
| Launchable | `net.knoellix.NativMix.desktop` |
| Icon name | `net.knoellix.NativMix` |
| Categories (desktop) | `AudioVideo;Audio;Mixer;` |
| Keywords | `audio;mixer;volume;pipewire;pulseaudio;arduino;` |
| Current release | `1.1.1` (tag `v1.1.1`, date `2026-10-10`) |

Verification later: domain `knoellix.net` (or GitHub `io.github…` would be a
different ID — keep `net.knoellix.NativMix` if you verify via that domain).

---

## Summary (short, English — AppStream)

```
Hardware volume mixer for Arduino and MIDI on PipeWire
```

German optional:

```
Hardware-Lautstärkemixer für Arduino und MIDI unter PipeWire
```

---

## Description (English — paste into `<description>`)

Use several `<p>` blocks. Draft:

```xml
<description>
  <p>
    NativMix is a hardware-assisted volume mixer for Linux. Map Arduino
    potentiometers over USB serial and/or MIDI CC controllers to per-application
    volume, hardware devices, or system master — using PipeWire or PulseAudio.
  </p>
  <p>
    Virtual Sinks isolate apps in a dedicated null-sink so seek-related volume
    spikes never reach your ears; the fader controls the sink while the app stays
    at unity gain. New streams are caught safely (two-stage mute-catch), then
    released at the mapped level once metadata is known.
  </p>
  <p>
    Features include MIDI Learn, profiles, optional V-Sink routing, tray
    integration, and XDG portal theme preference (light/dark). The Flatpak build
    uses a dedicated Fusion light/dark palette inside the sandbox rather than
    the host desktop theme.
  </p>
  <p>
    Serial device access is required for Arduino hardware — that is the primary
    purpose of the application.
  </p>
</description>
```

German optional (same structure in `<description xml:lang="de">` if you want).

Sources: README + wiki `EN-Features` / `EN-Flatpak` / `EN-Hardware-Setup`.

---

## Screenshots

**Plan:** take fresh shots of the **custom Flatpak / NativMix theme** (Fusion
light + dark), then host them (raw GitHub under `assets/` or release assets).

Suggested set (4 is enough for Flathub):

| # | Caption (EN) | Suggested file |
|---|--------------|----------------|
| 1 | Mixer with mapped apps | `assets/flathub-mixer-dark.jpg` (or `.png`) |
| 2 | Mixer — light custom theme | `assets/flathub-mixer-light.jpg` |
| 3 | Settings | `assets/flathub-settings.jpg` |
| 4 | Hardware / desk setup (optional) | existing `assets/mixer.jpg` still fine |

Current README shots (replace or keep as interim):

- https://raw.githubusercontent.com/knoelliX/NativMix/v1.1.1/assets/mixer.jpg
- https://raw.githubusercontent.com/knoelliX/NativMix/v1.1.1/assets/Breeze.jpg
- https://raw.githubusercontent.com/knoelliX/NativMix/v1.1.1/assets/Iridescent_Lightly_3.jpg
- https://raw.githubusercontent.com/knoelliX/NativMix/v1.1.1/assets/nothing.jpg

AppStream sketch (update URLs after upload):

```xml
<screenshots>
  <screenshot type="default">
    <caption>Main mixer</caption>
    <image>https://raw.githubusercontent.com/knoelliX/NativMix/v1.1.1/assets/YOUR-NEW-SHOT.jpg</image>
  </screenshot>
  <!-- more -->
</screenshots>
```

Tips: prefer **16:9** or similar, readable at store scale; avoid tiny crops.
`assets/icon.png` is very small — keep shipping **SVG** (`icon.svg`); Flathub
likes a solid hicolor set (256+).

---

## Releases block

```xml
<releases>
  <release version="1.1.1" date="2026-10-10">
    <description>
      <p>A/B crossfader (base×gain, USB Targets control, MIDI Learn on bar); +App redesigned as Targets; label persistence; MIDI trailing CC flush.</p>
    </description>
    <url>https://github.com/knoelliX/NativMix/releases/tag/v1.1.1</url>
  </release>
  <release version="1.1.0" date="2026-10-04">
    <description>
      <p>Windows WASAPI stable milestone; Linux SIGTERM quit fix; GUI fader persist on tray reopen; Hyprland auto-hide arming.</p>
    </description>
    <url>https://github.com/knoelliX/NativMix/releases/tag/v1.1.0</url>
  </release>
</releases>
```

Pull longer notes from `CHANGELOG.md` if you want.

---

## Content rating / extras (typical)

```xml
<content_rating type="oars-1.1" />
<!-- or fill oars attributes if the generator asks -->

<recommends>
  <control>pointing</control>
  <control>keyboard</control>
</recommends>

<requires>
  <display_length compare="ge">360</display_length>
</requires>
```

Add `<developer id="…">` per current AppStream / Flathub metainfo guidelines
when you write the final XML.

---

## finish-args → permission text (for PR + optional metainfo)

| Permission | Why |
|------------|-----|
| `--device=all` | Opens Arduino serial TTYs (`/dev/ttyACM*`, `/dev/ttyUSB*`). `--device=usb` does **not** expose those nodes. Core feature. |
| `--socket=pulseaudio` | Volume control via Pulse/PipeWire compatibility socket |
| `--filesystem=xdg-run/pipewire-0` | Native PipeWire socket |
| `--talk-name=org.freedesktop.portal.Desktop` | Theme (`color-scheme`) + Background autostart portal |
| `--filesystem=xdg-config/autostart:create` | Detect portal-created autostart `.desktop` on host |
| `--filesystem=home/.config/nativmix:create` | Config / profiles |
| `--filesystem=home/.cache/nativmix:create` | Logs / cache |
| `--system-talk-name=org.freedesktop.login1` | Suspend/resume: release serial cleanly |
| `--share=network` | Optional GitHub update **hint** only (no forced download) |
| `--talk-name=org.kde.StatusNotifierWatcher` | Tray |
| `--talk-name=org.freedesktop.Notifications` | Notifications |
| Wayland / fallback X11 / IPC | GUI |

Host note (wiki): user usually needs `dialout` (or equivalent) for serial nodes.

**PR blurb (English):**

> NativMix exists to drive physical Arduino mixers over USB serial (and MIDI).
> `--device=all` is required so the app can open kernel TTY devices such as
> `/dev/ttyACM0`. Flatpak `--device=usb` only exposes `/dev/bus/usb` and is not
> sufficient for pyserial.

---

## Checklist before you open the Flathub PR

- [ ] New custom-theme screenshots in `assets/` + committed / tagged or on `main`
- [ ] `metainfo.xml` filled (description, screenshots, release, developer)
- [ ] Manifest source = git tag (not `type: dir`) for Flathub copy
- [ ] `flathub-build` + install + Arduino/MIDI/audio smoke test
- [ ] `flatpak-builder-lint` clean (or documented exceptions)
- [ ] Submission PR opened **by you** against `flathub/flathub` branch `new-pr`
- [ ] Manifest not AI-authored; PR text/commits written by you

---

## Related in-repo files

- Manifest: `flatpak/net.knoellix.NativMix.yml`
- Metainfo (thin today): `flatpak/net.knoellix.NativMix.metainfo.xml`
- Desktop: `flatpak/net.knoellix.NativMix.desktop`
- Build notes: `packaging/FLATPAK.md`
- Wiki: `EN-Flatpak` / `DE-Flatpak`, Features, Hardware-Setup
