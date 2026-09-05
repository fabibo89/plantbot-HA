import logging
from datetime import timedelta
import aiohttp
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN, EVENT_WATERING_FINISHED, EVENT_ALERT, ALERT_CODE_LABELS
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import asyncio
import json
import time
from aiomqtt import Client as MQTTClient
from aiomqtt.exceptions import MqttError

_LOGGER = logging.getLogger(__name__)

# Station gilt als offline, wenn länger keine MQTT-Nachricht kam
MQTT_STALE_SECONDS = 120
# Gelegentlicher Snapshot-Request (MQTT status_request)
MQTT_STATUS_REQUEST_INTERVAL = 300

# Felder, die per MQTT live aktualisiert werden und beim Server-Refresh erhalten bleiben müssen
LIVE_MQTT_FIELDS = (
    "Sensoren",
    "valves",
    "status",
    "wifi",
    "flow",
    "lastVolume",
    "water_runtime",
    "watering_percent",
    "watering_remaining_ml",
    "watering_remaining_seconds",
    "alerts",
    "alert_status",
    "last_reset_reason",
    "runtime",
    "memory_usage",
    "current_version",
    "latest_version",
    "update_needed",
    "last_mqtt_seen",
    "firmware_update",
    "watering_status",
    "last_log",
    "last_ack",
    "current_watering_volume",
    "current_watering_duration",
    "last_alert",
)

# Server-Entity-Key → MQTT-Key (PlantBot JSON)
ENTITY_KEY_TO_MQTT = {
    "temperature": "temp",
    "humidity": "hum",
    "pressure": "pres",
    "conductivity": "cond",
    "soil_moisture": "hum",
    "light": "light",
    "battery": "bat",
    "water_level": "water_level",
    "water_surface_cm": "water_surface_cm",
}


def _mqtt_entity_key(key: str | None) -> str | None:
    if not key:
        return None
    return ENTITY_KEY_TO_MQTT.get(key, key)


def _apply_watering_remaining(station: dict, water: dict | None, status: str | None) -> None:
    """Berechne Restvolumen/-zeit aus Log-Feldern; bei Job-Ende auf 0."""
    if status in ("completed", "failed", "cancelled", "idle") or not water:
        station["watering_remaining_ml"] = 0
        station["watering_remaining_seconds"] = 0
        return

    planned_ml = water.get("planned_amount_ml")
    actual_ml = water.get("actual_amount_ml")
    planned_s = water.get("planned_duration_seconds")
    actual_s = water.get("actual_duration_seconds")
    percent = water.get("percent")

    rem_ml = None
    try:
        if planned_ml is not None and actual_ml is not None:
            rem_ml = max(0.0, float(planned_ml) - float(actual_ml))
        elif planned_ml is not None and percent is not None:
            rem_ml = max(0.0, float(planned_ml) * (100.0 - float(percent)) / 100.0)
    except (TypeError, ValueError):
        rem_ml = None

    rem_s = None
    try:
        if planned_s is not None and actual_s is not None:
            rem_s = max(0.0, float(planned_s) - float(actual_s))
        elif planned_s is not None and percent is not None:
            rem_s = max(0.0, float(planned_s) * (100.0 - float(percent)) / 100.0)
        elif actual_s is not None and percent is not None and float(percent) > 0:
            rem_s = max(0.0, float(actual_s) * (100.0 - float(percent)) / float(percent))
    except (TypeError, ValueError):
        rem_s = None

    if rem_ml is not None:
        station["watering_remaining_ml"] = round(rem_ml, 1)
    if rem_s is not None:
        station["watering_remaining_seconds"] = int(round(rem_s))


def _sensoren_from_server_devices(devices: list) -> dict:
    """Baue PlantBot-Sensoren-JSON aus Server last_value (Fallback wenn MQTT leer)."""
    sensoren: dict = {
        "PlantBot": {},
        "modbusSens": {},
        "BTSensoren": {},
        "analogSensoren": [],
    }
    for device in devices or []:
        template = device.get("device_template") or {}
        plantbot_key = template.get("plantbot_key")
        if not plantbot_key:
            continue
        identifier = device.get("identifier") or str(device.get("id"))
        for entity in device.get("entities") or []:
            if entity.get("last_value") is None:
                continue
            entity_tpl = entity.get("entity_template") or {}
            raw_key = entity_tpl.get("key")
            mqtt_key = _mqtt_entity_key(raw_key)
            if not mqtt_key:
                continue
            value = entity["last_value"]
            if plantbot_key == "PlantBot":
                sensoren["PlantBot"][mqtt_key] = value
            elif plantbot_key in ("modbusSens", "BTSensoren"):
                bucket = sensoren.setdefault(plantbot_key, {})
                addr = str(identifier)
                if addr not in bucket:
                    bucket[addr] = {}
                bucket[addr][mqtt_key] = value
    return sensoren


class PlantbotHACoordinator(DataUpdateCoordinator):
    def __init__(self, hass, config_data, entry_id=None):
        self.hass = hass
        self.config_data = config_data
        self.entry_id = entry_id
        self.connection_type = config_data.get("connection_type")
        self.server_url = config_data.get("server_url")
        self.device_ip = config_data.get("device_ip")
        self.email = config_data.get("email")
        self.access_token = config_data.get("access_token")
        self.refresh_token = config_data.get("refresh_token")
        self.session = async_get_clientsession(hass)
        
        # MQTT-Einstellungen
        self.mqtt_broker = config_data.get("mqtt_broker")
        self.mqtt_port = config_data.get("mqtt_port", 1883)
        self.mqtt_username = config_data.get("mqtt_username")
        self.mqtt_password = config_data.get("mqtt_password")
        
        # MQTT-Client für Subscribe
        self._mqtt_subscribe_client = None
        self._mqtt_subscribe_task = None
        self._subscribed_topics = set()  # Track subscribed topics
        self._last_status_request = 0.0
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Server-Metadaten periodisch; Live-Daten kommen per MQTT
            update_interval=timedelta(seconds=60),
        )

        if self.mqtt_broker:
            hass.async_create_task(self._ensure_mqtt_subscribe())

    def _merge_live_station_fields(self, target: dict, source: dict | None) -> None:
        """MQTT-Livefelder aus source in target übernehmen (source gewinnt wenn gesetzt)."""
        if not source:
            return
        for field in LIVE_MQTT_FIELDS:
            if field not in source:
                continue
            value = source[field]
            if field == "Sensoren" and isinstance(value, dict):
                if value.get("PlantBot") or value.get("modbusSens") or value.get("BTSensoren"):
                    target[field] = value
            elif value is not None:
                target[field] = value

    def _merge_all_live_data(self, result: dict) -> dict:
        """Nach Server-Fetch aktuelle MQTT-Daten wieder einmergen (Race-Schutz)."""
        if not isinstance(result, dict) or not isinstance(self.data, dict):
            return result
        merged = {}
        for key, station in result.items():
            merged_station = station.copy()
            self._merge_live_station_fields(merged_station, self.data.get(key))
            merged[key] = merged_station
        # Stationen, die nur via MQTT existieren, behalten
        for key, station in self.data.items():
            if key not in merged:
                merged[key] = station.copy()
        return merged

    async def _ensure_mqtt_subscribe(self):
        if not self.mqtt_broker or self._mqtt_subscribe_task:
            return
        _LOGGER.info("Starte MQTT-Subscribe-Task (Broker: %s:%s)", self.mqtt_broker, self.mqtt_port)
        self._mqtt_subscribe_task = self.hass.async_create_task(self._start_mqtt_subscribe())

    async def _async_update_data(self):
        """Fetch data from PlantBot(s)."""
        _LOGGER.info("_async_update_data aufgerufen, connection_type: %s", self.connection_type)
        try:
            if self.connection_type == "server":
                _LOGGER.info("Rufe _fetch_from_server auf")
                result = await self._fetch_from_server()
            else:  # connection_type == "device"
                _LOGGER.info("Baue Device-Station für IP: %s", self.device_ip)
                result = self._build_device_station(self.device_ip)
            
            # Stelle sicher, dass result ein Dictionary ist
            if not isinstance(result, dict):
                _LOGGER.warning("Unerwartetes Ergebnis-Format: %s, verwende leeres Dict", type(result))
                result = {}

            # Wenn wir gerade keine Daten bekommen (z.B. kurzfristiger Timeout),
            # liefere die letzten bekannten Daten zurück, damit Entities nicht "flackern"
            # und z.B. Update-Entities nicht kurzfristig "No update available" werden.
            if not result and isinstance(self.data, dict) and self.data:
                _LOGGER.warning(
                    "Datenabruf lieferte leeres Ergebnis – verwende gecachte Daten (%d Stationen)",
                    len(self.data),
                )
                result = self.data

            self._refresh_mqtt_availability(result)

            # MQTT-Updates, die während des Server-Fetches kamen, nicht überschreiben
            result = self._merge_all_live_data(result)

            # Gelegentlich Snapshot per MQTT anfordern
            if result and self.mqtt_broker:
                now = time.time()
                if now - self._last_status_request >= MQTT_STATUS_REQUEST_INTERVAL:
                    self._last_status_request = now
                    self.hass.async_create_task(self._request_status_all(result))

            await self._ensure_mqtt_subscribe()
            
            return result
        except asyncio.CancelledError:
            _LOGGER.warning("Abbruch während Datenabruf – vermutlich durch Shutdown oder Timeout")
            # Beim Cancel (z.B. Shutdown) lieber gecachte Daten behalten.
            return self.data if isinstance(self.data, dict) else {}
        except aiohttp.ClientError as err:
            _LOGGER.error("Verbindung zu PlantBot fehlgeschlagen: %s", err)
            # Kurzzeitige Netzwerkfehler sollen Entities nicht "leeren".
            if isinstance(self.data, dict) and self.data:
                _LOGGER.warning(
                    "Verwende gecachte Daten nach ClientError (%d Stationen)",
                    len(self.data),
                )
                return self.data
            return {}
        except Exception as err:
            _LOGGER.exception("Fehler bei der Kommunikation mit PlantBot:")
            if isinstance(self.data, dict) and self.data:
                _LOGGER.warning(
                    "Verwende gecachte Daten nach Exception (%d Stationen)",
                    len(self.data),
                )
                return self.data
            return {}

    async def _ensure_token_valid(self):
        """Stelle sicher, dass der Access Token gültig ist, refreshe falls nötig."""
        if not self.access_token:
            raise UpdateFailed("Kein Access Token verfügbar")
        
        # Versuche einen Test-Request, um zu prüfen ob Token noch gültig ist
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with self.session.get(
                f"{self.server_url}/api/v1/auth/me",
                headers=headers,
                ssl=False,
                timeout=5
            ) as response:
                if response.status == 200:
                    return  # Token ist gültig
                elif response.status == 401:
                    # Token ist abgelaufen, versuche Refresh
                    _LOGGER.debug("Access Token abgelaufen, versuche Refresh")
                    await self._refresh_token()
        except Exception as e:
            _LOGGER.debug("Fehler beim Token-Check: %s", e)
            # Versuche trotzdem Refresh
            await self._refresh_token()

    async def _refresh_token(self):
        """Refresh den Access Token mit dem Refresh Token."""
        if not self.refresh_token:
            raise UpdateFailed("Kein Refresh Token verfügbar")
        
        try:
            async with self.session.post(
                f"{self.server_url}/api/v1/auth/refresh",
                json={"refresh_token": self.refresh_token},
                ssl=False,
                timeout=10
            ) as response:
                if response.status == 200:
                    token_data = await response.json()
                    self.access_token = token_data.get("access_token")
                    self.refresh_token = token_data.get("refresh_token")
                    
                    # Aktualisiere Config Entry mit neuen Tokens
                    if self.entry_id:
                        entry = self.hass.config_entries.async_get_entry(self.entry_id)
                        if entry:
                            # Update config entry data
                            new_data = entry.data.copy()
                            new_data["access_token"] = self.access_token
                            new_data["refresh_token"] = self.refresh_token
                            self.hass.config_entries.async_update_entry(entry, data=new_data)
                            # Aktualisiere auch lokale config_data
                            self.config_data = new_data
                    
                    _LOGGER.debug("Token erfolgreich aktualisiert")
                else:
                    raise UpdateFailed("Token-Refresh fehlgeschlagen")
        except Exception as e:
            _LOGGER.error("Fehler beim Token-Refresh: %s", e)
            raise UpdateFailed(f"Token-Refresh fehlgeschlagen: {e}")

    async def _fetch_from_server(self):
        """Hole Liste der Stationen vom Server und dann Daten direkt von den PlantBots."""
        _LOGGER.info("Starte _fetch_from_server für Server: %s", self.server_url)
        result = {}
        
        # Stelle sicher, dass Token gültig ist
        try:
            _LOGGER.debug("Prüfe Token-Gültigkeit")
            await self._ensure_token_valid()
            _LOGGER.debug("Token ist gültig")
        except Exception as e:
            _LOGGER.error("Fehler beim Token-Check: %s", e)
            return result  # Rückgabe leeres Dict statt Exception
        
        # Hole Liste der Stationen vom Server
        try:
            # Verwende den korrekten Endpoint mit Authentifizierung
            # Nur online Stationen abrufen, um Timeouts zu vermeiden
            endpoint = f"{self.server_url}/api/v1/stations/?online_only=true"
            #endpoint = f"{self.server_url}/api/v1/stations"
            headers = {"Authorization": f"Bearer {self.access_token}"}
            
            stations = None
            try:
                async with self.session.get(
                    endpoint,
                    headers=headers,
                    ssl=False,
                    timeout=10
                ) as response:
                    if response.status == 200:
                        stations = await response.json()
                        _LOGGER.debug("Stationen vom Server erhalten: %s", stations)
                    elif response.status == 401:
                        # Token könnte abgelaufen sein, versuche Refresh
                        _LOGGER.warning("401 Unauthorized, versuche Token-Refresh")
                        await self._refresh_token()
                        # Wiederhole Request mit neuem Token
                        headers = {"Authorization": f"Bearer {self.access_token}"}
                        async with self.session.get(
                            endpoint,
                            headers=headers,
                            ssl=False,
                            timeout=10
                        ) as retry_response:
                            if retry_response.status == 200:
                                stations = await retry_response.json()
                                _LOGGER.debug("Stationen nach Token-Refresh erhalten")
                            else:
                                _LOGGER.error("Fehler nach Token-Refresh: HTTP %s", retry_response.status)
                                return result  # Rückgabe leeres Dict
                    else:
                        _LOGGER.error("Fehler beim Abrufen der Stationen: HTTP %s", response.status)
                        return result  # Rückgabe leeres Dict
            except Exception as e:
                _LOGGER.error("Fehler beim Zugriff auf %s: %s", endpoint, e, exc_info=True)
                return result  # Rückgabe leeres Dict
            
            if not stations:
                _LOGGER.warning("Keine Stationen vom Server erhalten")
                return result  # Rückgabe leeres Dict
            
            _LOGGER.debug("Stationen vom Server erhalten: %d Stationen gefunden", len(stations))
            
            # Für jede Station: Hole Daten direkt vom PlantBot
            for station in stations:
                _LOGGER.debug("Verarbeite Station: %s (ID: %s)", station.get("name"), station.get("id"))
                ip = station.get("ip_address") or station.get("ip")
                if not ip:
                    _LOGGER.warning("Station %s hat keine IP-Adresse, überspringe", station.get("id"))
                    continue
                
                # Stelle sicher, dass station_id ein Integer ist
                station_id_raw = station.get("id", ip)
                try:
                    station_id = int(station_id_raw)
                except (ValueError, TypeError):
                    _LOGGER.warning("Station-ID '%s' kann nicht zu Integer konvertiert werden, verwende IP: %s", station_id_raw, ip)
                    station_id = ip
                
                station_name = station.get("name", f"Station {station_id}")
                
                # Hole Pflanzen für diese Station
                plant_mapping = {}
                sensor_mapping = {}  # identifier -> plant_name
                server_sensoren = {}
                jobs_count = 0  # Anzahl pending Jobs
                _LOGGER.debug("Hole Pflanzen für Station ID: %s (Typ: %s), Name: %s, IP: %s", 
                             station_id, type(station_id).__name__, station_name, ip)
                try:
                    plants_endpoint = f"{self.server_url}/api/v1/plants/station/{station_id}"
                    headers = {"Authorization": f"Bearer {self.access_token}"}
                    async with self.session.get(plants_endpoint, headers=headers, ssl=False, timeout=10) as response:
                        if response.status == 200:
                            plants = await response.json()
                            _LOGGER.debug("Pflanzen-Daten für Station %s erhalten: %d Pflanzen", station_id, len(plants))
                            # Erstelle Mapping: (pump_number, valve_number) -> plant_name
                            for plant in plants:
                                pump = plant.get("pump_number")
                                valve = plant.get("valve_number")
                                plant_name = plant.get("name", "")
                                is_active = plant.get("is_active", True)
                                _LOGGER.debug("Pflanze: %s, Pumpe: %s (Typ: %s), Ventil: %s (Typ: %s), Aktiv: %s", 
                                             plant_name, pump, type(pump).__name__, valve, type(valve).__name__, is_active)
                                # Prüfe explizit, ob beide Werte vorhanden und > 0 sind UND ob die Pflanze aktiv ist
                                if pump is not None and valve is not None and is_active:
                                    try:
                                        pump_int = int(pump)
                                        valve_int = int(valve)
                                        if pump_int > 0 and valve_int > 0:
                                            plant_mapping[(pump_int, valve_int)] = plant_name
                                            _LOGGER.debug("Pflanze '%s' zu Mapping hinzugefügt: (P:%d, V:%d)", 
                                                         plant_name, pump_int, valve_int)
                                        else:
                                            _LOGGER.debug("Pflanze '%s' übersprungen: Pumpe=%d oder Ventil=%d ist <= 0", 
                                                         plant_name, pump_int, valve_int)
                                    except (ValueError, TypeError) as e:
                                        _LOGGER.warning("Pflanze '%s' übersprungen: Konvertierungsfehler für Pumpe/Ventil: %s", 
                                                       plant_name, e)
                                else:
                                    reason = []
                                    if pump is None:
                                        reason.append("Pumpe=None")
                                    if valve is None:
                                        reason.append("Ventil=None")
                                    if not is_active:
                                        reason.append("inaktiv")
                                    _LOGGER.debug("Pflanze '%s' übersprungen: %s", plant_name, ", ".join(reason) if reason else "unbekannt")
                            _LOGGER.debug("Pflanzen-Mapping für Station %s: %d Einträge", station_id, len(plant_mapping))
                        
                        # Hole Sensor-Devices vom Server (für Sensor-zu-Pflanze-Zuordnung)
                        sensor_mapping = {}  # identifier -> plant_name
                        try:
                            sensors_endpoint = f"{self.server_url}/api/v1/sensors/devices/station/{station_id}"
                            headers = {"Authorization": f"Bearer {self.access_token}"}
                            async with self.session.get(sensors_endpoint, headers=headers, ssl=False, timeout=10) as response:
                                if response.status == 200:
                                    sensor_devices = await response.json()
                                    _LOGGER.debug("Sensor-Devices für Station %s erhalten: %d Devices", station_id, len(sensor_devices))
                                    server_sensoren = _sensoren_from_server_devices(sensor_devices)
                                    for device in sensor_devices:
                                        identifier = device.get("identifier")
                                        plant = device.get("plant")
                                        if identifier and plant:
                                            plant_name = plant.get("name", "")
                                            sensor_mapping[identifier] = plant_name
                                            _LOGGER.debug("Sensor-Device Mapping: %s -> %s", identifier, plant_name)
                                    _LOGGER.debug("Sensor-Mapping für Station %s: %d Einträge", station_id, len(sensor_mapping))
                                elif response.status == 401:
                                    # Token könnte abgelaufen sein, versuche Refresh
                                    await self._refresh_token()
                                    headers = {"Authorization": f"Bearer {self.access_token}"}
                                    async with self.session.get(sensors_endpoint, headers=headers, ssl=False, timeout=10) as retry_response:
                                        if retry_response.status == 200:
                                            sensor_devices = await retry_response.json()
                                            server_sensoren = _sensoren_from_server_devices(sensor_devices)
                                            for device in sensor_devices:
                                                identifier = device.get("identifier")
                                                plant = device.get("plant")
                                                if identifier and plant:
                                                    plant_name = plant.get("name", "")
                                                    sensor_mapping[identifier] = plant_name
                                elif response.status == 404:
                                    _LOGGER.debug("Keine Sensor-Devices für Station %s gefunden (404)", station_id)
                                else:
                                    _LOGGER.warning("Unerwarteter Status beim Abrufen der Sensor-Devices für Station %s: HTTP %s", 
                                                   station_id, response.status)
                        except Exception as e:
                            _LOGGER.warning("Fehler beim Abrufen der Sensor-Devices für Station %s: %s", station_id, e, exc_info=True)
                        
                        # Hole Jobs (Warteschlange) vom Server
                        jobs_count = 0
                        try:
                            jobs_endpoint = f"{self.server_url}/api/v1/watering/jobs?station_id={station_id}&status=pending&limit=100"
                            headers = {"Authorization": f"Bearer {self.access_token}"}
                            async with self.session.get(jobs_endpoint, headers=headers, ssl=False, timeout=10) as response:
                                if response.status == 200:
                                    jobs = await response.json()
                                    jobs_count = len(jobs)
                                    _LOGGER.debug("Jobs für Station %s erhalten: %d pending Jobs", station_id, jobs_count)
                                elif response.status == 401:
                                    # Token könnte abgelaufen sein, versuche Refresh
                                    await self._refresh_token()
                                    headers = {"Authorization": f"Bearer {self.access_token}"}
                                    async with self.session.get(jobs_endpoint, headers=headers, ssl=False, timeout=10) as retry_response:
                                        if retry_response.status == 200:
                                            jobs = await retry_response.json()
                                            jobs_count = len(jobs)
                                elif response.status == 404:
                                    _LOGGER.debug("Keine Jobs für Station %s gefunden (404)", station_id)
                                else:
                                    _LOGGER.debug("Unerwarteter Status beim Abrufen der Jobs für Station %s: HTTP %s", 
                                                 station_id, response.status)
                        except Exception as e:
                            _LOGGER.warning("Fehler beim Abrufen der Jobs für Station %s: %s", station_id, e, exc_info=True)
                        
                except Exception as e:
                    _LOGGER.warning("Fehler beim Abrufen der Pflanzen für Station %s: %s", station_id, e, exc_info=True)
                    jobs_count = 0  # Fallback
                    sensor_mapping = {}  # Fallback
                
                try:
                    result_key = f"station_{station_id}"
                    existing = {}
                    if isinstance(self.data, dict):
                        existing = (self.data.get(result_key) or {}).copy()

                    station_entry = {
                        "id": station_id,
                        "name": station_name,
                        "ip": ip,
                        "source": "server",
                        "available": existing.get("available", False),
                        "num_pumps": station.get("num_pumps", 1),
                        "num_valves": station.get("num_valves", 8),
                        "fertilizer_pump_number": station.get("fertilizer_pump_number"),
                        "plant_mapping": plant_mapping,
                        "sensor_mapping": sensor_mapping,
                        "jobs": jobs_count,
                    }
                    # Live-Daten aus MQTT behalten
                    for field in LIVE_MQTT_FIELDS:
                        if field in existing:
                            station_entry[field] = existing[field]
                    # Fallback: Sensoren vom Server (last_value), wenn MQTT noch leer
                    existing_sensoren = station_entry.get("Sensoren") or {}
                    has_live_sensoren = bool(
                        (existing_sensoren.get("PlantBot") or existing_sensoren.get("modbusSens") or existing_sensoren.get("BTSensoren"))
                    )
                    if not has_live_sensoren and server_sensoren and (
                        server_sensoren.get("PlantBot")
                        or server_sensoren.get("modbusSens")
                        or server_sensoren.get("BTSensoren")
                    ):
                        station_entry["Sensoren"] = server_sensoren
                        _LOGGER.debug(
                            "Station %s: Sensoren aus Server last_value übernommen (MQTT-Fallback)",
                            station_id,
                        )
                    result[result_key] = station_entry
                except Exception as e:
                    _LOGGER.warning("Fehler beim Aufbauen der Station %s (%s): %s", station_name, ip, e)
                    result[f"station_{station_id}"] = {
                        "id": station_id,
                        "name": station_name,
                        "ip": ip,
                        "source": "server",
                        "available": False,
                        "num_pumps": station.get("num_pumps", 1),
                        "num_valves": station.get("num_valves", 8),
                        "fertilizer_pump_number": station.get("fertilizer_pump_number"),
                        "plant_mapping": plant_mapping,
                        "sensor_mapping": sensor_mapping,
                        "jobs": jobs_count,
                    }
            
            return result
            
        except Exception as e:
            _LOGGER.error("Fehler beim Abrufen vom Server: %s", e, exc_info=True)
            return result  # Rückgabe leeres Dict statt Exception

    def _build_device_station(self, ip):
        """Station-Eintrag für Direct-Device-Modus (Live-Daten kommen per MQTT)."""
        result_key = f"station_{ip}"
        existing = {}
        if isinstance(self.data, dict):
            existing = (self.data.get(result_key) or {}).copy()
        station_entry = {
            "id": str(ip),
            "name": existing.get("name", ip),
            "ip": ip,
            "source": "device",
            "available": existing.get("available", False),
            "num_pumps": existing.get("num_pumps", 1),
            "num_valves": existing.get("num_valves", 8),
            "plant_mapping": existing.get("plant_mapping", {}),
            "sensor_mapping": existing.get("sensor_mapping", {}),
            "Sensoren": existing.get("Sensoren", {}),
            "valves": existing.get("valves", []),
        }
        for field in (
            "status",
            "wifi",
            "flow",
            "lastVolume",
            "water_runtime",
            "watering_percent",
            "last_reset_reason",
            "runtime",
            "memory_usage",
            "current_version",
            "latest_version",
            "update_needed",
            "last_mqtt_seen",
            "firmware_update",
            "watering_status",
        ):
            if field in existing:
                station_entry[field] = existing[field]
        return {result_key: station_entry}

    def _refresh_mqtt_availability(self, stations):
        """Setze available anhand des letzten MQTT-Timestamps."""
        if not isinstance(stations, dict):
            return
        now = time.time()
        for station in stations.values():
            last_seen = station.get("last_mqtt_seen")
            if last_seen:
                station["available"] = (now - float(last_seen)) <= MQTT_STALE_SECONDS
            elif "available" not in station:
                station["available"] = False

    def _touch_mqtt_seen(self, station):
        station["last_mqtt_seen"] = time.time()
        station["available"] = True

    async def _request_status_all(self, stations_data):
        """Fordere sensors+status+valves Snapshot von allen Stationen an."""
        for station_data in (stations_data or {}).values():
            ip = station_data.get("ip")
            if ip:
                await self.request_status_snapshot(ip)

    async def request_status_snapshot(self, ip):
        """Sende status_request an eine Station."""
        if not self.mqtt_broker:
            return False
        topic = f"plantbot/{ip}/commands/status_request"
        try:
            if self._mqtt_subscribe_client:
                await self._mqtt_subscribe_client.publish(topic, b"{}", qos=1)
            else:
                client_id = f"plantbot_req_{self.entry_id or 'default'}_{id(self)}"
                client_kwargs = self._get_mqtt_client_config(client_id)
                async with MQTTClient(**client_kwargs) as client:
                    await client.publish(topic, b"{}", qos=1)
            _LOGGER.info("status_request gesendet: %s", topic)
            return True
        except Exception as e:
            _LOGGER.warning("status_request fehlgeschlagen für %s: %s", ip, e)
            return False

    def _get_mqtt_client_config(self, client_id):
        """Erstelle MQTT-Client-Konfiguration (wiederverwendbar)."""
        client_kwargs = {
            "hostname": self.mqtt_broker,
            "port": self.mqtt_port,
            "identifier": client_id,
        }
        # Nur Username/Password hinzufügen, wenn sie gesetzt sind
        if self.mqtt_username:
            client_kwargs["username"] = self.mqtt_username
        if self.mqtt_password:
            client_kwargs["password"] = self.mqtt_password
        return client_kwargs

    async def send_valve_command(self, ip, pump_number, valve_id, command, **kwargs):
        """Sende Ventil-Befehl an PlantBot über MQTT."""
        if not self.mqtt_broker:
            _LOGGER.error("Kein MQTT-Broker konfiguriert")
            return False
        
        try:
            # Erstelle Payload
            payload = {
                "pump_number": int(pump_number),
                "valve_id": int(valve_id),
                "action": command,  # "open" oder "close"
            }
            
            # Füge optionale Parameter hinzu
            if "duration" in kwargs:
                payload["duration"] = int(kwargs["duration"])
            if "volume" in kwargs:
                payload["volume"] = int(kwargs["volume"])
            if kwargs.get("plant_name"):
                payload["plant_name"] = str(kwargs["plant_name"])
            
            topic = f"plantbot/{ip}/commands/valve"
            payload_json = json.dumps(payload)
            
            _LOGGER.debug("Sende MQTT-Befehl: %s -> %s", topic, payload_json)
            
            # Erstelle MQTT-Client für diesen Befehl
            client_id = f"plantbot_{self.entry_id or 'default'}_{id(self)}"
            client_kwargs = self._get_mqtt_client_config(client_id)
            
            async with MQTTClient(**client_kwargs) as client:
                await client.publish(topic, payload_json, qos=1)
                _LOGGER.info("Ventil-Befehl erfolgreich über MQTT gesendet: %s", topic)
            
            # Aktualisiere Daten nach kurzer Verzögerung
            await asyncio.sleep(0.5)
            await self.async_request_refresh()
            return True
            
        except MqttError as e:
            _LOGGER.error("MQTT-Fehler beim Senden des Ventil-Befehls: %s", e)
            return False
        except Exception as e:
            _LOGGER.error("Fehler beim Senden des Ventil-Befehls über MQTT: %s", e)
            return False

    async def _start_mqtt_subscribe(self):
        """Starte MQTT-Subscribe für logs und ack."""
        if not self.mqtt_broker:
            _LOGGER.warning("Kein MQTT-Broker konfiguriert, kann nicht subscriben")
            return
        
        _LOGGER.info("Starte MQTT-Subscribe-Client für Broker: %s:%d", self.mqtt_broker, self.mqtt_port)
        client_id = f"plantbot_sub_{self.entry_id or 'default'}"
        
        while True:
            try:
                # Erstelle Client-Konfiguration
                client_kwargs = self._get_mqtt_client_config(client_id)
                
                _LOGGER.debug("Verbinde MQTT-Client mit: %s", client_kwargs)
                self._mqtt_subscribe_client = MQTTClient(**client_kwargs)
                
                async with self._mqtt_subscribe_client:
                    _LOGGER.info("MQTT-Subscribe-Client verbunden")
                    
                    # Warte kurz, um sicherzustellen, dass die Verbindung vollständig etabliert ist
                    await asyncio.sleep(0.5)
                    
                    # Subscribe auf alle bekannten Stationen (verwende self.data, auch wenn aktuelles Update leer war)
                    stations_data = self.data if self.data else {}
                    _LOGGER.info("Subscribe auf MQTT-Topics für %d Stationen", len(stations_data))
                    await self._subscribe_to_stations(stations_data)
                    
                    # Warte auf Nachrichten
                    _LOGGER.info("Warte auf MQTT-Nachrichten...")
                    async for message in self._mqtt_subscribe_client.messages:
                        try:
                            _LOGGER.debug("MQTT-Nachricht empfangen: %s", message.topic.value)
                            await self._handle_mqtt_message(message.topic.value, message.payload)
                        except Exception as e:
                            _LOGGER.error("Fehler beim Verarbeiten der MQTT-Nachricht: %s", e)
                            
            except MqttError as e:
                _LOGGER.error("MQTT-Subscribe-Fehler: %s, versuche Reconnect in 10s", e)
                self._mqtt_subscribe_client = None
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                _LOGGER.info("MQTT-Subscribe abgebrochen")
                break
            except Exception as e:
                _LOGGER.error("Unerwarteter Fehler im MQTT-Subscribe: %s, versuche Reconnect in 10s", e)
                self._mqtt_subscribe_client = None
                await asyncio.sleep(10)

    async def _subscribe_to_stations(self, stations_data):
        """Subscribe auf MQTT-Topics für alle bekannten Stationen."""
        # Prüfe, ob Client existiert und im Context Manager ist
        if not self._mqtt_subscribe_client:
            _LOGGER.warning("Kein MQTT-Client vorhanden für Subscribe")
            return
        
        if not stations_data:
            _LOGGER.warning("Keine Stations-Daten für Subscribe vorhanden")
            return
        
        _LOGGER.info("Subscribe auf MQTT-Topics für %d Stationen", len(stations_data))
        subscribed_count = 0
        
        try:
            for station_key, station_data in stations_data.items():
                ip = station_data.get("ip")
                if not ip:
                    _LOGGER.debug("Station %s hat keine IP, überspringe", station_key)
                    continue
                
                # Topics für diese Station
                topic_suffixes = ("logs", "ack", "update", "sensors", "status", "valves", "alerts")
                for suffix in topic_suffixes:
                    topic = f"plantbot/{ip}/{suffix}"
                    if topic not in self._subscribed_topics:
                        try:
                            await self._mqtt_subscribe_client.subscribe(topic, qos=1)
                            self._subscribed_topics.add(topic)
                            _LOGGER.info("Subscribed auf %s", topic)
                            subscribed_count += 1
                        except Exception as e:
                            _LOGGER.warning("Fehler beim Subscribe auf %s: %s", topic, e)
                    else:
                        _LOGGER.debug("Bereits subscribed auf %s", topic)

                # Snapshot anfordern (sensors + status + valves)
                await self.request_status_snapshot(ip)
            
            _LOGGER.info("MQTT-Subscribe abgeschlossen: %d Topics abonniert", subscribed_count)
        except Exception as e:
            _LOGGER.error("Fehler beim Subscribe auf Stationen: %s", e, exc_info=True)

    async def _handle_mqtt_message(self, topic: str, payload: bytes):
        """Verarbeite empfangene MQTT-Nachricht."""
        try:
            payload_str = payload.decode('utf-8')
            data = json.loads(payload_str)
            
            # Extrahiere IP aus Topic: plantbot/{ip}/logs oder plantbot/{ip}/ack
            parts = topic.split('/')
            if len(parts) < 3:
                return
            
            ip = parts[1]
            message_type = parts[2]  # "logs" | "ack" | "update" | "sensors" | "status" | "valves"
            
            # Finde Station anhand IP
            station_id = None
            station_data = None
            for key, station in (self.data or {}).items():
                if station.get("ip") == ip:
                    station_id = key
                    station_data = station
                    break
            
            if not station_id:
                _LOGGER.warning("Keine Station gefunden für IP %s (Topic: %s)", ip, topic)
                return
            
            _LOGGER.debug("MQTT-Nachricht empfangen: %s -> %s", topic, payload_str[:100])
            
            if message_type == "logs":
                await self._handle_log_message(station_id, station_data, data)
            elif message_type == "ack":
                await self._handle_ack_message(station_id, station_data, data)
            elif message_type == "update":
                await self._handle_update_message(station_id, station_data, data)
            elif message_type == "sensors":
                await self._handle_sensors_message(station_id, station_data, data)
            elif message_type == "status":
                await self._handle_status_message(station_id, station_data, data)
            elif message_type == "valves":
                await self._handle_valves_message(station_id, station_data, data)
            elif message_type == "alerts":
                await self._handle_alert_message(station_id, station_data, data)
            else:
                _LOGGER.warning("Unbekannter MQTT-Message-Typ: %s (Topic: %s)", message_type, topic)
            
        except json.JSONDecodeError as e:
            _LOGGER.error("Fehler beim Parsen der MQTT-Nachricht von %s: %s", topic, e)
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der MQTT-Nachricht von %s: %s", topic, e)

    async def _handle_sensors_message(self, station_id, station_data, sensor_data):
        """Sensordaten von plantbot/{ip}/sensors."""
        try:
            if not self.data or station_id not in self.data:
                return
            updated_data = self.data.copy()
            station = updated_data[station_id].copy()
            body = sensor_data.get("body") if isinstance(sensor_data, dict) else None
            if isinstance(body, dict) and "Sensoren" in body:
                station["Sensoren"] = body["Sensoren"]
                if body.get("name"):
                    # Nur setzen wenn kein Server-Name vorhanden
                    if not station.get("server_name") and station.get("source") != "server":
                        station["name"] = body["name"]
            elif isinstance(sensor_data, dict) and "Sensoren" in sensor_data:
                station["Sensoren"] = sensor_data["Sensoren"]
            self._touch_mqtt_seen(station)
            updated_data[station_id] = station
            self.async_set_updated_data(updated_data)
            _LOGGER.debug(
                "Station %s: Sensoren via MQTT aktualisiert (PlantBot keys: %s)",
                station_id,
                list((station.get("Sensoren") or {}).get("PlantBot", {}).keys()),
            )
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Sensors-Nachricht: %s", e)

    async def _handle_status_message(self, station_id, station_data, status_data):
        """Status von plantbot/{ip}/status."""
        try:
            if not self.data or station_id not in self.data:
                return
            updated_data = self.data.copy()
            station = updated_data[station_id].copy()
            if status_data.get("firmware_version") is not None:
                station["current_version"] = status_data.get("firmware_version")
            if status_data.get("latest_version") is not None:
                station["latest_version"] = status_data.get("latest_version")
            if status_data.get("update_needed") is not None:
                station["update_needed"] = bool(status_data.get("update_needed"))
            if status_data.get("wifi_rssi") is not None:
                station["wifi"] = status_data.get("wifi_rssi")
            if status_data.get("free_heap") is not None:
                station["memory_usage"] = status_data.get("free_heap")
            if status_data.get("uptime_seconds") is not None:
                station["runtime"] = status_data.get("uptime_seconds")
            # Legacy: ältere Firmware sendete flow/volume/runtime noch im Status
            if status_data.get("flow") is not None:
                station["flow"] = status_data.get("flow")
            if status_data.get("last_volume_ml") is not None:
                station["lastVolume"] = status_data.get("last_volume_ml")
            elif status_data.get("lastVolume") is not None:
                station["lastVolume"] = status_data.get("lastVolume")
            if status_data.get("water_runtime") is not None:
                station["water_runtime"] = status_data.get("water_runtime")
            if status_data.get("last_reset_reason") is not None:
                station["last_reset_reason"] = status_data.get("last_reset_reason")
            if status_data.get("online") is False:
                station["status"] = "offline"
            elif status_data.get("watering") is True:
                # Backup falls Logs noch nicht kamen
                station["status"] = "am Gießen"
                station["watering_status"] = "running"
            elif status_data.get("watering") is False:
                # Idle-Signal: Logs setzen "am Gießen", Status watering:false → wieder bereit
                station["status"] = "bereit"
                station["watering_status"] = "idle"
                station["flow"] = 0
                _apply_watering_remaining(station, None, "idle")
            self._touch_mqtt_seen(station)
            updated_data[station_id] = station
            self.async_set_updated_data(updated_data)
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Status-Nachricht: %s", e)

    async def _handle_valves_message(self, station_id, station_data, valves_data):
        """Ventilzustände von plantbot/{ip}/valves."""
        try:
            if not self.data or station_id not in self.data:
                return
            updated_data = self.data.copy()
            station = updated_data[station_id].copy()
            valves = valves_data.get("valves") if isinstance(valves_data, dict) else None
            if isinstance(valves, list):
                station["valves"] = valves
            self._touch_mqtt_seen(station)
            updated_data[station_id] = station
            self.async_set_updated_data(updated_data)
            _LOGGER.debug("Station %s: %d Ventile via MQTT aktualisiert", station_id, len(valves or []))
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Valves-Nachricht: %s", e)

    @staticmethod
    def _format_alert_status(alerts: dict) -> str:
        """Menschenlesbarer Alert-Status für Sensor."""
        if not alerts:
            return "ok"
        labels = []
        for code in alerts:
            labels.append(ALERT_CODE_LABELS.get(code, code))
        return ", ".join(labels)

    async def _handle_alert_message(self, station_id, station_data, alert_data):
        """Station-Alerts von plantbot/{ip}/alerts."""
        try:
            if not self.data or station_id not in self.data:
                return
            if not isinstance(alert_data, dict):
                return

            code = alert_data.get("code")
            state = alert_data.get("state")
            if not code or not state:
                _LOGGER.warning("Alert ohne code/state ignoriert: %s", alert_data)
                return

            updated_data = self.data.copy()
            station = updated_data[station_id].copy()
            alerts = dict(station.get("alerts") or {})

            if state == "cleared":
                alerts.pop(code, None)
            else:
                alerts[code] = {
                    "state": state,
                    "severity": alert_data.get("severity", "warning"),
                    "water_level_cm": alert_data.get("water_level_cm"),
                    "min_water_cm": alert_data.get("min_water_cm"),
                    "ts": alert_data.get("ts"),
                }

            station["alerts"] = alerts
            station["alert_status"] = self._format_alert_status(alerts)
            station["last_alert"] = alert_data
            self._touch_mqtt_seen(station)
            updated_data[station_id] = station
            self.async_set_updated_data(updated_data)

            event_data = {
                "station_id": station_id,
                "station_name": station.get("name"),
                "code": code,
                "state": state,
                "severity": alert_data.get("severity", "warning"),
                "label": ALERT_CODE_LABELS.get(code, code),
                "water_level_cm": alert_data.get("water_level_cm"),
                "min_water_cm": alert_data.get("min_water_cm"),
                "alert_status": station["alert_status"],
            }
            device_id = self._device_id_for_station(station_id)
            if device_id:
                event_data["device_id"] = device_id

            self.hass.bus.async_fire(EVENT_ALERT, event_data)
            _LOGGER.info(
                "Alert %s: station=%s code=%s state=%s",
                EVENT_ALERT,
                station.get("name"),
                code,
                state,
            )
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Alert-Nachricht: %s", e)

    def _status_from_watering_log(self, log_data: dict) -> str | None:
        """Aktiv-Status aus /logs. 'bereit' kommt nur von MQTT status mit watering:false."""
        status = log_data.get("status", "running")
        phase = log_data.get("phase", "")
        if status != "running" or phase == "done":
            return None
        if phase == "dose":
            return "am Düngen"
        if phase == "flush":
            return "am Spülen"
        return "am Gießen"

    async def _handle_log_message(self, station_id, station_data, log_data):
        """Verarbeite Log-Nachricht (Live-Updates während des Gießens)."""
        try:
            # Aktualisiere Station-Daten mit Log-Informationen
            if not self.data:
                return
            
            # Erstelle Kopie der aktuellen Daten
            updated_data = self.data.copy()
            
            if station_id in updated_data:
                station = updated_data[station_id].copy()
                
                water = log_data.get("water") if isinstance(log_data.get("water"), dict) else {}
                fert = log_data.get("fertilizer") if isinstance(log_data.get("fertilizer"), dict) else {}

                # Merge: fehlende Blöcke behalten (phase-abhängig nur einer aktiv)
                prev_log = station.get("last_log") or {}
                merged_log = dict(log_data)
                if not water and isinstance(prev_log.get("water"), dict):
                    merged_log["water"] = prev_log["water"]
                    water = prev_log["water"]
                if not fert and isinstance(prev_log.get("fertilizer"), dict):
                    merged_log["fertilizer"] = prev_log["fertilizer"]
                station["last_log"] = merged_log

                status = log_data.get("status", "running")
                station["watering_status"] = status
                display_status = self._status_from_watering_log(log_data)
                if display_status is not None:
                    station["status"] = display_status

                # Aktualisiere aktuelle Werte aus nested water (wenn vorhanden)
                if water.get("actual_amount_ml") is not None:
                    station["current_watering_volume"] = water["actual_amount_ml"]
                    station["lastVolume"] = water["actual_amount_ml"]
                if water.get("actual_duration_seconds") is not None:
                    station["current_watering_duration"] = water["actual_duration_seconds"]
                    station["water_runtime"] = water["actual_duration_seconds"]
                if water.get("flow") is not None:
                    # Nach Job-Ende kein Rest-Flow anzeigen
                    if status in ("completed", "failed", "cancelled"):
                        station["flow"] = 0
                    else:
                        station["flow"] = water["flow"]
                        _LOGGER.debug("Flow-Wert aktualisiert via MQTT Log: %s", water["flow"])
                elif status in ("completed", "failed", "cancelled"):
                    station["flow"] = 0
                if water.get("percent") is not None:
                    station["watering_percent"] = water["percent"]

                _apply_watering_remaining(station, water, status)

                pump_number = water.get("pump_number")
                valve_number = water.get("valve_number")
                
                if pump_number and valve_number:
                    # Stelle sicher, dass valves Array existiert
                    station.setdefault("valves", [])
                    
                    # Suche nach vorhandenem Ventil oder erstelle neues
                    valve_found = False
                    for valve in station["valves"]:
                        valve_no = valve.get("valve_no") or valve.get("id")
                        pump_no = valve.get("pump_no")
                        
                        if (valve_no and str(valve_no) == str(valve_number)) and \
                           (pump_no is None or int(pump_no) == int(pump_number)):
                            # Aktualisiere Status: "running" = open, "completed" = closed
                            valve["state"] = "open" if status == "running" else "closed"
                            valve["pump_no"] = int(pump_number)
                            valve["valve_no"] = int(valve_number)
                            valve_found = True
                            _LOGGER.debug("Ventil-Status aktualisiert via MQTT Log: P:%d V:%d -> %s", 
                                         pump_number, valve_number, valve["state"])
                            break
                    
                    # Falls Ventil nicht gefunden, erstelle neues
                    if not valve_found:
                        new_valve = {
                            "pump_no": int(pump_number),
                            "valve_no": int(valve_number),
                            "state": "open" if status == "running" else "closed"
                        }
                        station["valves"].append(new_valve)
                        _LOGGER.debug("Neues Ventil hinzugefügt via MQTT Log: P:%d V:%d -> %s", 
                                     pump_number, valve_number, new_valve["state"])
                
                updated_data[station_id] = station
                
                # Aktualisiere Coordinator-Daten
                self._touch_mqtt_seen(station)
                self.async_set_updated_data(updated_data)
                
                _LOGGER.debug("Station %s aktualisiert mit Log-Daten: %s ml, %s s, Flow: %s", 
                             station_id, 
                             water.get("actual_amount_ml", 0),
                             water.get("actual_duration_seconds", 0),
                             water.get("flow", "N/A"))
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Log-Nachricht: %s", e)

    def _resolve_watering_context(self, station: dict, ack_data: dict) -> tuple[str | None, int | None, int | None]:
        """Resolve plant name and pump/valve from ACK (with last_log / plant_mapping fallbacks)."""
        pump_number = ack_data.get("pump_number")
        valve_number = ack_data.get("valve_number")

        if pump_number is None or valve_number is None:
            last_log = station.get("last_log") or {}
            if last_log.get("job_id") == ack_data.get("job_id"):
                water = last_log.get("water") if isinstance(last_log.get("water"), dict) else {}
                pump_number = pump_number if pump_number is not None else water.get("pump_number")
                valve_number = valve_number if valve_number is not None else water.get("valve_number")

        plant_name = ack_data.get("plant_name")
        if not plant_name and pump_number is not None and valve_number is not None:
            plant_mapping = station.get("plant_mapping") or {}
            try:
                plant_name = plant_mapping.get((int(pump_number), int(valve_number)))
            except (TypeError, ValueError):
                plant_name = None

        return plant_name, pump_number, valve_number

    def _fire_watering_finished_event(self, station_id: str, station: dict, ack_data: dict) -> None:
        """Fire homeassistant event when a watering job finishes (ACK with job_id >= 0)."""
        job_id = ack_data.get("job_id")
        try:
            # job_id > 0: Server-Job; job_id == 0: HA Zeit/Volumen; negativ: reines Ventil (kein Event)
            if job_id is None or int(job_id) < 0:
                return
        except (TypeError, ValueError):
            return

        status = ack_data.get("status", "unknown")
        if status not in ("completed", "failed", "cancelled"):
            return

        plant_name, pump_number, valve_number = self._resolve_watering_context(station, ack_data)
        event_data = {
            "station_id": station_id,
            "station_name": station.get("name"),
            "job_id": int(job_id),
            "status": status,
            "plant_name": plant_name,
            "pump_number": pump_number,
            "valve_number": valve_number,
            "amount_ml": ack_data.get("actual_amount_ml"),
            "duration_seconds": ack_data.get("actual_duration_seconds"),
        }
        device_id = self._device_id_for_station(station_id)
        if device_id:
            event_data["device_id"] = device_id
        if ack_data.get("error"):
            event_data["error"] = ack_data["error"]
        fertilizer = ack_data.get("fertilizer")
        if isinstance(fertilizer, dict):
            event_data["fertilizer"] = fertilizer

        self.hass.bus.async_fire(EVENT_WATERING_FINISHED, event_data)
        _LOGGER.info(
            "Event %s: station=%s plant=%s status=%s job_id=%s device_id=%s",
            EVENT_WATERING_FINISHED,
            station.get("name"),
            plant_name,
            status,
            job_id,
            device_id,
        )

    def _device_id_for_station(self, station_id: str) -> str | None:
        """HA device_id zur Station (für Device-Trigger / Event-Attribut)."""
        from homeassistant.helpers import device_registry as dr

        from .const import station_device_identifiers

        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            identifiers=station_device_identifiers(station_id)
        )
        return device.id if device else None

    async def _handle_ack_message(self, station_id, station_data, ack_data):
        """Verarbeite ACK-Nachricht (Bewässerung beendet)."""
        try:
            if not self.data:
                return
            
            # Erstelle Kopie der aktuellen Daten
            updated_data = self.data.copy()
            
            if station_id in updated_data:
                station = updated_data[station_id].copy()
                
                # Füge ACK-Daten hinzu
                station["last_ack"] = ack_data
                status = ack_data.get("status", "unknown")
                station["watering_status"] = status
                
                # Wenn beendet, setze aktuelle Werte zurück
                if status in ["completed", "failed", "cancelled"]:
                    station["current_watering_volume"] = ack_data.get("actual_amount_ml", 0)
                    station["current_watering_duration"] = ack_data.get("actual_duration_seconds", 0)
                    station["flow"] = 0
                    _apply_watering_remaining(station, None, status)
                    # Aktualisiere auch lastVolume und water_runtime für Sensoren
                    if "actual_amount_ml" in ack_data:
                        station["lastVolume"] = ack_data["actual_amount_ml"]
                    if "actual_duration_seconds" in ack_data:
                        station["water_runtime"] = ack_data["actual_duration_seconds"]
                
                # NEU: Setze Ventil-Status auf "closed" wenn Job beendet ist
                pump_number = ack_data.get("pump_number")
                valve_number = ack_data.get("valve_number")
                
                if pump_number and valve_number and status in ["completed", "failed", "cancelled"]:
                    # Stelle sicher, dass valves Array existiert
                    station.setdefault("valves", [])
                    
                    # Suche nach vorhandenem Ventil und setze auf closed
                    valve_found = False
                    for valve in station["valves"]:
                        valve_no = valve.get("valve_no") or valve.get("id")
                        pump_no = valve.get("pump_no")
                        
                        if (valve_no and str(valve_no) == str(valve_number)) and \
                           (pump_no is None or int(pump_no) == int(pump_number)):
                            valve["state"] = "closed"
                            valve["pump_no"] = int(pump_number)
                            valve["valve_no"] = int(valve_number)
                            valve_found = True
                            _LOGGER.debug("Ventil-Status auf 'closed' gesetzt via MQTT ACK: P:%d V:%d", 
                                         pump_number, valve_number)
                            break
                    
                    # Falls Ventil nicht gefunden, erstelle neues mit closed
                    if not valve_found:
                        new_valve = {
                            "pump_no": int(pump_number),
                            "valve_no": int(valve_number),
                            "state": "closed"
                        }
                        station["valves"].append(new_valve)
                        _LOGGER.debug("Neues Ventil hinzugefügt via MQTT ACK (closed): P:%d V:%d", 
                                     pump_number, valve_number)
                
                updated_data[station_id] = station
                
                # Aktualisiere Coordinator-Daten
                self._touch_mqtt_seen(station)
                self.async_set_updated_data(updated_data)
                self._fire_watering_finished_event(station_id, station, ack_data)
                
                _LOGGER.info("Station %s: Bewässerung %s - %s ml in %s s",
                            station_id,
                            status,
                            ack_data.get("actual_amount_ml", 0),
                            ack_data.get("actual_duration_seconds", 0))
                
                # Trigger Refresh nach kurzer Verzögerung, um finalen Status zu holen
                await asyncio.sleep(1)
                await self.async_request_refresh()
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der ACK-Nachricht: %s", e)

    async def _handle_update_message(self, station_id, station_data, update_data):
        """Verarbeite Update-Status (OTA) via MQTT."""
        try:
            if not self.data:
                return

            updated_data = self.data.copy()
            if station_id not in updated_data:
                return

            station = updated_data[station_id].copy()
            station["firmware_update"] = update_data
            self._touch_mqtt_seen(station)
            updated_data[station_id] = station
            self.async_set_updated_data(updated_data)

            status = str(update_data.get("status", ""))
            progress = str(update_data.get("progress", 0))
            if status.lower() in ["done", "complete", "success", "failed", "error", "started"]:
                _LOGGER.info("Station %s: Update-Status via MQTT: %s (%s%%)", station_id, status, progress)
            else:
                _LOGGER.debug("Station %s: Update-Status via MQTT: %s (%s%%)", station_id, status, progress)
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Update-Status-Nachricht: %s", e)

