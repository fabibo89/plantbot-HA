from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class PlantBotHttpButton(ButtonEntity):
    """Sendet einen HTTP-GET-Befehl direkt an die PlantBot-Station."""

    def __init__(
        self,
        coordinator,
        hass,
        station_id,
        station_name,
        station_ip,
        *,
        suffix,
        name,
        path,
        icon,
        entity_category=EntityCategory.CONFIG,
    ):
        self.coordinator = coordinator
        self._session = async_get_clientsession(hass)
        self.station_id = str(station_id)
        self.station_name = station_name
        self.station_ip = station_ip
        self._path = path

        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{suffix}_{self.station_id}"
        self._attr_entity_category = entity_category
        self._attr_icon = icon

        station_key = (
            self.station_id
            if self.station_id.startswith("station_")
            else f"station_{self.station_id}"
        )
        if coordinator.data:
            station_data = coordinator.data.get(station_key, {})
            self.station_ip = (
                station_data.get("ip")
                or station_data.get("ip_address")
                or station_ip
            )
        else:
            self.station_ip = station_ip

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"station_{self.station_id}")},
            "name": self.station_name,
            "manufacturer": "PlantBot",
            "model": "Bewässerungsstation",
            "configuration_url": f"http://{self.station_ip}",
        }

    async def async_press(self):
        url = f"http://{self.station_ip}{self._path}"
        _LOGGER.info("Sende %s an %s", self._attr_name, url)

        try:
            async with self._session.get(url, timeout=5, ssl=False) as response:
                if response.status == 200:
                    _LOGGER.info("%s erfolgreich an %s", self._attr_name, self.station_name)
                else:
                    _LOGGER.warning(
                        "%s fehlgeschlagen (%s): HTTP %s",
                        self._attr_name,
                        self.station_name,
                        response.status,
                    )
        except Exception as e:
            _LOGGER.error(
                "Fehler beim Senden von %s an %s: %s",
                self._attr_name,
                self.station_name,
                e,
            )

    @property
    def available(self):
        if not self.coordinator.data:
            return False

        station_key = (
            self.station_id
            if self.station_id.startswith("station_")
            else f"station_{self.station_id}"
        )
        if station_key not in self.coordinator.data:
            return False

        station_data = self.coordinator.data[station_key]
        return bool(station_data.get("available", True))

    async def async_update(self):
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self):
        self.coordinator.async_add_listener(self.async_write_ha_state)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    buttons = []

    if not coordinator.data:
        _LOGGER.warning("Coordinator hat noch keine Daten, warte auf ersten Update")
        async_add_entities([])
        return

    _LOGGER.debug("Initialisiere Button-Plattform mit %d Stationen", len(coordinator.data))

    for station_id, station in coordinator.data.items():
        station_name = station.get("name", f"Station {station_id}")
        station_ip = station.get("ip") or station.get("ip_address")

        if not station_ip:
            if isinstance(station_id, str) and "." in station_id:
                station_ip = station_id
            else:
                _LOGGER.warning(
                    "Keine IP-Adresse für Station %s gefunden, überspringe Buttons",
                    station_id,
                )
                continue

        _LOGGER.info(
            "Erstelle Buttons für Station %s (ID: %s, IP: %s)",
            station_name,
            station_id,
            station_ip,
        )

        buttons.append(
            PlantBotHttpButton(
                coordinator,
                hass,
                station_id,
                station_name,
                station_ip,
                suffix="reset",
                name=f"Reset {station_name}",
                path="/HA/reset",
                icon="mdi:restart",
            )
        )
        buttons.append(
            PlantBotHttpButton(
                coordinator,
                hass,
                station_id,
                station_name,
                station_ip,
                suffix="display_on",
                name=f"Display an {station_name}",
                path="/display/on",
                icon="mdi:monitor",
                entity_category=None,
            )
        )

    async_add_entities(buttons, True)
