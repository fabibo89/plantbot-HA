import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature, PERCENTAGE, UnitOfLength, UnitOfTime
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.const import UnitOfPressure
from homeassistant.helpers.entity import EntityCategory

_LOGGER = logging.getLogger(__name__)

from .const import DOMAIN, station_device_identifiers

# Env-Keys aus Sensoren.PlantBot (nicht als Stations-Top-Level-Entity)
ENV_SENSOR_KEYS = frozenset(
    {"temp", "hum", "pres", "water_level", "water_surface_cm", "alt"}
)

# --- PlantBot minimal validator ---
def _plantbot_value_is_valid(props, value):
    # Basic empty checks
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none")):
        return False
    # Try numeric
    num = None
    try:
        num = float(value)
    except (TypeError, ValueError):
        pass
    # Zero handling
    if props.get("ignore_zero", False) and num == 0.0:
        return False
    # Range handling
    rng = props.get("valid_range")
    if rng and isinstance(rng, (tuple, list)) and num is not None:
        try:
            lo, hi = rng
            if num < lo or num > hi:
                return False
        except Exception:
            pass
    return True

# Optional station metrics that should exist even before the first MQTT snapshot.
ALWAYS_CREATE_STATION_SENSORS = frozenset(
    {
        "flow",
        "lastVolume",
        "water_runtime",
        "watering_percent",
        "watering_remaining_ml",
        "watering_remaining_seconds",
        "alert_status",
        "jobs",
        "last_reset_reason",
        "runtime",
        "memory_usage",
        "current_version",
    }
)

# Nur im Server-Modus sinnvoll (Queue, Pflanzen-API, …)
SERVER_ONLY_STATION_SENSORS = frozenset({"jobs"})

SENSOR_TYPES = {
    "temp": {"name": "Temperatur", "unit": UnitOfTemperature.CELSIUS, "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "ignore_zero": True, 'valid_range': (-30.0, 60.0)},
    "hum": {"name": "Feuchtigkeit", "unit": PERCENTAGE, "device_class": SensorDeviceClass.HUMIDITY, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "ignore_zero": True, 'valid_range': (0.0, 100.0)},
    "pres": {"name": "Luftdruck", "unit": UnitOfPressure.HPA, "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "ignore_zero": True, "icon": "mdi:gauge", 'valid_range': (800.0, 1100.0)},
    "water_level": {"name": "Wasserstand", "unit": UnitOfLength.CENTIMETERS, "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:waves", 'valid_range': (0.0, 100.0)},
    "water_surface_cm": {"name": "Wasseroberfläche", "unit": UnitOfLength.CENTIMETERS, "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:arrow-expand-vertical", 'valid_range': (0.0, 200.0)},
    "jobs": {"name": "Jobs", "unit": "count", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:playlist-play", "ignore_zero": False},
    "flow": {"name": "Flow", "unit": None, "device_class": None, "state_class": SensorStateClass.TOTAL, "optional": True, "icon": "mdi:water-pump"},
    "lastVolume": {"name": "Volume", "unit": 'ml', "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:water", "entity_category": EntityCategory.DIAGNOSTIC},
    "watering_percent": {"name": "Gießfortschritt", "unit": PERCENTAGE, "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:water-percent", "ignore_zero": False, "valid_range": (0.0, 100.0)},
    "watering_remaining_ml": {"name": "Restvolumen", "unit": "ml", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:cup-water", "ignore_zero": False, "entity_category": EntityCategory.DIAGNOSTIC},
    "watering_remaining_seconds": {"name": "Restzeit", "unit": UnitOfTime.SECONDS, "device_class": SensorDeviceClass.DURATION, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:timer-outline", "ignore_zero": False, "entity_category": EntityCategory.DIAGNOSTIC},
    "alert_status": {"name": "Alert-Status", "unit": None, "device_class": None, "optional": True, "icon": "mdi:alert-circle-outline", "ignore_zero": False},
    "status": {"name": "Status", "unit": None, "device_class": None, "optional": False, "icon": "mdi:information"},
    "wifi": {"name": "WIFI", "unit": SIGNAL_STRENGTH_DECIBELS_MILLIWATT, "device_class": SensorDeviceClass.SIGNAL_STRENGTH, "state_class": SensorStateClass.MEASUREMENT, "optional": False, 'valid_range': (-100.0, -20.0), "entity_category": EntityCategory.DIAGNOSTIC},
    "runtime": {"name": "Runtime", "unit": UnitOfTime.SECONDS, "device_class": SensorDeviceClass.DURATION, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "entity_category": EntityCategory.DIAGNOSTIC},
    "water_runtime": {"name": "Wasser Runtime", "unit": UnitOfTime.SECONDS, "device_class": SensorDeviceClass.DURATION, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:timer-sand", "entity_category": EntityCategory.DIAGNOSTIC},
    "last_reset_reason": {"name": "Letzter Reset Grund", "unit": None, "device_class": None, "optional": True, "icon": "mdi:restart", "entity_category": EntityCategory.DIAGNOSTIC},
    "memory_usage": {"name": "Speicherauslastung", "unit": "B", "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "optional": True, "icon": "mdi:memory", "entity_category": EntityCategory.DIAGNOSTIC},
    "current_version": {"name": "Firmware Version", "unit": None, "device_class": None, "optional": True, "icon": "mdi:information", "entity_category": EntityCategory.DIAGNOSTIC},
}

DYNAMIC_SENSOR_TYPES = {
    "modbusSens_hum": {"name_template": "Bodenfeuchtigkeit MB {addr}", "name_server": "Bodenfeuchtigkeit", "unit": PERCENTAGE, "optional": True, "device_class": SensorDeviceClass.HUMIDITY, "state_class": SensorStateClass.MEASUREMENT, "valid_range": (5.0, 100.0)},
    "modbusSens_temp": {"name_template": "Bodentemperatur MB {addr}", "name_server": "Bodentemperatur", "unit": UnitOfTemperature.CELSIUS, "optional": True, "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "ignore_zero": False, "valid_range": (-10.0, 100.0)},
    "modbusSens_cond": {"name_template": "Bodenleitfähigkeit MB {addr}", "name_server": "Bodenleitfähigkeit", "unit": "µS/cm", "optional": True, "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:flash", "valid_range": (0.0, 5000.0)},
    "BTSensoren_temp": {"name_template": "Temperatur BT {mac}", "name_server": "Bodentemperatur", "unit": UnitOfTemperature.CELSIUS, "optional": True, "device_class": SensorDeviceClass.TEMPERATURE, "state_class": SensorStateClass.MEASUREMENT, "ignore_zero": False, "valid_range": (-10.0, 100.0)},
    "BTSensoren_hum": {"name_template": "Bodenfeuchtigkeit BT {mac}", "name_server": "Bodenfeuchtigkeit", "unit": PERCENTAGE, "optional": True, "device_class": SensorDeviceClass.HUMIDITY, "state_class": SensorStateClass.MEASUREMENT, "valid_range": (5.0, 100.0)},
    "BTSensoren_bat": {"name_template": "Batterie BT {mac}", "name_server": "Batterie", "unit": PERCENTAGE, "optional": True, "device_class": SensorDeviceClass.BATTERY, "state_class": SensorStateClass.MEASUREMENT, "valid_range": (0.0, 100.0)},
    "BTSensoren_con": {"name_template": "Bodenleitfähigkeit BT {mac}", "name_server": "Bodenleitfähigkeit", "unit": "µS/cm", "optional": True, "device_class": None, "state_class": SensorStateClass.MEASUREMENT, "icon": "mdi:flash", "valid_range": (0.0, 5000.0)},
    "BTSensoren_light": {"name_template": "Licht BT {mac}", "name_server": "Licht", "unit": "lx", "optional": True, "device_class": SensorDeviceClass.ILLUMINANCE, "state_class": SensorStateClass.MEASUREMENT, "valid_range": (0.0, 100000.0)}
}

_DYNAMIC_SECTION_CONFIGS = (
    {
        "section": "modbusSens",
        "prefix": "modbusSens",
        "sensor_types": ("hum", "temp", "cond"),
        "addr_format": "addr",
    },
    {
        "section": "BTSensoren",
        "prefix": "BTSensoren",
        "sensor_types": ("temp", "hum", "bat", "con", "light"),
        "addr_format": "mac",
    },
)


def _sensor_unique_id(station_id: str, key: str) -> str:
    return f"{DOMAIN}_{station_id}_{key}"


def _station_key(station_id: str) -> str:
    sid = str(station_id)
    return sid if sid.startswith("station_") else f"station_{sid}"


def _props_for_dynamic_sensor(
    coordinator, station_id: str, config: dict, addr_str: str, sensor_type: str
) -> dict:
    props = DYNAMIC_SENSOR_TYPES[f"{config['prefix']}_{sensor_type}"].copy()
    station_data = (coordinator.data or {}).get(_station_key(station_id), {})
    sensor_mapping = station_data.get("sensor_mapping", {})
    plant_name = sensor_mapping.get(addr_str)

    if plant_name:
        props["name"] = f"{props['name_server']} {plant_name}"
    elif config["addr_format"] == "mac":
        addr_short = addr_str[-5:] if len(addr_str) >= 5 else addr_str
        props["name"] = props["name_template"].format(mac=addr_short)
    else:
        props["name"] = props["name_template"].format(addr=addr_str)
    return props


def _discover_sensors(coordinator) -> list["PlantbotHASensor"]:
    """Baue alle Sensor-Entities aus dem aktuellen Coordinator-Stand."""
    entities: list[PlantbotHASensor] = []
    if not coordinator.data:
        return entities

    is_server = coordinator.connection_type == "server"

    for station_id, station in coordinator.data.items():
        if not isinstance(station, dict):
            continue
        station_name = station.get("name", f"Station {station_id}")
        sensoren = station.get("Sensoren") or {}

        # 1. Stations-Metriken (Top-Level)
        for key, props in SENSOR_TYPES.items():
            if key in ENV_SENSOR_KEYS:
                continue  # nur über env_* aus Sensoren.PlantBot
            if key in SERVER_ONLY_STATION_SENSORS and not is_server:
                continue
            value = station.get(key)
            if (
                not props["optional"]
                or key in station
                or key in ALWAYS_CREATE_STATION_SENSORS
            ):
                if (
                    props.get("optional", False)
                    and key not in ALWAYS_CREATE_STATION_SENSORS
                    and not _plantbot_value_is_valid(props, value)
                ):
                    continue
                entities.append(
                    PlantbotHASensor(coordinator, station_id, key, props, station_name)
                )

        # 2. Environment aus MQTT sensors (PlantBot)
        env = sensoren.get("PlantBot") or {}
        for key, value in env.items():
            props = SENSOR_TYPES.get(key)
            if not props:
                continue
            # Anlegen sobald der Key im Payload ist (Wert darf später kommen)
            entities.append(
                PlantbotHASensor(
                    coordinator, station_id, f"env_{key}", props, station_name
                )
            )

        # 3. Dynamisch: Modbus / BT
        for config in _DYNAMIC_SECTION_CONFIGS:
            section_data = sensoren.get(config["section"]) or {}
            if not isinstance(section_data, dict):
                continue
            for addr, values in section_data.items():
                if not isinstance(values, dict):
                    continue
                addr_str = str(addr)
                for sensor_type in config["sensor_types"]:
                    if sensor_type not in values:
                        continue
                    key = f"{config['prefix']}_{sensor_type}_{addr_str}"
                    props = _props_for_dynamic_sensor(
                        coordinator, station_id, config, addr_str, sensor_type
                    )
                    entities.append(
                        PlantbotHASensor(
                            coordinator, station_id, key, props, station_name
                        )
                    )

    return entities


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known_unique_ids: set[str] = set()

    def _add_new_entities() -> None:
        new_entities: list[PlantbotHASensor] = []
        for entity in _discover_sensors(coordinator):
            uid = entity.unique_id
            if uid in known_unique_ids:
                continue
            known_unique_ids.add(uid)
            new_entities.append(entity)
        if new_entities:
            _LOGGER.info(
                "PlantBot Sensor-Discovery: %d neue Entities", len(new_entities)
            )
            async_add_entities(new_entities)

    # Initial (auch wenn data noch leer → Listener holt Nachzügler)
    _add_new_entities()

    def _on_coordinator_update() -> None:
        _add_new_entities()

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))


class PlantbotHASensor(SensorEntity):
    def __init__(self, coordinator, station_id, key, props, station_name):
        self.coordinator = coordinator
        self.station_id = str(station_id)
        self.key = key
        self.station_name = station_name
        self._attr_native_unit_of_measurement = props["unit"]
        self._optional = props["optional"]
        self._attr_device_class = props["device_class"]
        self._attr_state_class = props.get("state_class")
        station_key = _station_key(self.station_id)
        self.station_ip = (coordinator.data or {}).get(station_key, {}).get("ip")
        self._attr_icon = props.get("icon")
        self._props = props
        self._attr_entity_category = props.get("entity_category")
        self._attr_name = props["name"]
        self._attr_unique_id = _sensor_unique_id(self.station_id, key)

    @property
    def extra_state_attributes(self):
        if self.key != "alert_status":
            return None
        station_data = (self.coordinator.data or {}).get(_station_key(self.station_id), {})
        return {
            "alerts": station_data.get("alerts") or {},
            "last_alert": station_data.get("last_alert"),
        }

    @property
    def native_value(self):
        if not self.available:
            return None

        station_key = _station_key(self.station_id)
        station_data = (self.coordinator.data or {}).get(station_key, {})
        sensoren = station_data.get("Sensoren", {})

        value = None
        sensor_configs = [
            {
                "prefixes": ["modbusSens_hum_", "modbusSens_temp_", "modbusSens_cond_"],
                "section": "modbusSens",
                "addr_extraction": lambda key, prefix: key.rsplit("_", 1)[-1],
                "sensor_type_extraction": lambda prefix: prefix.replace("modbusSens_", "").rstrip("_")
            },
            {
                "prefixes": ["BTSensoren_temp_", "BTSensoren_hum_", "BTSensoren_bat_", "BTSensoren_con_", "BTSensoren_light_"],
                "section": "BTSensoren",
                "addr_extraction": lambda key, prefix: key.replace(prefix, ""),
                "sensor_type_extraction": lambda prefix: prefix.replace("BTSensoren_", "").rstrip("_")
            }
        ]
        
        for config in sensor_configs:
            for prefix in config["prefixes"]:
                if self.key.startswith(prefix):
                    section_data = sensoren.get(config["section"], {})
                    addr = config["addr_extraction"](self.key, prefix)
                    sensor_type = config["sensor_type_extraction"](prefix)
                    value = section_data.get(addr, {}).get(sensor_type)
                    break
            if value is not None:
                break
        
        # Environment-Sensoren (PlantBot)
        if value is None and self.key.startswith("env_"):
            env_key = self.key.replace("env_", "")
            env = sensoren.get("PlantBot", {})
            value = env.get(env_key)
            if value is None:
                value = station_data.get(env_key)
        elif value is None:
            value = station_data.get(self.key)

        if self.key == "alert_status" and (value is None or value == ""):
            return "ok"

        # Validierung
        if not _plantbot_value_is_valid(self._props, value):
            return None

        # Spezielle Umrechnung für bestimmte Sensoren
        if self._props.get("convert_from_seconds", False) and value is not None:
            try:
                num = float(value) / 60.0
                return round(num, 1)
            except (TypeError, ValueError):
                pass

        # Zahl zurückgeben, ansonsten den Rohwert
        if isinstance(value, int):
            return value
        try:
            num = float(value)
            return int(num) if num.is_integer() else num
        except (TypeError, ValueError):
            return value

    @property
    def available(self):
        data = self.coordinator.data or {}
        station_key = _station_key(self.station_id)
        # unique_id/station_id nutzen denselben Coordinator-Key
        station_data = data.get(self.station_id) or data.get(station_key)
        if not station_data:
            return False
        return bool(station_data.get("available", True))

    @property
    def device_info(self):
        info = {
            "identifiers": station_device_identifiers(self.station_id),
            "name": self.station_name,
            "manufacturer": "PlantBot",
            "model": "Bewässerungsstation",
        }
        if self.station_ip:
            info["configuration_url"] = f"http://{self.station_ip}"
        return info

    async def async_update(self):
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
