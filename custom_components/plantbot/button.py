from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    buttons = []
    
    # Prüfe ob coordinator.data vorhanden ist
    if not coordinator.data:
        _LOGGER.warning("Coordinator hat noch keine Daten, warte auf ersten Update")
        async_add_entities([])
        return
    
    _LOGGER.debug("Initialisiere Button-Plattform mit %d Stationen", len(coordinator.data))

    for station_id, station in coordinator.data.items():
        station_name = station.get("name", f"Station {station_id}")
        # Hole IP-Adresse - prüfe verschiedene mögliche Keys
        station_ip = station.get("ip") or station.get("ip_address")
        
        # Wenn keine IP gefunden, versuche station_id zu verwenden (falls es eine IP ist)
        if not station_ip:
            # Prüfe ob station_id selbst eine IP-Adresse ist
            if isinstance(station_id, str) and "." in station_id:
                station_ip = station_id
            else:
                _LOGGER.warning("Keine IP-Adresse für Station %s gefunden, überspringe Reset-Button", station_id)
                continue
        
        _LOGGER.info("Erstelle Reset-Button für Station %s (ID: %s, IP: %s)", station_name, station_id, station_ip)
        buttons.append(PlantBotResetButton(coordinator, hass, station_id, station_name, station_ip))

    async_add_entities(buttons, True)


class PlantBotResetButton(ButtonEntity):
    
    def __init__(self, coordinator, hass, station_id, station_name, station_ip):
        self.coordinator = coordinator
        self._session = async_get_clientsession(hass)
        self.station_id = str(station_id)
        self.station_name = station_name
        self.station_ip = station_ip
        
        self._attr_name = f"Reset {station_name}"
        self._attr_unique_id = f"{DOMAIN}_reset_{self.station_id}"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_icon = "mdi:restart"
        
        # Stelle sicher, dass station_ip gesetzt ist
        # Coordinator speichert Keys als "station_{id}"
        station_key = self.station_id if self.station_id.startswith("station_") else f"station_{self.station_id}"
        # Hole IP aus coordinator.data falls verfügbar, sonst verwende übergebene IP
        if coordinator.data:
            station_data = coordinator.data.get(station_key, {})
            self.station_ip = station_data.get("ip") or station_data.get("ip_address") or station_ip
        else:
            self.station_ip = station_ip

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"station_{self.station_id}")},
            "name": self.station_name,
            "manufacturer": "PlantBot",
            "model": "Bewässerungsstation",
            "configuration_url": f"http://{self.station_ip}"
        }

    async def async_press(self):
        """Sende Reset-Befehl an das PlantBot-Gerät."""
        url = f"http://{self.station_ip}/HA/reset"
        _LOGGER.info("Sende Reset-Befehl an %s", url)
        
        try:
            async with self._session.get(url, timeout=5, ssl=False) as response:
                if response.status == 200:
                    _LOGGER.info("Reset-Befehl erfolgreich gesendet an %s", self.station_name)
                else:
                    _LOGGER.warning("Reset-Request fehlgeschlagen (%s): HTTP %s", self._attr_name, response.status)
        except Exception as e:
            _LOGGER.error("Fehler beim Senden des Reset-Requests an %s: %s", self._attr_name, e)

    @property
    def available(self):
        return self.coordinator.last_update_success

    async def async_update(self):
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self):
        self.coordinator.async_add_listener(self.async_write_ha_state)

