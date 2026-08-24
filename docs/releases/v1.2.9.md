# Release Notes – PlantBot Bewässerung **1.2.9**

**Datum:** 2026-03-29  
**Basis:** Änderungen seit **v1.2.8** (3. September 2025)

## Integration & Einrichtung

- **Config Flow:** Deutlich erweitert für die Einrichtung und Anpassung der Station(en).
- **Plattform-Loading:** Gezieltere Prüfungen pro Plattform in `__init__.py`, damit fehlende oder noch nicht bereite Teile sauberer behandelt werden.
- **Coordinator:** Setup und Fehlerbehandlung beim Start und im Betrieb verbessert.

## Coordinator & Kommunikation

- **Coordinator:** Umfangreich ausgebaut (u. a. MQTT, Status der Stationen, Datenhaltung).
- **HTTP `/status`:** Timeout für `GET http://<IP>/status` auf **15 s** erhöht (`STATUS_REQUEST_TIMEOUT` in `coordinator.py`), damit langsame Antworten (WLAN, Last) seltener als Fehler enden.
- **Job-Queue:** Abruf der Job-Queue der Station; **MQTT-Konfiguration** im Coordinator refaktoriert.

## Sensoren

- **Sensor-Handling** und Anbindung an den Coordinator refaktoriert.
- **Sensor ↔ Pflanze:** Zuordnung und **dynamische Sensor-Namen** für klarere Darstellung in der UI.
- **Dynamische Sensoren:** Optionen **`valid_range`** und **`ignore_zero`** zur sinnvollen Filterung bzw. Interpretation von Messwerten.
- **Laufzeit-/Einheiten-Konvertierung** bei Sensoren verbessert; **Sensor-Typen** angepasst.

## Firmware-Updates

- **Update-Plattform:** Robusteres Verhalten bei Firmware-Updates und zugehöriger Gerätelogik.

## Ventile & Dienste

- **Ventil-Plattform** an die neue Coordinator-/Datenstruktur angepasst.
- **Dienste** (`services.yaml`): u. a. **„Öffne für Sekunden“** und **„Öffne für Volumen“** mit Ventil-Entity-Selektor (nur PlantBot-Ventile).
- **Übersetzungen:** Deutsche Service-Texte ergänzt bzw. angepasst.

## Button

- **Reset-Button** pro Station (Hardware-Reset über die Web-API), nach dem großen Refactor wieder als eigene Plattform integriert und an die aktuelle Datenstruktur angepasst.

## Repository / Wartung

- **`.gitignore`** erweitert; **`__pycache__`** und **`.DS_Store`** aus dem Tracking entfernt bzw. dauerhaft ignoriert.
- **`logo.png`** aus dem Integrationsordner entfernt (schlankeres Repo).

---

**Hinweis:** Zwischen **v1.2.8** und **1.2.9** gab es Vorab-Stände mit Suffixen wie `-beta`, `-gamma`, `-delta`, `-epsilon`; dieser Release fasst die **gesamte** Codeentwicklung seit **v1.2.8** zusammen. Details pro Zwischenstand: Git-Tags und Commit-Historie.
