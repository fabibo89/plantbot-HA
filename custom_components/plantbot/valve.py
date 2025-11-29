import logging
from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from .const import DOMAIN
from homeassistant.core import callback
from homeassistant.components.valve import ValveDeviceClass
from homeassistant.exceptions import HomeAssistantError
import aiohttp
import asyncio

_LOGGER = logging.getLogger(__name__)
ENTITIES: dict[str, "PlantbotHAValve"] = {}

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    
    # Prüfe ob coordinator.data vorhanden ist
    if not coordinator.data:
        _LOGGER.warning("Coordinator hat noch keine Daten, warte auf ersten Update")
        # Erstelle leere Liste, Entities werden bei nächstem Update hinzugefügt
        async_add_entities([])
        return
    
    _LOGGER.debug("Initialisiere Valve-Plattform mit %d Stationen", len(coordinator.data))

    for station_id, station in coordinator.data.items():
        num_pumps = station.get("num_pumps", 1)
        num_valves = station.get("num_valves", 8)
        fertilizer_pump = station.get("fertilizer_pump_number")
        plant_mapping = station.get("plant_mapping", {})
        station_name = station.get("name", f"Station {station_id}")
        
        _LOGGER.debug("Station %s (%s): %d Pumpen, %d Ventile, Dünger-Pumpe: %s", 
                     station_id, station_name, num_pumps, num_valves, fertilizer_pump)
        
        # Bestimme relevante Pumpen (alle außer Dünger-Pumpe)
        relevant_pumps = []
        for pump in range(1, num_pumps + 1):
            if pump != fertilizer_pump:
                relevant_pumps.append(pump)
        
        # Erstelle Ventile für jede relevante Pumpe
        for pump_number in relevant_pumps:
            for valve_number in range(1, num_valves + 1):
                # Prüfe ob Pflanze vorhanden
                lookup_key = (pump_number, valve_number)
                plant_name = plant_mapping.get(lookup_key)
                
                if plant_name:
                    valve_name = f"{plant_name} (P:{pump_number},V:{valve_number})"
                else:
                    valve_name = f"Pumpe: {pump_number} / Ventil: {valve_number}"
                
                valve_data = {
                    "id": valve_number,
                    "pump_number": pump_number,
                    "name": valve_name,
                }
                
                entity = PlantbotHAValve(coordinator, station_id, station_name, valve_data)
                ENTITIES[entity.unique_id] = entity
                entities.append(entity)
                _LOGGER.debug("Registriere Ventil: %s (P:%d, V:%d)", valve_name, pump_number, valve_number)

    async_add_entities(entities)

class PlantbotHAValve(ValveEntity):
    def __init__(self, coordinator, station_id, station_name, valve_data):
        self.valve_name = valve_data.get("name", f"Ventil {valve_data.get('id')}")
        self._attr_name = self.valve_name
        self.coordinator = coordinator
        self.station_id = str(station_id)
        self.station_name = station_name
        self.valve_id = str(valve_data.get("id"))
        self.pump_number = valve_data.get("pump_number", 1)
        self._attr_unique_id = f"{DOMAIN}_{self.station_id}_pump_{self.pump_number}_valve_{self.valve_id}"
        self._attr_reports_position = False
        self._attr_supported_features = (
            ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
        )
        self._attr_should_poll = False
        self._attr_device_class = ValveDeviceClass.WATER
        self.station_ip = coordinator.data.get(self.station_id, {}).get("ip")
        
        _LOGGER.debug("Erstelle Ventil-Entität P:%d V:%s (station: %s, IP: %s)", 
                     self.pump_number, self.valve_id, self.station_id, self.station_ip)

    @property
    def available(self):
        return self.coordinator.last_update_success and self._get_valve() is not None

    def _get_valve(self):
        """Hole Ventil-Status vom PlantBot /status Endpoint."""
        station = self.coordinator.data.get(self.station_id, {})
        valves = station.get("valves", [])

        # Suche nach Ventil mit passender pump_number und valve_id
        # Der /status Endpoint gibt pump_no und valve_no zurück
        for v in valves:
            # Unterstütze beide Formate für Rückwärtskompatibilität
            valve_no = v.get("valve_no") or v.get("id")
            pump_no = v.get("pump_no")
            
            # Prüfe ob valve_id übereinstimmt
            if valve_no and str(valve_no) == str(self.valve_id):
                # Wenn pump_no vorhanden ist, prüfe auch diese
                if pump_no is None or int(pump_no) == self.pump_number:
                    return v

        # Falls kein Ventil im Status gefunden, erstelle Dummy-Objekt
        _LOGGER.debug("Valve %s (Pumpe %s) nicht im Status gefunden für Station %s, verwende Standard", 
                     self.valve_id, self.pump_number, self.station_id)
        return {"valve_no": int(self.valve_id), "pump_no": self.pump_number, "state": "closed"}

    async def async_open_valve(self, **kwargs):
        if not self.station_ip:
            raise HomeAssistantError("Keine IP-Adresse für PlantBot verfügbar")
        await self._send_command("open")

    async def async_close_valve(self, **kwargs):
        if not self.station_ip:
            raise HomeAssistantError("Keine IP-Adresse für PlantBot verfügbar")
        await self._send_command("close")

    async def _send_command(self, command):
        """Sende Ventil-Befehl an PlantBot."""
        success = await self.coordinator.send_valve_command(
            self.station_ip,
            self.pump_number,
            self.valve_id,
            command
        )
        if not success:
            raise HomeAssistantError(f"Fehler beim Senden des Ventil-Befehls: {command}")

    async def async_added_to_hass(self):
        self.coordinator.async_add_listener(self._handle_coordinator_update)
        self.async_write_ha_state()
        await super().async_added_to_hass()
        _LOGGER.debug("async_added_to_hass %s", self.entity_id)
        if self.entity_id:
            ENTITIES[self.entity_id] = self
            _LOGGER.debug("Registriere Valve: %s → entity_id: %s", self.valve_name, self.entity_id)

    @callback
    def _handle_coordinator_update(self):
        valve = self._get_valve()
        if valve:
            state = valve.get("state")
            self._attr_is_closed = state == "closed"
        else:
            _LOGGER.warning("no Valve")
            self._attr_is_closed = None
        self.async_write_ha_state()

    @property
    def device_info(self):
        info = {
            "identifiers": {(DOMAIN, f"station_{self.station_id}")},
            "name": self.station_name,
            "manufacturer": "PlantBot",
            "model": "Bewässerungsstation",
        }
        if self.station_ip:
            info["configuration_url"] = f"http://{self.station_ip}"
        return info
    
    async def open_for_seconds(self, duration):
        """Öffne Ventil für eine bestimmte Dauer in Sekunden."""
        if not self.station_ip:
            raise HomeAssistantError("Keine IP-Adresse für PlantBot verfügbar")
        
        success = await self.coordinator.send_valve_command(
            self.station_ip,
            self.pump_number,
            self.valve_id,
            "open",
            duration=duration
        )
        if not success:
            raise HomeAssistantError(f"Fehler beim Öffnen des Ventils für {duration} Sekunden")

    async def open_for_volume(self, volume):
        """Öffne Ventil für ein bestimmtes Volumen in ml."""
        if not self.station_ip:
            raise HomeAssistantError("Keine IP-Adresse für PlantBot verfügbar")
        
        success = await self.coordinator.send_valve_command(
            self.station_ip,
            self.pump_number,
            self.valve_id,
            "open",
            volume=volume
        )
        if not success:
            raise HomeAssistantError(f"Fehler beim Öffnen des Ventils für {volume} ml")

