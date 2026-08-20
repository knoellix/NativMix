# Fokus: Ressourcen & Audio-Logik
Prüfe `audio/*.py`, `hardware/*.py` und `utils/routing.py`.
- Suche nach Leaks: Werden `MidiIn` und `Pulse` Instanzen bei Fehlern sicher geschlossen?
- Rule 11 (Mute-Catch): Erfolgt der Reflex-Mute bei jedem `new`-Event sofort?
- WASAPI (Windows): Sind `pycaw` Aufrufe sauber gekapselt und Attribute am richtigen Objekt?
# Fokus: Validierung & State-Persistence
- **Validierung vor Aktion**: Prüft der `PipeWireManager` den Stream-Status, BEVOR er Volume-Befehle sendet? Wird vor dem Zugriff auf `psutil.Process(pid)` geprüft, ob die PID noch existiert?
- **State-Persistence**: Werden Änderungen am Hardware-Mapping oder an Offsets sofort via `ConfigManager` persistiert? Prüfe, ob beim Start der App der letzte bekannte Zustand (z.B. Mute-Status) sauber wiederhergestellt wird.
# Fokus: Resilienz & Fehler-Handling
- **Circuit Breaker (Aspekt 2)**: Prüfe im MIDI-Subsystem (`midi.py`), ob der Schutzmechanismus nach 3 Fehlern die Wiederherstellung stoppt, um die GUI zu schützen.
- **Graceful Recovery (Aspekt 5)**: Suche nach dem exponentiellen Backoff bei PipeWire-Verbindungsverlust (`manager.py:812-848`). Ist die Logik stabil gegen "Endlosschleifen"?
- **Kontinuierliche Überwachung (Aspekt 3)**: Checke den `_WasapiListenerThread` – pollt er zuverlässig alle 250ms ohne den Haupt-Thread zu blockieren?
