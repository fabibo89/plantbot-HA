import logging
from datetime import timedelta
import aiohttp
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import asyncio
import json
from aiomqtt import Client as MQTTClient
from aiomqtt.exceptions import MqttError

_LOGGER = logging.getLogger(__name__)

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
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self):
        """Fetch data from PlantBot(s)."""
        _LOGGER.info("_async_update_data aufgerufen, connection_type: %s", self.connection_type)
        try:
            if self.connection_type == "server":
                _LOGGER.info("Rufe _fetch_from_server auf")
                result = await self._fetch_from_server()
            else:  # connection_type == "device"
                _LOGGER.info("Rufe _fetch_from_device auf für IP: %s", self.device_ip)
                result = await self._fetch_from_device(self.device_ip)
            
            # Stelle sicher, dass result ein Dictionary ist
            if not isinstance(result, dict):
                _LOGGER.warning("Unerwartetes Ergebnis-Format: %s, verwende leeres Dict", type(result))
                result = {}
            
            # Starte MQTT-Subscribe nach erstem erfolgreichen Update
            # Verwende result oder vorhandene Daten (falls result leer ist wegen Timeouts)
            data_for_subscribe = result if result else (self.data if self.data else {})
            if data_for_subscribe and self.mqtt_broker and not self._mqtt_subscribe_task:
                _LOGGER.info("Starte MQTT-Subscribe für %d Stationen", len(data_for_subscribe))
                self._mqtt_subscribe_task = self.hass.async_create_task(self._start_mqtt_subscribe())
            
            return result
        except asyncio.CancelledError:
            _LOGGER.warning("Abbruch während Datenabruf – vermutlich durch Shutdown oder Timeout")
            self.last_update_success = False
            return {}  # Rückgabe leeres Dict statt Exception
        except aiohttp.ClientError as err:
            _LOGGER.error("Verbindung zu PlantBot fehlgeschlagen: %s", err)
            self.last_update_success = False
            return {}  # Rückgabe leeres Dict statt Exception
        except Exception as err:
            _LOGGER.exception("Fehler bei der Kommunikation mit PlantBot:")
            self.last_update_success = False
            return {}  # Rückgabe leeres Dict statt Exception

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
            endpoint = f"{self.server_url}/api/v1/stations"
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
                except Exception as e:
                    _LOGGER.warning("Fehler beim Abrufen der Pflanzen für Station %s: %s", station_id, e, exc_info=True)
                
                try:
                    device_data = await self._fetch_from_device(ip)
                    # Füge Server-Metadaten hinzu
                    if device_data:
                        # Stelle sicher, dass wir den korrekten Key verwenden (basierend auf station_id)
                        result_key = f"station_{station_id}"
                        # Wenn device_data einen anderen Key hat, verwenden wir unseren Key
                        if result_key not in device_data and device_data:
                            # Nimm die Daten aus dem ersten (und einzigen) Eintrag
                            data = list(device_data.values())[0]
                            device_data = {result_key: data}
                        
                        for key, data in device_data.items():
                            data["server_id"] = station_id
                            data["server_name"] = station_name
                            # Verwende Server-Namen statt Device-Namen (IP)
                            if station_name:
                                data["name"] = station_name
                            data["source"] = "server"
                            # Füge Hardware-Metadaten hinzu
                            data["num_pumps"] = station.get("num_pumps", 1)
                            data["num_valves"] = station.get("num_valves", 8)
                            data["fertilizer_pump_number"] = station.get("fertilizer_pump_number")
                            data["plant_mapping"] = plant_mapping
                            data["sensor_mapping"] = sensor_mapping  # identifier -> plant_name
                        result.update(device_data)
                except Exception as e:
                    _LOGGER.warning("Fehler beim Abrufen von Daten von PlantBot %s (%s): %s", station_name, ip, e)
                    # Erstelle trotzdem einen Eintrag mit Server-Daten
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
                        "sensor_mapping": sensor_mapping,  # identifier -> plant_name
                    }
            
            return result
            
        except Exception as e:
            _LOGGER.error("Fehler beim Abrufen vom Server: %s", e, exc_info=True)
            return result  # Rückgabe leeres Dict statt Exception

    async def _fetch_from_device(self, ip):
        """Hole Daten direkt von einem PlantBot-Gerät."""
        # Verwende /status Endpoint mit GET (wie im Hardware-Code definiert)
        endpoint = f"http://{ip}/status"
        
        try:
            async with self.session.get(endpoint, ssl=False, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    _LOGGER.debug("Daten von PlantBot %s erhalten (GET /status): %s", ip, data)
                    
                    # /status gibt direktes Station-Objekt mit Sensoren zurück
                    if isinstance(data, dict):
                        station_id = data.get("id", ip)
                        # Normalisiere station_id
                        if isinstance(station_id, str) and station_id.startswith("station_"):
                            station_id = station_id.replace("station_", "")
                        elif station_id is None:
                            station_id = ip
                        
                        data["source"] = "device"
                        data["ip"] = ip
                        data["id"] = str(station_id)
                        
                        # Stelle sicher, dass Sensoren-Objekt vorhanden ist
                        if "Sensoren" not in data:
                            data["Sensoren"] = {}
                        
                        # Für Device-Modus: Standard-Werte setzen (können später vom Server kommen)
                        if "num_pumps" not in data:
                            data["num_pumps"] = 1
                        if "num_valves" not in data:
                            data["num_valves"] = 8
                        if "plant_mapping" not in data:
                            data["plant_mapping"] = {}
                        if "sensor_mapping" not in data:
                            data["sensor_mapping"] = {}
                        
                        return {f"station_{station_id}": data}
                    else:
                        _LOGGER.warning("Unerwartetes Datenformat von %s: %s", endpoint, type(data))
                        raise UpdateFailed(f"Unerwartetes Datenformat von PlantBot {ip}")
                else:
                    _LOGGER.error("Fehler beim Abrufen von %s: HTTP %s", endpoint, response.status)
                    raise UpdateFailed(f"Fehler beim Abrufen von PlantBot {ip}: HTTP {response.status}")
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout beim Zugriff auf %s", endpoint)
            raise UpdateFailed(f"Timeout beim Abrufen von PlantBot {ip}")
        except Exception as e:
            _LOGGER.error("Fehler beim Zugriff auf %s: %s", endpoint, e)
            raise UpdateFailed(f"Fehler beim Abrufen von PlantBot {ip}: {e}")

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
            
            topic = f"plantbot/{ip}/commands/valve"
            payload_json = json.dumps(payload)
            
            _LOGGER.debug("Sende MQTT-Befehl: %s -> %s", topic, payload_json)
            
            # Erstelle MQTT-Client für diesen Befehl
            client_id = f"plantbot_{self.entry_id or 'default'}_{id(self)}"
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
                logs_topic = f"plantbot/{ip}/logs"
                ack_topic = f"plantbot/{ip}/ack"
                
                # Subscribe auf logs
                if logs_topic not in self._subscribed_topics:
                    try:
                        await self._mqtt_subscribe_client.subscribe(logs_topic, qos=1)
                        self._subscribed_topics.add(logs_topic)
                        _LOGGER.info("Subscribed auf %s", logs_topic)
                        subscribed_count += 1
                    except Exception as e:
                        _LOGGER.warning("Fehler beim Subscribe auf %s: %s", logs_topic, e)
                else:
                    _LOGGER.debug("Bereits subscribed auf %s", logs_topic)
                
                # Subscribe auf ack
                if ack_topic not in self._subscribed_topics:
                    try:
                        await self._mqtt_subscribe_client.subscribe(ack_topic, qos=1)
                        self._subscribed_topics.add(ack_topic)
                        _LOGGER.info("Subscribed auf %s", ack_topic)
                        subscribed_count += 1
                    except Exception as e:
                        _LOGGER.warning("Fehler beim Subscribe auf %s: %s", ack_topic, e)
                else:
                    _LOGGER.debug("Bereits subscribed auf %s", ack_topic)
            
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
            message_type = parts[2]  # "logs" oder "ack"
            
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
            
            _LOGGER.info("MQTT-Nachricht empfangen: %s -> %s", topic, payload_str[:100])
            
            if message_type == "logs":
                _LOGGER.debug("Verarbeite Log-Nachricht für Station %s", station_id)
                await self._handle_log_message(station_id, station_data, data)
            elif message_type == "ack":
                _LOGGER.debug("Verarbeite ACK-Nachricht für Station %s", station_id)
                await self._handle_ack_message(station_id, station_data, data)
            else:
                _LOGGER.warning("Unbekannter MQTT-Message-Typ: %s (Topic: %s)", message_type, topic)
            
        except json.JSONDecodeError as e:
            _LOGGER.error("Fehler beim Parsen der MQTT-Nachricht von %s: %s", topic, e)
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der MQTT-Nachricht von %s: %s", topic, e)

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
                
                # Füge Log-Daten hinzu
                station["last_log"] = log_data
                status = log_data.get("status", "running")
                station["watering_status"] = status
                
                # Aktualisiere aktuelle Werte
                if "actual_amount_ml" in log_data:
                    station["current_watering_volume"] = log_data["actual_amount_ml"]
                    # Aktualisiere auch lastVolume für Sensor
                    station["lastVolume"] = log_data["actual_amount_ml"]
                if "actual_duration_seconds" in log_data:
                    station["current_watering_duration"] = log_data["actual_duration_seconds"]
                    # Aktualisiere auch water_runtime für Sensor
                    station["water_runtime"] = log_data["actual_duration_seconds"]
                
                # NEU: Aktualisiere Flow-Wert aus Logs (falls vorhanden)
                if "flow" in log_data:
                    station["flow"] = log_data["flow"]
                    _LOGGER.debug("Flow-Wert aktualisiert via MQTT Log: %s", log_data["flow"])
                
                # NEU: Aktualisiere Ventil-Status basierend auf Log-Daten
                pump_number = log_data.get("pump_number")
                valve_number = log_data.get("valve_number")
                
                if pump_number and valve_number:
                    # Stelle sicher, dass valves Array existiert
                    if "valves" not in station:
                        station["valves"] = []
                    
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
                self.async_set_updated_data(updated_data)
                
                _LOGGER.debug("Station %s aktualisiert mit Log-Daten: %s ml, %s s, Flow: %s", 
                             station_id, 
                             log_data.get("actual_amount_ml", 0),
                             log_data.get("actual_duration_seconds", 0),
                             log_data.get("flow", "N/A"))
        except Exception as e:
            _LOGGER.error("Fehler beim Verarbeiten der Log-Nachricht: %s", e)

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
                    if "valves" not in station:
                        station["valves"] = []
                    
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
                self.async_set_updated_data(updated_data)
                
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

