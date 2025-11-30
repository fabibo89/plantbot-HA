import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from .coordinator import PlantbotHACoordinator
from .valve import ENTITIES

import asyncio

PLATFORMS = ["valve", "sensor", "update", "button"]
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up PlantBot HA from a config entry."""
    try:
        _LOGGER.info("Starte Setup für PlantBot HA Integration, Entry ID: %s", entry.entry_id)
        
        # Validiere Config-Daten
        if not entry.data:
            _LOGGER.error("Keine Config-Daten in Entry gefunden")
            return False
        
        config_data = entry.data
        connection_type = config_data.get("connection_type")
        
        if not connection_type:
            _LOGGER.error("Kein connection_type in Config-Daten gefunden")
            return False
        
        if connection_type == "server":
            if not config_data.get("server_url"):
                _LOGGER.error("Kein server_url in Config-Daten gefunden")
                return False
        elif connection_type == "device":
            if not config_data.get("device_ip"):
                _LOGGER.error("Kein device_ip in Config-Daten gefunden")
                return False
        else:
            _LOGGER.error("Ungültiger connection_type: %s", connection_type)
            return False
        
        _LOGGER.debug("Config-Daten validiert: connection_type=%s", connection_type)
        
        coordinator = PlantbotHACoordinator(hass, config_data, entry.entry_id)
        _LOGGER.debug("Coordinator erstellt, starte ersten Refresh")
        
        # Speichere Coordinator zuerst, damit Platforms darauf zugreifen können
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        _LOGGER.debug("Coordinator zu hass.data hinzugefügt")
        
        # Starte ersten Refresh mit Timeout, aber blockiere nicht das Setup
        try:
            await asyncio.wait_for(coordinator.async_config_entry_first_refresh(), timeout=30.0)
            _LOGGER.debug("Erster Refresh erfolgreich")
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout beim ersten Refresh (wird bei nächstem Update erneut versucht)")
            # Erstelle trotzdem einen Eintrag, damit die Integration geladen wird
        except asyncio.CancelledError:
            _LOGGER.warning("Erster Refresh abgebrochen (wird bei nächstem Update erneut versucht)")
        except Exception as refresh_error:
            _LOGGER.warning("Fehler beim ersten Refresh (wird bei nächstem Update erneut versucht): %s", refresh_error)
            # Erstelle trotzdem einen Eintrag, damit die Integration geladen wird
            # Der Coordinator wird bei den nächsten Updates versuchen, die Daten zu holen
        
        # Lade Platforms - auch wenn Refresh fehlgeschlagen ist
        # Versuche jede Platform einzeln zu laden, um zu sehen welche fehlschlägt
        platforms_to_load = []
        for platform in PLATFORMS:
            # Prüfe ob button-Platform verfügbar ist
            if platform == "button":
                try:
                    # Versuche button-Modul zu importieren
                    import importlib
                    importlib.import_module(f"custom_components.{DOMAIN}.button")
                    platforms_to_load.append(platform)
                    _LOGGER.debug("Button-Platform verfügbar, wird geladen")
                except (ModuleNotFoundError, ImportError) as e:
                    _LOGGER.warning("Button-Platform nicht verfügbar, überspringe sie: %s", e)
                    continue
            else:
                platforms_to_load.append(platform)
        
        if platforms_to_load:
            try:
                await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)
                _LOGGER.info("Platforms erfolgreich geladen: %s", platforms_to_load)
            except asyncio.CancelledError:
                _LOGGER.error("Platform-Setup wurde abgebrochen")
                raise
            except Exception as platform_error:
                _LOGGER.error("Fehler beim Laden der Platforms: %s", platform_error, exc_info=True)
                raise
        else:
            _LOGGER.warning("Keine Platforms zum Laden verfügbar")
        _LOGGER.debug("Platforms geladen")

        # Registriere Services für Ventilsteuerung
        # Die services.yaml wird automatisch von Home Assistant geladen
        hass.services.async_register(DOMAIN, "open_for_seconds", handle_open_for_seconds)
        hass.services.async_register(DOMAIN, "open_for_volume", handle_open_for_volume)
        _LOGGER.info("PlantBot HA Integration erfolgreich geladen")

        return True
    except Exception as e:
        _LOGGER.exception("Kritischer Fehler beim Laden der PlantBot HA Integration: %s", e)
        return False

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].get(entry.entry_id)
        if coordinator:
            # Stoppe MQTT-Subscribe
            if coordinator._mqtt_subscribe_task:
                coordinator._mqtt_subscribe_task.cancel()
                try:
                    await coordinator._mqtt_subscribe_task
                except asyncio.CancelledError:
                    pass
            if coordinator._mqtt_subscribe_client:
                try:
                    await coordinator._mqtt_subscribe_client.disconnect()
                except Exception:
                    pass
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

async def handle_open_for_seconds(call):
    """Service-Handler: Öffne Ventil für eine bestimmte Dauer."""
    valve_id = call.data.get("valve")
    duration = call.data.get("duration")
    
    if not valve_id or not duration:
        _LOGGER.error("valve und duration müssen angegeben werden")
        return
    
    entity = ENTITIES.get(valve_id)
    if entity:
        _LOGGER.debug("Komponente gefunden %s", entity.valve_id)
        await entity.open_for_seconds(duration)
    else:
        _LOGGER.error("Komponente nicht gefunden %s", valve_id)

async def handle_open_for_volume(call):
    """Service-Handler: Öffne Ventil für ein bestimmtes Volumen."""
    valve_id = call.data.get("valve")
    volume = call.data.get("volume")
    
    if not valve_id or not volume:
        _LOGGER.error("valve und volume müssen angegeben werden")
        return
    
    entity = ENTITIES.get(valve_id)
    if entity:
        _LOGGER.debug("Komponente gefunden %s", entity.valve_id)
        await entity.open_for_volume(volume)
    else:
        _LOGGER.error("Komponente nicht gefunden %s", valve_id)

