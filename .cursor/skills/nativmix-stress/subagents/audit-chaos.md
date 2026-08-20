# Fokus: Resilience & Edge-Cases (Chaos-Monkey)
- **Hardware-Ausfall**: Was passiert bei einem `SerialException` in `arduino.py`? Wird der Reconnect sauber via `Circuit Breaker` gesteuert?
- **PipeWire-Drop**: Simuliere einen Verbindungsabbruch von `pulsectl`. Prüfe den exponentiellen Backoff in `manager.py`.
- **Race-Conditions**: Wenn zwei Events (z.B. Poti-Änderung + Mute-Toggle) gleichzeitig kommen: Ist das `_state_lock` (RLock) an der richtigen Stelle?
- **Timeout-Check**: Wo fehlen Timeouts bei blockierenden Aufrufen?
- **Flatpak-Sandbox**: Was passiert, wenn Portal Settings fehlen (kein `color-scheme`)? Bleibt die Fusion-Palette lesbar?
- **Flatpak-Serial**: Was passiert, wenn `/dev/ttyACM0` in der Sandbox nicht erreichbar ist — sauberer Fehler statt Hang?
- **Theme-Live-Switch**: Portal meldet Hell↔Dunkel während die GUI offen ist — werden Tooltips und Highlights konsistent neu gesetzt?
