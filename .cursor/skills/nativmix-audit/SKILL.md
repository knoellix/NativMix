---
name: nativmix-audit
description: >
  Standard Code-Audit für NativMix. Prüft Architektur, Threads und Logik
  basierend auf den Cursor-Regeln (`.cursor/rules/`) und spezialisierten Sub-Agenten.
---

# NativMix Audit Koordinator

Projekt-Skill — liegt in `.cursor/skills/nativmix-audit/` (nur für dieses Repo).

## Vorgehensweise
1. **Initialisierung**: Scanne `lib/nativmix/` und lies die relevanten Regeln in `.cursor/rules/` (insb. `nativmix-architecture.mdc`, `nativmix-gotchas.mdc`, `nativmix-platform-guardrails.mdc`, `nativmix-flatpak.mdc`).
2. **Delegation**: Beauftrage die Standard-Spezialisten (Pfade relativ zu diesem Skill-Ordner):
   - **Semantic-Check** (`subagents/audit-semantic.md`): Abgleich der Änderungen mit den Projektregeln.
   - **Thread-Spezialist** (`subagents/audit-threads.md`): Signal-Sicherheit & Race Conditions.
   - **Logik-Spezialist** (`subagents/audit-logic.md`): Ressourcen-Leaks, Rule 11 & WASAPI.
   - **Standards-Spezialist** (`subagents/audit-standards.md`): XDG, Anti-Pattern & Redundanz.
   - **Flatpak-Spezialist** (`subagents/audit-flatpak.md`): Manifest, Sandbox, Theme-Split — **nur bei Flatpak/Packaging/Theme-Änderungen**.
3. **Zusammenfassung**: Erstelle den Report (🔴, 🟠, 🟡, ⚪).

## Report-Format
Fasse die Ergebnisse prägnant zusammen. Beende mit: "Soll ich mit den Bugs (🔴) anfangen? (Hinweis: Für eine Fehler-Simulation nutze 'nativmix-stress')"
