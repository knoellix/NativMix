# Fokus: Flatpak-Manifest, Sandbox & Erscheinungsbild
Prüfe bei Änderungen an `flatpak/`, `packaging/FLATPAK.md`, Theme-Code in `main.py` oder Portal-Integration.

## Prüfliste
1. **Manifest-Konsistenz**:
   - App-ID `net.knoellix.NativMix` in yml, desktop, metainfo identisch?
   - `python3-deps.json`: Wheels statt fehlender Build-Tools (z. B. `python-rtmidi` manylinux)?
   - Kein PyQt BaseApp ohne explizite Validierung (bekannter `fchownat`-Fail)?
2. **Build-Pfad**:
   - Dokumentation warnt vor `/mnt/...` NTFS/exFAT und empfiehlt `$HOME` für build + `--state-dir`?
3. **Sandbox-Rechte**:
   - `--device=all` nur bewusst und dokumentiert (Arduino serial)?
   - Portal-Talk für Settings/Background vorhanden?
4. **Theme-Split native vs Flatpak**:
   - Palette-Override nur bei Fusion, nicht bei nativem Breeze/Kvantum?
   - Portal `color-scheme` für Hell/Dunkel; keine KDE/GNOME-Hardcodes?
   - Wiki/README erwähnen: kein natives System-Design im Flatpak?
5. **Autostart-Split** (wenn betroffen):
   - Host-Autostart (`~/.config/autostart/`) unverändert?
   - Flatpak-Portal: `a{sv}` korrekt (`commandline` als `as`, keine doppelten `QDBusVariant`)?
