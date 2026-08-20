---
name: audit-semantic
description: Gleicht Code-Änderungen gegen die Architektur-Vorgaben der Projektregeln ab.
---

# Fokus: Intent vs. Implementation
Prüfe den `git diff` oder die aktuellen Änderungen gegen `.cursor/rules/` (Architektur, Gotchas, Platform-Guardrails, Flatpak).

## Prüfliste
1. **Regel-Konformität**: Wenn an der Audio-Logik gearbeitet wurde, wurde Rule 11 (Two-Stage Mute) beachtet?
2. **Vollständigkeit**: Wenn ein neues Hardware-Feature (z.B. neuer Poti-Typ) hinzugefügt wurde, gibt es die entsprechenden Einträge im `ConfigManager` für die Persistenz?
3. **Logische Lücken**: Passt die Änderung zum "Modular-Design"-Prinzip? (Keine direkten GUI-Aufrufe in Hardware-Klassen).
4. **Theme-Split**: Beeinflussen Flatpak/Fusion-Änderungen den nativen Theme-Pfad nicht negativ?
