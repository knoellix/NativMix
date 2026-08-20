# Fokus: PyQt6 Threads & Signale
Prüfe `main.py`, `hardware/*.py` und `audio/manager.py`.
- Suche nach direkten GUI-Aufrufen aus Worker-Threads.
- Prüfe Signalketten: Fehlt bei `clicked`-Slots der `checked: bool = False` Parameter?
- Verifiziere `@pyqtSlot` Signaturen.
# Fokus: Resilienz & Isolation
- **Fehlerisolation**: Prüfe, ob Exceptions in `run()`-Methoden gefangen werden, damit ein Hardware-Fehler (z.B. Arduino abgezogen) nicht das gesamte Python-Backend abstürzen lässt.
- **Timeout-Handling**: Suche nach blockierenden Aufrufen wie `ser.read()` oder `socket.recv()`. Sind überall Timeouts gesetzt? Prüfe `QThread.wait(timeout)` – wird danach `terminate()` gerufen, wenn der Thread hängt?
# Fokus: Thread-Sicherheit & Ressourcen-Management
- **State-Locking (Aspekt 4)**: Prüfe in `manager.py`, ob Dictionaries (Sinks, App-Maps) konsequent durch `self._state_lock` (RLock) geschützt sind, besonders bei parallelen Volume- und Mute-Updates.
- **Ressourcen-Cleanup (Aspekt 6)**: Verifiziere in den `stop()`-Methoden (speziell `wasapi_manager.py`), dass Signale **vor** dem Thread-Exit getrennt werden, um Geister-Events zu vermeiden.
