# Release Notes – PlantBot Bewässerung **1.2.10**

**Datum:** 2026-04-13  
**Basis:** Änderungen seit **v1.2.9** (29. März 2026)

## Features

- **OTA-Update-Status via MQTT (Push statt Polling):** Firmware-Update-Fortschritt wird live über MQTT übernommen; das reduziert HTTP-Polling und macht den UI-Status reaktiver.
- **Release Notes “voll” + Kurz-Zusammenfassung:** Die Integration kann vollständige Release Notes anzeigen (“Versionshinweise lesen”), während `release_summary` gezielt eine kurze Vorschau liefert.

## Fixes

- **„No update available“/Flackern bei transienten Fehlern reduziert:** Stationen behalten ihren letzten gültigen Zustand bei kurzzeitigen Timeouts/Neustarts; Update-Entitäten verlieren dabei nicht spontan ihre Versionsdaten.
- **Offline-Handling ohne Error-Tracebacks:** Wenn eine Station kurz offline ist (Timeout/Reboot), wird das als „offline“ behandelt statt als harter Fehler mit Stacktrace.

## Verbesserungen

- **Per-Station Availability:** Entities orientieren sich an der Erreichbarkeit der jeweiligen Station, statt global an `last_update_success`.
- **Update-Dialog schneller & stabiler:** Update-Status wird bevorzugt aus MQTT-Daten übernommen (HTTP-Status bleibt Fallback).

---

<!-- HA_SUMMARY: OTA-Update-Status jetzt via MQTT Push (weniger Polling). Offline/Timeouts verursachen keine Error-Tracebacks mehr; Entities bleiben stabil. Release Notes: kurze 255-Zeichen Summary + volle Notes unter „Versionshinweise lesen“. -->

