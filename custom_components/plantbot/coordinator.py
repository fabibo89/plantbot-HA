import logging
from datetime import timedelta
import aiohttp
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN
import json
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import asyncio

_LOGGER = logging.getLogger(__name__)

class PlantbotCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, server_url):
        self.hass = hass
        self.server_url = server_url
        self.session = async_get_clientsession(hass)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    def _merge_valve_data(self, server_valves, device_valves):
        """Führt Server-Ventil-Metadaten mit Device-Status zusammen.
        
        Server: [{"id": 21, "name": "P:1-V:1 Zucchini", "vent": 1}]
        Device: [{"id": 1, "name": "Ventil 1", "state": "closed"}]
        Mapping: server.vent == device.id
        """
        # Device-States nach ID indexieren für schnellen Zugriff
        device_states = {valve["id"]: valve.get("state", "unknown") for valve in device_valves}
        
        merged = []
        for server_valve in server_valves:
            vent_id = server_valve.get("vent")
            if vent_id and vent_id in device_states:
                # Server-Metadaten + Device-Status zusammenführen
                merged_valve = server_valve.copy()
                merged_valve["state"] = device_states[vent_id]
                merged.append(merged_valve)
                _LOGGER.debug("Ventil %s (%s) hat Status: %s", 
                            server_valve.get("name", f"ID {server_valve.get('id')}"), 
                            vent_id, device_states[vent_id])
            else:
                # Server-Ventil ohne Device-Status (Fallback: unknown)
                merged_valve = server_valve.copy()
                merged_valve["state"] = "unknown"
                merged.append(merged_valve)
                _LOGGER.warning("Ventil %s (vent=%s) hat keinen Device-Status", 
                              server_valve.get("name", f"ID {server_valve.get('id')}"), vent_id)
        
        return merged

    async def _async_update_data(self):
        try:
            # Versuche zuerst Server-Endpunkt
            async with self.session.get(f"{self.server_url}/status", ssl=False) as response:
                _LOGGER.debug("Empfangene Daten vom Server/Device: %s", f"{self.server_url}/status")
                if response.status == 200:
                    raw = await response.json()
                    _LOGGER.debug("Antwort-JSON: %s", raw)
                    # Prüfe ob Server-Antwort (Liste von Stationen)
                    if raw.get("status") == "success" and isinstance(raw.get("data"), list):
                        basis_data = raw["data"]
                        _LOGGER.debug("Server-Basis-Daten: %s", basis_data)
                        result = {f"station_{station['id']}": station for station in basis_data}
                        
                        # Server-ID aus URL extrahieren (z.B. aus http://192.168.1.100:3000)
                        server_id = self.server_url.split("://")[1].split(":")[0].split(".")[-1]  # Letztes Oktett der IP
                        
                        # Für jede Station: Hole Detaildaten vom Gerät, falls IP vorhanden
                        for station in basis_data:
                            ip = station.get("ip")
                            station_id = station['id']
                            key_station_id = f"station_{station_id}"
                            
                            # Markiere als Server-Connection und füge Server-ID hinzu
                            result[key_station_id]["source"] = "server"
                            result[key_station_id]["server_id"] = server_id
                            
                            if ip:
                                try:
                                    async with self.session.get(f"http://{ip}/status", timeout=5) as dev_resp:
                                        if dev_resp.status == 200:
                                            device_data = await dev_resp.json()
                                            _LOGGER.debug("Device-Daten von %s: %s", ip, device_data)
                                            
                                            # Name aus Gerätedaten übernehmen, falls vorhanden
                                            if "name" in device_data:
                                                result[key_station_id]["name"] = device_data["name"]
                                            
                                            # Ventil-Daten intelligent zusammenführen
                                            server_valves = result[key_station_id].get("valves", [])
                                            device_valves = device_data.get("valves", [])
                                            merged_valves = self._merge_valve_data(server_valves, device_valves)
                                            
                                            # Device-Daten übernehmen, aber Ventile separat behandeln
                                            result[key_station_id].update(device_data)
                                            result[key_station_id]["valves"] = merged_valves
                                            
                                            _LOGGER.debug("Ventil-Daten für Station %s zusammengeführt: %d Server-Ventile, %d Device-States", 
                                                        station_id, len(server_valves), len(device_valves))
                                        else:
                                            self.last_update_success = False
                                            _LOGGER.warning("Gerät %s antwortet nicht wie erwartet (%s)", ip, dev_resp.status)
                                except Exception as e:
                                    _LOGGER.warning("Fehler beim Statusabruf von Gerät %s: %s", ip, e)
                                    self.last_update_success = False
                        return result
                    # Einzelgerät-Modus: Daten direkt als Station
                    elif "status" in raw and "data" in raw and isinstance(raw["data"], dict):
                        # Einzelgerät liefert ein dict unter "data"
                        station = raw["data"]
                        station_id = station.get("id", "single")
                        station_name = station.get("name") or station.get("ip") or f"Station {station_id}"
                        station["name"] = station_name
                        station["source"] = "device"  # Markiere als Device-Connection
                        return {f"station_{station_id}": station}
                    else:
                        # Fallback: Versuche /status direkt (Einzelgerät)
                        async with self.session.get(f"{self.server_url}/status", ssl=False) as dev_resp:
                            if dev_resp.status == 200:
                                device_data = await dev_resp.json()
                                station_id = device_data.get("id", "single")
                                station_name = device_data.get("name") or device_data.get("ip") or f"Station {station_id}"
                                device_data["name"] = station_name
                                device_data["source"] = "device"  # Markiere als Device-Connection
                                return {f"station_{station_id}": device_data}
                            else:
                                raise UpdateFailed(f"Status {dev_resp.status}")
                else:
                    # Kein Erfolg beim Server-Endpunkt, versuche Einzelgerät
                    async with self.session.get(f"{self.server_url}/status", ssl=False) as dev_resp:
                        if dev_resp.status == 200:
                            device_data = await dev_resp.json()
                            station_id = device_data.get("id", "single")
                            device_data["source"] = "device"  # Markiere als Device-Connection
                            return {f"station_{station_id}": device_data}
                        else:
                            raise UpdateFailed(f"Status {dev_resp.status}")
        except asyncio.CancelledError:
            _LOGGER.warning("Abbruch während Datenabruf – vermutlich durch Shutdown oder Timeout")
            self.last_update_success = False
            raise
        except aiohttp.ClientError as err:
            _LOGGER.error("Verbindung zu PlantBot fehlgeschlagen: %s", err)
            self.last_update_success = False
            raise UpdateFailed from err
        except Exception as err:
            _LOGGER.exception("Fehler bei der Kommunikation mit PlantBot:")
            self.last_update_success = False
            raise UpdateFailed(f"Fehler: {err}")

