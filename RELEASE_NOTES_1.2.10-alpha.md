# Release Notes – PlantBot Bewässerung **1.2.10-alpha**

**Datum:** 2026-08-24  
**Basis:** Änderungen seit **v1.2.9** (29. März 2026)  
**Voraussetzung:** Plantbot-Firmware **≥ 1.3.0-alpha** (MQTT `valves` + `status_request`).  
**Firmware:** [PlantBot Firmware v1.3.0-alpha](https://github.com/fabibo89/plantbot-OTA/releases/tag/v1.3.0-alpha)  
**Hinweis:** Alpha – bitte nur mit Firmware ≥ 1.3.0-alpha testen.

## Zugehörige Firmware (v1.3.0-alpha)

Diese HA-Version ist auf **Firmware ≥ 1.3.0-alpha** ausgelegt. Details und Binaries:

→ **[plantbot-OTA Release v1.3.0-alpha](https://github.com/fabibo89/plantbot-OTA/releases/tag/v1.3.0-alpha)**

Dort u. a.: Düngen/Spülen vor dem Gießen, MQTT Live-Logs mit Phase & Düngermengen, Wasserstand-Alerts, Web-Control, sowie die für HA nötigen Topics `…/valves` und `…/commands/status_request`.

**Wichtig – SPIFFS / Web-Dateien mitaktualisieren:** Ein Firmware-OTA aktualisiert **nur** die Firmware, **nicht** die Dateien im SPIFFS (Web-UI unter `/control`, `/config`, `/manager`, …). Nach dem Update die Dateien aus dem Release-Asset **`spiffs-data.zip`** (Ordner `Updater/`) auf die Station nachladen – Details stehen in den [Firmware-Release-Notes](https://github.com/fabibo89/plantbot-OTA/releases/tag/v1.3.0-alpha).

## Features

- **Live-Daten per MQTT statt HTTP-Polling:** Sensoren, Status und Ventile kommen über MQTT (`sensors` / `status` / `valves`). HA sendet beim Start und alle 5 Min `commands/status_request` für einen Snapshot.
- **Availability über MQTT-Heartbeat:** Station gilt als online, solange Nachrichten kommen (`last_mqtt_seen`, Timeout 120 s) – kein Flackern mehr durch kurze `/status`-Timeouts.
- **OTA-Update-Status via MQTT:** Firmware-Update-Fortschritt wird live über MQTT übernommen; HTTP `/update_status` bleibt Fallback.
- **Wasseroberfläche-Sensor:** Neuer optionaler Sensor `water_surface_cm` (Abstand Sensor → Oberfläche). Wasserstand behält Icon `mdi:waves`, Wasseroberfläche `mdi:arrow-expand-vertical`.
- **Release Notes in der UI:** Volle Versionshinweise + kurze `release_summary`-Vorschau (255 Zeichen).

## Fixes / Robustheit

- **„No update available“ / Flackern bei transienten Fehlern:** Stationen behalten den letzten gültigen Zustand bei kurzzeitigen Timeouts/Neustarts.
- **Offline ohne Error-Tracebacks:** Kurze Offline-Phasen werden als offline behandelt, nicht als harter Coordinator-Fehler.
- **Per-Station Availability:** Entities orientieren sich an der jeweiligen Station (`available`), nicht global an `last_update_success`.

## Verbesserungen

- **Buttons refaktoriert (`PlantBotHttpButton`):** Generische HTTP-GET-Buttons; Reset bleibt, Struktur für weitere Station-Aktionen vorbereitet.
- **Update-Dialog stabiler:** Status bevorzugt aus MQTT, HTTP nur noch Fallback.
- **Coordinator:** Server-Metadaten (Pflanzen, Jobs, …) weiter per API; Geräte-Live-Daten primär MQTT. Update-Intervall Metadaten 60 s.

## Breaking / Voraussetzung

- **Benötigt Plantbot-Firmware ≥ 1.3.0-alpha:** [v1.3.0-alpha auf plantbot-OTA](https://github.com/fabibo89/plantbot-OTA/releases/tag/v1.3.0-alpha). Ältere Firmware fehlt `plantbot/{ip}/valves` und `plantbot/{ip}/commands/status_request` – Live-Daten und Ventilstatus sind dann unvollständig.
- **SPIFFS-Dateien aktualisieren:** OTA ersetzt nicht die Web-UI-Dateien – `spiffs-data.zip` aus dem Firmware-Release nachladen.
- Firmware ≥ 1.3.0-alpha: `…/valves` (retained, bei Änderung) und `…/commands/status_request` (Snapshot: sensors + status + valves).
- HTTP `GET /status` wird für den Normalbetrieb nicht mehr alle 30 s gepollt (OTA/Reset weiter HTTP).

---

<!-- HA_SUMMARY: Alpha: Live-Daten per MQTT statt HTTP-Polling; Availability via Heartbeat. Benötigt Firmware ≥ 1.3.0-alpha inkl. SPIFFS-Update (spiffs-data.zip). Neu: Wasseroberfläche. OTA-Status per MQTT. -->
