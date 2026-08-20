# Fokus: XDG, Anti-Pattern & Dead Code
Prüfe `utils/paths.py`, `gui/*.py` und `utils/config_manager.py`.
- Rule 14: Keine `/tmp/` Pfade, strikte Nutzung von `XDG_RUNTIME_DIR`.
- Anti-Pattern: Ersetze `list-hacks` (`flag=[False]`) durch `nonlocal`.
- Suche unbenutzte Imports und redundante Wrapper-Methoden.
# Fokus: Observability (Debugging)
- **Detailliertes Logging**: Prüfe, ob kritische Zustandsübergänge (z.B. IPC-Verbindung aufgebaut, Arduino-Handshake erfolgreich) mit `logging.info()` oder `logging.debug()` geloggt werden.
- **Fehler-Kontext**: Werden Exceptions mit `logger.exception("...")` geloggt, um den Stacktrace zu erhalten, statt nur `print(e)`?
# Fokus: System-Integrität & Standards
- **Initial-Audit (Aspekt 1)**: Validiere `perform_initial_audio_audit()` in `manager.py`. Werden `pactl` und `pw-link` beim Start korrekt geprüft? Werden bestehende Loopbacks sauber erkannt oder entstehen Dubletten?
# Fokus: Packaging, AUR & PEP Standards
Prüfe: `pyproject.toml`, `setup.py` (falls vorhanden) und Dateistruktur.

## Prüfliste
1. **Modern Packaging (PEP 517/621)**:
   - Ist eine `pyproject.toml` vorhanden? (Pflicht für sauberes AUR-Build).
   - Sind `dependencies` und `build-system` korrekt deklariert?
2. **AUR/Arch-Konformität**:
   - Keine Installationen nach `/usr/local`. Alles muss relativ zum Prefix sein.
   - Werden Icons nach `/usr/share/icons/hicolor/...` aufgelöst (via `paths.py`)?
3. **RPM/Lint Sauberkeit**:
   - **Shebangs**: Haben alle ausführbaren Skripte `#!/usr/bin/env python`?
   - **Berechtigungen**: Werden Dateien mit 644 und Verzeichnisse mit 755 geplant?
   - **Imports**: Keine zirkulären Imports, die `rpm-lint` oder `pylint` triggern könnten.
4. **Flatpak-Ergänzungen** (wenn betroffen):
   - `flatpak/net.knoellix.NativMix.yml`, `python3-deps.json`, `packaging/FLATPAK.md` konsistent?
   - Single-version rule aus `nativmix-quality-release.mdc` eingehalten?
