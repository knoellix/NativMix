# OBS AppImage — Bauanleitung (Maintainer)

Portable Bundle für „viele Distros“ über den **Open Build Service**, parallel zu den bestehenden RPM/DEB-Builds in diesem Ordner.

> **Nicht verwechseln mit Flatpak.** Flatpak bleibt lokal/`flatpak/` + Issue [#31](https://github.com/knoelliX/NativMix/issues/31).  
> AppImage hier = OBS-Ziel **AppImage** (Many distros), siehe [OBS AppImage docs](https://docs.appimage.org/packaging-guide/hosted-services/opensuse-build-service.html).

## Status / Warnung (2026-08)

OBS-AppImage für NativMix ist **noch nicht produktionsreif**.

Der Leap-basierte `OBS:AppImage`-Stack und unser Tumbleweed-RPM kollidieren bei den Runtime-Deps:

| Symptom | Ursache |
|---|---|
| `nothing provides nativmix` / `zsync` / `openSUSE-release` | Meta nur auf `OBS:AppImage/AppImage` — ohne eigenes RPM-Repo und ohne Leap-Variante |
| Build baut nur `.rpm`, kein `.AppImage` | Projekt-Config fehlt `Type: appimage` |
| `nothing provides python3-qt6` / `python3-python-rtmidi` | Ingredient `nativmix` zieht TW-Requires; Leap liefert höchstens `python311-qt6` (via `KDE:Qt:PyQt`), Factory-Mix zerlegt Preinstalls (`aaa_base` / `/bin/date`) |

**Praktische Empfehlung:** Für portable/immutable Nutzer **Flatpak** nutzen. OBS-AppImage erst wieder anfassen, wenn entweder ein Leap-fähiges Ingredient (inkl. PyQt6-Closure) steht oder ein eigenes Bundle-Rezept ohne RPM-Requires-Expansion.

## Was in OBS stehen muss (wenn du es trotzdem versuchst)

### 1. Project Meta — Repository `AppImage`

```xml
<repository name="AppImage">
  <path project="home:knoelliX" repository="openSUSE_Tumbleweed"/>
  <path project="OBS:AppImage" repository="AppImage.leap_15.6"/>
  <arch>x86_64</arch>
</repository>
```

- Erster Path: eigenes RPM (Ingredient `nativmix`).
- Zweiter Path: **`AppImage.leap_15.6`** (nicht nur `AppImage`) — sonst fehlen `build-pkg2appimage` / `zsync` / `openSUSE-release`.

### 2. Project Config (prjconf) — Pflicht

Ohne das baut OBS im AppImage-Repo nur ein normales RPM:

```
%if "%_repository" == "AppImage"
Type: appimage
Repotype: staticlinks
Patterntype: none

Required: build-pkg2appimage

Prefer: -libsystemd0-mini
Prefer: -udev-mini
Prefer: -libudev-mini1
Prefer: -systemd-mini
%endif
```

Setzen mit: `osc meta prjconf home:knoelliX -e` (bzw. `-F`).

### 3. Paket-Dateien

| Datei | Rolle |
|---|---|
| [`nativmix.spec`](nativmix.spec) + [`_service`](_service) | RPM aus Git-Tag |
| [`appimage.yml`](appimage.yml) | AppImage-Rezept (`ingredients: [nativmix]`) |
| Diese Datei | Maintainer-Schritte |

In `_service` zusätzlich:

```xml
<service name="appimage"/>
```

## `appimage.yml` (Kurz)

- `ingredients.packages: [nativmix]` — OBS expandiert dabei auch die **Requires** des RPMs. Genau dort scheitert der Leap-Stack aktuell an `python3-qt6`.
- Script kopiert `.desktop` + Icon an die AppImage-Wurzel.

## Flatpak bleibt der portable Pfad

| | OBS AppImage | Flatpak |
|---|---|---|
| Wo | dieses OBS-Projekt + `appimage.yml` | `flatpak/` + [`../FLATPAK.md`](../FLATPAK.md) |
| Status | Meta/prjconf ok-ish, Dep-Closure blockiert | lokal baubar, Sandbox |
| Preferiert | — | portable Fallback |

Native OBS-RPM/DEB bleiben der **bevorzugte** Installationsweg.
