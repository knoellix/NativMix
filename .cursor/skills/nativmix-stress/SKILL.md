---
name: nativmix-stress
description: Führt eine statische Fehlersimulation (Chaos-Monkey) für NativMix durch.
---

# NativMix Stress-Test (Chaos-Monkey)

Projekt-Skill — liegt in `.cursor/skills/nativmix-stress/` (nur für dieses Repo).

## Vorgehensweise
1. **Delegation**: Beauftrage `subagents/audit-chaos.md` mit der Analyse der aktuellen Dateien oder des `git diff`.
2. **Simulation**:
   - Denke den Code als fehlerhaftes System durch (z.B. "Was, wenn der Puffer leer ist?").
   - Suche nach fehlenden Circuit-Breaker-Logiken.
3. **Report**: Erstelle eine Liste der "Sollbruchstellen".

Beende mit: "Soll ich einen Resilienz-Fix für das kritischste Szenario vorschlagen?"
