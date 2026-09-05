"""Device triggers for PlantBot (Automation UI).

HA lädt dieses Modul lazy beim Öffnen der Geräte-Trigger-Liste.
Importfehler werden von HA still geschluckt – daher möglichst wenig Imports.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_ALERT, EVENT_WATERING_FINISHED

_LOGGER = logging.getLogger(__name__)

TriggerActionType = Any
TriggerInfo = Any

TRIGGER_TYPE_WATERING_FINISHED = "watering_finished"
TRIGGER_TYPE_WATERING_COMPLETED = "watering_completed"
TRIGGER_TYPE_WATERING_FAILED = "watering_failed"
TRIGGER_TYPE_WATERING_CANCELLED = "watering_cancelled"
TRIGGER_TYPE_ALERT_ACTIVE = "alert_active"
TRIGGER_TYPE_ALERT_CLEARED = "alert_cleared"
TRIGGER_TYPE_WATER_LEVEL_LOW = "water_level_low"
TRIGGER_TYPE_WATER_LEVEL_LOW_CLEARED = "water_level_low_cleared"

TRIGGER_TYPES = {
    TRIGGER_TYPE_WATERING_FINISHED,
    TRIGGER_TYPE_WATERING_COMPLETED,
    TRIGGER_TYPE_WATERING_FAILED,
    TRIGGER_TYPE_WATERING_CANCELLED,
    TRIGGER_TYPE_ALERT_ACTIVE,
    TRIGGER_TYPE_ALERT_CLEARED,
    TRIGGER_TYPE_WATER_LEVEL_LOW,
    TRIGGER_TYPE_WATER_LEVEL_LOW_CLEARED,
}

WATERING_TRIGGER_STATUS = {
    TRIGGER_TYPE_WATERING_COMPLETED: "completed",
    TRIGGER_TYPE_WATERING_FAILED: "failed",
    TRIGGER_TYPE_WATERING_CANCELLED: "cancelled",
}

ALERT_TRIGGER_DATA = {
    TRIGGER_TYPE_ALERT_ACTIVE: {"state": "active"},
    TRIGGER_TYPE_ALERT_CLEARED: {"state": "cleared"},
    TRIGGER_TYPE_WATER_LEVEL_LOW: {"code": "water_level_low", "state": "active"},
    TRIGGER_TYPE_WATER_LEVEL_LOW_CLEARED: {
        "code": "water_level_low",
        "state": "cleared",
    },
}

TRIGGER_SCHEMA = cv.TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_PLATFORM): "device",
        vol.Required(CONF_DOMAIN): str,
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Remove("metadata"): dict,
    }
)

_LOGGER.info("PlantBot device_trigger-Modul geladen")


def _device_belongs_to_plantbot(hass: HomeAssistant, device_id: str) -> bool:
    """True if device is linked to the PlantBot config entry or identifiers."""
    device = dr.async_get(hass).async_get(device_id)
    if not device:
        return False

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN:
            return True

    if any(ident_domain == DOMAIN for ident_domain, _ident in device.identifiers):
        return True

    return False


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate trigger config."""
    return TRIGGER_SCHEMA(config)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List device triggers for PlantBot stations."""
    if not _device_belongs_to_plantbot(hass, device_id):
        _LOGGER.info(
            "device_trigger: Gerät %s gehört nicht zu PlantBot – keine Trigger",
            device_id,
        )
        return []

    triggers = [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in (
            TRIGGER_TYPE_WATERING_FINISHED,
            TRIGGER_TYPE_WATERING_COMPLETED,
            TRIGGER_TYPE_WATERING_FAILED,
            TRIGGER_TYPE_WATERING_CANCELLED,
            TRIGGER_TYPE_ALERT_ACTIVE,
            TRIGGER_TYPE_ALERT_CLEARED,
            TRIGGER_TYPE_WATER_LEVEL_LOW,
            TRIGGER_TYPE_WATER_LEVEL_LOW_CLEARED,
        )
    ]
    _LOGGER.info(
        "device_trigger: %d Trigger für Gerät %s", len(triggers), device_id
    )
    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger to PlantBot events."""
    from homeassistant.components.homeassistant.triggers import event as event_trigger

    trigger_type = config[CONF_TYPE]
    event_data: dict[str, Any] = {CONF_DEVICE_ID: config[CONF_DEVICE_ID]}

    if trigger_type in ALERT_TRIGGER_DATA:
        event_type = EVENT_ALERT
        event_data.update(ALERT_TRIGGER_DATA[trigger_type])
    else:
        event_type = EVENT_WATERING_FINISHED
        status = WATERING_TRIGGER_STATUS.get(trigger_type)
        if status:
            event_data["status"] = status

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: event_type,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
