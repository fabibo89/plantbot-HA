from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
import aiohttp
import asyncio

from .const import DOMAIN
import logging

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for station_id, station in coordinator.data.items():
        _LOGGER.debug("Update-Check für %s - %s", station_id, station.get("update_needed"))
        station_ip = station.get("ip")
        update_needed = station.get("update_needed")
        if not (update_needed is True or str(update_needed).lower() == "true"):
            _LOGGER.debug("Kein Update erforderlich oder kein update_needed vorhanden für %s", station_id)
            #continue

    entities.append(PlantbotFirmwareUpdate(coordinator, station_id, station.get("name", f"Station {station_id}"), station_ip))
    
    async_add_entities(entities)

class PlantbotFirmwareUpdate(UpdateEntity):

    def __init__(self, coordinator, station_id, station_name,station_ip):
        self.coordinator = coordinator
        self.station_id = str(station_id)
        self.station_name = station_name
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._station_ip = station_ip
        self._attr_unique_id = f"plantbot_update_{self.station_id}"
        self._attr_name = f"{station_name} Firmware Update"
        self._attr_title = f"{station_name} Firmware"
        self._attr_in_progress = False
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        )
        self._progress = None
        self._in_progress = False
        self._update_data = {}

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"station_{self.station_id}")},
            name=self.station_name,
            manufacturer="PlantBot",
            model="Bewässerungsstation",
            configuration_url= f"http://{self._station_ip}"
        )

        _LOGGER.debug("###################FirmwareUpdate-Entität erstellt für %s", self.station_id)

    @property
    def installed_version(self):
        value = self.coordinator.data[self.station_id].get("current_version")
        return None if value in (None, "", "null") else value

    @property
    def latest_version(self):
        value = self.coordinator.data[self.station_id].get("latestVersion")
        return None if value in (None, "", "null") else value

    @property
    def available(self):
        return self.coordinator.last_update_success
    @property
    def progress(self) -> int | None:
        return self._update_data.get("progress")
    @property
    def update_percentage(self) -> int | None:
        try:
            return int(self._update_data.get("progress"))
        except (TypeError, ValueError):
            return None
    @property
    def update_progress(self) -> int | None:
        try:
            return int(self._update_data.get("progress"))
        except (TypeError, ValueError):
            return None        
    @property
    def release_summary(self) -> str | None:
        return "Bugfixes und Verbesserungen"
    @property
    def in_progress(self) -> bool:
        status = self._update_data.get("update_needed")
        if not status:
            _LOGGER.info("#####übreschrieben####")
            return False
        return self._in_progress

    @property
    def release_url(self) -> str | None:
        return "https://github.com/fabibo89/plantbot-OTA/releases"

    async def async_update(self):
        await self.coordinator.async_request_refresh()
        await self._fetch_update_status()


    async def async_install(self, version: str, backup: bool) -> None:
        """Installiere die neue Firmware auf dem Gerät."""
        self._in_progress = True
        self._progress = 0
        self._status = "installing"
        self.async_write_ha_state()

        _LOGGER.info("Starte Firmware-Update für %s", self.station_name)

        try:
            station = self.coordinator.data[self.station_id]
            ip = station.get("ip") or station.get("name")
            url = f"http://{ip}/HA/update"
            _LOGGER.info("ich sucher hier: %s", url)
            async with aiohttp.ClientSession() as session:
                async with session.post(url) as resp:
                    if resp.status != 200:
                        raise Exception(f"Update fehlgeschlagen: {resp.status}")
        except Exception as e:
            _LOGGER.error("Fehler beim Firmware-Update: %s", e)
            self._status = "failed"
            self._in_progress = False
            self.async_write_ha_state()
            return
        
        #Status regelmäßig abfragen (z. B. für 60 Sekunden alle 2s)
        for _ in range(60):  # 30 x 2 Sekunden = 60 Sekunden max
            try:
                await self._fetch_update_status()
                self._progress = 1
                self.async_write_ha_state()
                _LOGGER.warning("nummer: %s", _)
                _LOGGER.warning("status: %s", self._status)

                if self._status in ["done", "failed"]:
                    break
                if not self._update_data.get("update_needed"):
                    break

                await asyncio.sleep(3)
            except Exception as e:
                _LOGGER.warning("Fehler beim Statusabruf während Update: %s", e)

        self._in_progress = False
        self.async_write_ha_state()

    async def _fetch_update_status(self):
        import aiohttp
        url = f"http://{self._station_ip}/HA/update_status"
        _LOGGER.debug("Suche nach Status %s",self._station_ip)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        self._update_data = await resp.json()
                        _LOGGER.debug("Folgendes bekommen %s",self._update_data)
                        _LOGGER.debug("Update-Status: %s – Fortschritt: %s%%", self._update_data.get("status"), self._update_data.get("progress"))
                    else:
                        self._update_data = {}
        except Exception as e:
            _LOGGER.warning("Update-Status konnte nicht geladen werden: %s", e)
            self._update_data = {}
