DOMAIN = "plantbot"

# Fired when plantbot/{ip}/ack reports a finished watering job
# (job_id > 0 = Server-Job, job_id == 0 = HA Zeit/Volumen; Server- und Device-Modus).
EVENT_WATERING_FINISHED = "plantbot_watering_finished"

# Fired when plantbot/{ip}/alerts reports active/cleared station alerts.
EVENT_ALERT = "plantbot_alert"

ALERT_CODE_LABELS = {
    "water_level_low": "Wasserstand niedrig",
}


def station_device_identifiers(station_id: str) -> set[tuple[str, str]]:
    """HA device identifiers for a station.

    Coordinator keys are already ``station_<id>``. Historically entities also
    used ``station_<key>`` (double prefix). Register both so old/new devices
    merge and Device-Triggers bind to the visible station device.
    """
    sid = str(station_id)
    return {(DOMAIN, sid), (DOMAIN, f"station_{sid}")}

