# PlantBot – Home Assistant Integration

Custom Integration für [Home Assistant](https://www.home-assistant.io/), die PlantBot-Bewässerungsstationen anbindet.

**Aktuelle Version:** siehe [`custom_components/plantbot/manifest.json`](custom_components/plantbot/manifest.json)  
**Repository:** [fabibo89/plantbot-HA](https://github.com/fabibo89/plantbot-HA)

---

## Voraussetzungen

| Komponente | Hinweis |
|------------|---------|
| Home Assistant | mit HACS empfohlen |
| MQTT-Broker | erreichbar von HA **und** von der Station |
| PlantBot-Firmware | empfohlen **≥ 1.3.0-gamma** ([plantbot-OTA](https://github.com/fabibo89/plantbot-OTA/releases)) |
| Optional: plantbot-server | für Server-Modus (Pflanzen, Jobs, Regeln) |

Ab Firmware 1.3.x kommen Livedaten über MQTT (`sensors` / `status` / `valves`). HTTP `GET /status` wird für den Normalbetrieb **nicht** mehr gepollt.

---

## Installation

### Über HACS (empfohlen)

1. HACS → **Integrationen** → ⋮ → **Benutzerdefinierte Repositories**
2. Repository: `https://github.com/fabibo89/plantbot-HA`
3. Kategorie: **Integration**
4. PlantBot installieren und Home Assistant neu starten
5. **Einstellungen → Geräte & Dienste → Integration hinzufügen → PlantBot Bewässerung**

### Manuell

1. Ordner `custom_components/plantbot` nach `<config>/custom_components/plantbot` kopieren
2. Home Assistant neu starten
3. Integration wie oben hinzufügen

---

## Einrichtung

Beim Setup wählst du den Verbindungsmodus und den MQTT-Broker.

### Server-Modus

- URL zum **plantbot-server** (z. B. `http://192.168.1.100:3000`)
- Login (E-Mail / Passwort)
- MQTT-Broker (Host, Port, optional User/Passwort)

HA holt Metadaten (Stationen, Pflanzen, Sensor-Zuordnung, Jobs) per API und Livedaten per MQTT.

### Device-Modus

- IP der Station
- MQTT-Broker

Keine Server-API — Pflanzennamen und Job-Warteschlange fehlen; Ventile und Sensoren kommen von der Station.

---

## Features

### Entities

- **Ventile** – öffnen/schließen; Namen im Server-Modus über Pflanzen-Mapping
- **Sensoren** – Umgebung (Temp, Feuchte, Druck, Wasserstand, Wasseroberfläche), Flow, Volume, Wasser-Runtime, WiFi, Runtime, Speicher, Reset-Grund, Firmware-Version, Jobs (Server)
- **Boden-/MiFlora-Sensoren** – dynamisch aus MQTT `Sensoren` (Modbus / Bluetooth)
- **Update** – Firmware-OTA starten, Fortschritt, Release Notes
- **Buttons** – z. B. Stations-Reset

### Services

| Service | Beschreibung |
|---------|--------------|
| `plantbot.open_for_seconds` | Ventil für eine Dauer öffnen |
| `plantbot.open_for_volume` | Ventil bis zu einer Menge (ml) öffnen |

### Events (Automation per YAML)

Die neue HA-Automation-UI zeigt Custom-Device-Trigger oft nicht. Nutze deshalb **Ereignis-Trigger** in YAML (`automations.yaml` oder Automation → ⋮ → Als YAML bearbeiten).

Events kommen in **Server- und Device-Modus**, sobald die Station ein ACK mit `job_id >= 0` sendet:

| `job_id` | Herkunft | Event? |
|----------|----------|--------|
| `> 0` | Server-Gießjob | ja |
| `= 0` | HA Zeit-/Volumen-Gießen | ja |
| `< 0` | reines Ventil auf/zu | nein |

#### `plantbot_watering_finished`

Nach Gießende (`status`: `completed` / `failed` / `cancelled`).

Daten u. a.: `station_id`, `station_name`, `device_id`, `job_id`, `status`, `plant_name`, `pump_number`, `valve_number`, `amount_ml`, `duration_seconds`, optional `error` / `fertilizer`.

```yaml
- alias: PlantBot Gießen fertig
  triggers:
    - trigger: event
      event_type: plantbot_watering_finished
      event_data:
        status: completed
  actions:
    - action: notify.persistent_notification
      data:
        title: Gießen fertig
        message: >-
          {{ trigger.event.data.plant_name }}:
          {{ trigger.event.data.amount_ml }} ml
          in {{ trigger.event.data.duration_seconds }} s

- alias: PlantBot Gießen fehlgeschlagen
  triggers:
    - trigger: event
      event_type: plantbot_watering_finished
      event_data:
        status: failed
  actions:
    - action: notify.persistent_notification
      data:
        title: Gießen fehlgeschlagen
        message: >-
          {{ trigger.event.data.plant_name }}:
          {{ trigger.event.data.error | default('unbekannt') }}
```

#### `plantbot_alert`

MQTT `plantbot/{ip}/alerts` (z. B. Wasserstand).

Daten u. a.: `station_id`, `station_name`, `device_id`, `code`, `state` (`active` / `cleared`), `severity`, `label`, optional `water_level_cm` / `min_water_cm`.

```yaml
- alias: PlantBot Wasserstand niedrig
  triggers:
    - trigger: event
      event_type: plantbot_alert
      event_data:
        code: water_level_low
        state: active
  actions:
    - action: notify.persistent_notification
      data:
        title: Wasserstand niedrig
        message: >-
          {{ trigger.event.data.station_name }}:
          {{ trigger.event.data.water_level_cm }} cm
          (min {{ trigger.event.data.min_water_cm }} cm)
```

Optional in `event_data` weiter filtern, z. B. `station_name: Test` oder `device_id: …`.

Sensor **Alert-Status** zeigt aktive Alerts zusätzlich als Entity (Attribute: `alerts`, `last_alert`).

---

## Architektur (Kurz)

```
                    ┌─────────────────┐
   Server-API ─────►│  plantbot-HA    │◄──── MQTT (Livedaten)
   (Metadaten)      │  Coordinator    │      sensors / status / valves
                    └────────┬────────┘      logs / ack / update
                             │
                    Entities, Services, Events
```

- **MQTT:** Sensoren, Status (inkl. `flow`, `last_volume_ml`, `water_runtime`, `latest_version`, `update_needed`), Ventile, Gieß-Logs/ACKs, Alerts, OTA-Fortschritt
- **HTTP:** OTA starten (`/Github_update`), Reset — kein periodisches `/status`-Polling
- **Availability:** Station online, solange MQTT-Nachrichten kommen (Timeout ~120 s)
- **Snapshot:** HA sendet beim Start und alle 5 Min `plantbot/{ip}/commands/status_request`

---

## Firmware & Updates

- Passende Firmware: [plantbot-OTA Releases](https://github.com/fabibo89/plantbot-OTA/releases)
- Nach Firmware-OTA ggf. **SPIFFS** (`spiffs-data.zip` → Ordner `Updater/`) nachladen — siehe Firmware-Release-Notes
- In HA erscheint „Update verfügbar“, wenn die Station per MQTT `update_needed` / `latest_version` meldet (Firmware ≥ 1.3.0-gamma; Check beim Boot + alle 6 h Idle)

---

## Release Notes

Versionshistorie und Pre-Releases:

→ [`docs/releases/`](docs/releases/)

Aktuell z. B. [v1.2.10-beta](docs/releases/v1.2.10-beta.md) (Firmware ≥ 1.3.0-gamma).

Bei Publish füllen GitHub-Workflows die Release-Body aus diesen Dateien.

---

## Troubleshooting

| Symptom | Mögliche Ursache |
|---------|------------------|
| Alles „Nicht verfügbar“ | MQTT-Broker falsch / HA und Station nicht am gleichen Broker / Station offline |
| Sensoren „Unbekannt“, Status aber ok | Erweiterte Status-Felder fehlen → Firmware zu alt |
| Update-Entity zeigt nichts Neues | Firmware ohne `latest_version` im MQTT-Status; Integration neu laden |
| Event bleibt aus | Nur ACK mit `job_id >= 0` (Server-Job oder HA Zeit/Volumen); reines Ventil auf/zu hat kein Event |
| Falsche / tote Entities nach Update | Alte Entity-Keys — verwaiste Entities löschen, Integration neu laden |

MQTT-Check (Beispiel):

```bash
mosquitto_sub -h <broker> -t 'plantbot/+/status' -v
mosquitto_sub -h <broker> -t 'plantbot/+/sensors' -v
```

---

## Entwicklung

```
custom_components/plantbot/
  __init__.py        # Setup / Platforms
  config_flow.py     # UI-Setup (Server / Device / MQTT)
  coordinator.py     # Server-API + MQTT
  sensor.py / valve.py / update.py / button.py
  device_trigger.py  # Device-Trigger (UI oft unvollständig → Events/YAML)
  services.yaml
docs/releases/       # Release Notes pro Tag
```

Issues und PRs: [github.com/fabibo89/plantbot-HA](https://github.com/fabibo89/plantbot-HA)

---

## Lizenz / Zugehörige Projekte

- Firmware / OTA: [plantbot-OTA](https://github.com/fabibo89/plantbot-OTA)
- Backend: [plantbot-server](https://github.com/fabibo89/plantbot-server) (falls öffentlich / privat bei dir)
- Hardware-Quellen: plantbot-hardware
