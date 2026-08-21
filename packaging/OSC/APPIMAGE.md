# OBS AppImage — Bauanleitung (Maintainer)

Portable Bundle für „viele Distros“ über den **Open Build Service**, parallel zu den bestehenden RPM/DEB-Builds in diesem Ordner.

> **Nicht verwechseln mit Flatpak.** Flatpak bleibt lokal/`flatpak/` + Issue [#31](https://github.com/knoelliX/NativMix/issues/31).  
> AppImage hier = OBS-Ziel **AppImage** (Many distros), siehe [OBS AppImage docs](https://docs.appimage.org/packaging-guide/hosted-services/opensuse-build-service.html).

## Was du in der OBS-Web-UI einstellst

1. Projekt öffnen (z. B. `home:knoellix:…` / euer NativMix-Projekt).
2. **Repositories / Meta:** AppImage-Buildziel aktivieren (oft als **AppImage** / „Many distros“).
3. Typisches Meta-Fragment (Namen anpassen — `HOMEPROJ` und ein RPM-Repo, aus dem das Paket `nativmix` kommt):

```xml
<repository name="AppImage">
  <path project="HOMEPROJ" repository="openSUSE_Tumbleweed"/>
  <path project="OBS:AppImage" repository="AppImage"/>
  <arch>x86_64</arch>
</repository>
```

- `HOMEPROJ` + `openSUSE_Tumbleweed` (oder Leap): liefert das **RPM** `nativmix` als Ingredient.
- `OBS:AppImage`: AppImage-Toolchain auf OBS.
- ARM optional separat (`AppImage.arm`), siehe Upstream-Doku.

4. Paket speichern / triggern — OBS baut neu, wenn Spec/Tag oder Ingredients sich ändern.

Kein zweites „Many distros“-Geheimnis: das AppImage-Repo **ist** der portable Zielkanal.

## Dateien in diesem Ordner

| Datei | Rolle |
|---|---|
| [`nativmix.spec`](nativmix.spec) + [`_service`](_service) | Wie bisher: RPM aus Git-Tag |
| [`appimage.yml`](appimage.yml) | AppImage-Rezept: packt das RPM `nativmix` in eine `.AppImage` |
| Diese Datei | Maintainer-Schritte |

### `_service` und AppImage

Das bestehende `_service` (tar_scm / set_version / mido) bleibt für **RPM**.

Für AppImage braucht OBS zusätzlich den Service `appimage`. Zwei saubere Varianten:

**A — gleiches Paket (einfach):** In `_service` den Service ergänzen (neben den bestehenden):

```xml
<service name="appimage"/>
```

Dann baut dasselbe OBS-Paket RPM **und** AppImage (je nach aktivem Repository).

**B — eigenes Paket `nativmix-appimage`:** Nur `appimage.yml` + `_service` mit `<service name="appimage"/>`, Ingredient zeigt auf RPM `nativmix` aus dem Schwesterpaket. Weniger Vermischung, etwas mehr Pflege.

Empfehlung zum Start: **A**, solange ein Build grün ist.

## `appimage.yml` (Kurz)

- `ingredients.packages: [nativmix]` — Inhalt aus dem OBS-RPM.
- `binpatch: true` — Binary-Patch für portable Libs wo nötig.
- Script kopiert `.desktop` und Icon an die AppImage-Wurzel (Pfade wie im Spec).

Nach dem ersten erfolgreichen Build: Artefakt von OBS laden und auf **mindestens einer fremden Distro** smoke-testen (PipeWire, MIDI, Arduino).

## Checkliste nach Tag (z. B. v1.0.18)

1. `_service` `revision` zeigt auf den neuen Tag (wie bei RPM).
2. OBS: AppImage-Repo aktiv, Build grün.
3. `.AppImage` herunterladen, `chmod +x`, starten.
4. Hardware: Arduino + MIDI kurz prüfen (AppImage erbt Host-Rechte anders als Flatpak — Serial/MIDI am Host freigeben).
5. Download-Link / Release-Notes optional verlinken.

## Flatpak bleibt separat

| | OBS AppImage | Flatpak |
|---|---|---|
| Wo | dieses OBS-Projekt + `appimage.yml` | `flatpak/` + [`../FLATPAK.md`](../FLATPAK.md) |
| UI in OBS | Repo **AppImage** | eigenes Flatpak-Rezept / Flathub (später #31) |
| Preferiert | portable Binary | Sandbox / immutable |

Native OBS-RPM/DEB bleiben der **bevorzugte** Installationsweg; AppImage und Flatpak sind Fallbacks.
