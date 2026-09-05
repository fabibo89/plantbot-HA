from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
import aiohttp
import asyncio
import time
from typing import Optional
import re

from .const import DOMAIN, station_device_identifiers
import logging

_LOGGER = logging.getLogger(__name__)

# Konstanten für Update-Timeout
UPDATE_STATUS_TIMEOUT = 300  # 5 Minuten max für Update
UPDATE_STATUS_INTERVAL = 2  # Status alle 2 Sekunden abfragen
UPDATE_START_RETRIES = 3  # 3 Versuche Update zu starten
UPDATE_START_RETRY_DELAY = 2  # 2 Sekunden zwischen Versuchen
# Nach OTA/Reboot: länger warten, statt UI auf "Aktualisieren" zurückzusetzen
UPDATE_REBOOT_WAIT_SECONDS = 180  # max. Offline-/Reboot-Wartezeit nach OTA-Aktivität
UPDATE_REBOOT_PROGRESS_HOLD = 95  # Fortschritt einfrieren während Reboot

# GitHub Releases (for release notes shown in UI)
GITHUB_OWNER = "fabibo89"
GITHUB_REPO = "plantbot-OTA"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
RELEASE_NOTES_MAX_CHARS = 6000
RELEASE_SUMMARY_MAX_CHARS = 255

# Supported formats inside GitHub release body:
# - <!-- HA_SUMMARY: ... -->
# - HA_SUMMARY: ...
# - ## HA Summary\n<text...>
_HA_SUMMARY_COMMENT_RE = re.compile(r"<!--\s*HA_SUMMARY\s*:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
_HA_SUMMARY_LINE_RE = re.compile(r"^\s*HA_SUMMARY\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_HA_SUMMARY_HEADING_RE = re.compile(r"^\s*##\s*HA\s+Summary\s*$", re.IGNORECASE | re.MULTILINE)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    
    # Prüfe ob coordinator.data vorhanden ist
    if not coordinator.data:
        _LOGGER.warning("Coordinator hat noch keine Daten, warte auf ersten Update")
        async_add_entities([])
        return

    for station_id, station in coordinator.data.items():
        # Hole IP-Adresse
        station_ip = station.get("ip") or station.get("ip_address")
        station_name = station.get("name", f"Station {station_id}")
        
        if not station_ip:
            _LOGGER.warning("Keine IP-Adresse für Station %s gefunden, überspringe Update-Entity", station_id)
            continue
        
        _LOGGER.debug("Erstelle Update-Entity für Station %s (%s)", station_name, station_ip)
        entities.append(PlantbotFirmwareUpdate(coordinator, station_id, station_name, station_ip))
    
    async_add_entities(entities)

class PlantbotFirmwareUpdate(UpdateEntity):

    def __init__(self, coordinator, station_id, station_name, station_ip):
        self.coordinator = coordinator
        self.station_id = str(station_id)
        self.station_name = station_name
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._station_ip = station_ip
        self._attr_unique_id = f"{DOMAIN}_update_{self.station_id}"
        self._attr_name = f"{station_name} Firmware Update"
        self._attr_title = f"{station_name} Firmware"
        self._attr_in_progress = False
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS | UpdateEntityFeature.RELEASE_NOTES
        )
        self._progress = None
        self._in_progress = False
        self._update_data = {}
        self._update_start_time = None
        self._last_status_check = None
        self._consecutive_errors = 0
        self._max_consecutive_errors = 5
        self._release_notes_cache: dict[str, str] = {}  # tag -> notes
        self._release_notes: str | None = None
        self._release_notes_tag: str | None = None
        self._release_summary_cache: dict[str, str] = {}  # tag -> 255-char summary
        self._release_summary: str | None = None
        self._release_summary_tag: str | None = None
        self._release_notes_task: asyncio.Task | None = None
        self._release_notes_task_tag: str | None = None

        self._attr_device_info = DeviceInfo(
            identifiers=station_device_identifiers(self.station_id),
            name=self.station_name,
            manufacturer="PlantBot",
            model="Bewässerungsstation",
            configuration_url=f"http://{self._station_ip}"
        )

        _LOGGER.debug("FirmwareUpdate-Entität erstellt für %s", self.station_id)

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener and prefetch release notes."""
        self.coordinator.async_add_listener(self._handle_coordinator_update)
        # Attempt to prefetch immediately after entity is added.
        self._schedule_release_notes_prefetch()
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel background tasks when entity is removed."""
        if self._release_notes_task and not self._release_notes_task.done():
            self._release_notes_task.cancel()
        await super().async_will_remove_from_hass()

    def _handle_coordinator_update(self) -> None:
        """Called when coordinator has new data."""
        self._schedule_release_notes_prefetch()
        self.async_write_ha_state()

    def _schedule_release_notes_prefetch(self) -> None:
        """Fetch GitHub release notes in background once update is needed."""
        try:
            update_needed = self._get_update_needed()
        except Exception:
            update_needed = False

        tag = self.latest_version
        if not update_needed or not tag:
            return

        tag = str(tag).strip()
        if not tag:
            return

        # Already have notes for this version.
        if self._release_notes_tag == tag and self._release_notes:
            return

        # Avoid duplicate in-flight fetches for same tag.
        if (
            self._release_notes_task
            and not self._release_notes_task.done()
            and self._release_notes_task_tag == tag
        ):
            return

        if not getattr(self, "hass", None):
            return

        async def _runner() -> None:
            await self._fetch_release_notes()
            self.async_write_ha_state()

        self._release_notes_task_tag = tag
        self._release_notes_task = self.hass.async_create_task(_runner())

    @property
    def installed_version(self):
        if not self.coordinator.data or self.station_id not in self.coordinator.data:
            return None
        
        station_data = self.coordinator.data[self.station_id]
        # Versuche verschiedene mögliche Felder für Firmware-Version
        firmware = station_data.get("Firmware", {})
        value = (
            firmware.get("current_version") or 
            station_data.get("current_version") or
            station_data.get("firmware_version") or
            station_data.get("version")
        )
        return None if value in (None, "", "null") else str(value)

    @property
    def latest_version(self):
        if not self.coordinator.data or self.station_id not in self.coordinator.data:
            return None
        
        station_data = self.coordinator.data[self.station_id]
        # Versuche verschiedene mögliche Felder für neueste Firmware-Version
        firmware = station_data.get("Firmware", {})
        value = (
            firmware.get("latest_version") or 
            station_data.get("latest_version") or
            station_data.get("latestVersion")
        )
        return None if value in (None, "", "null") else str(value)

    @property
    def available(self):
        # Während OTA/Reboot Entity verfügbar halten, sonst springt die HA-UI
        # zurück auf "Aktualisieren", sobald die Station kurz offline ist.
        if self._in_progress:
            return True
        if not self.coordinator.data or self.station_id not in self.coordinator.data:
            return False
        station_data = self.coordinator.data[self.station_id]
        return bool(station_data.get("available", True))

    def _get_update_needed(self):
        """Prüft, ob ein Update benötigt wird."""
        if not self.coordinator.data or self.station_id not in self.coordinator.data:
            return False
        
        station_data = self.coordinator.data[self.station_id]
        firmware = station_data.get("Firmware", {})
        update_needed = firmware.get("update_needed") or station_data.get("update_needed")
        
        # Fallback: Versionsnummern vergleichen
        if update_needed is None:
            installed = self.installed_version
            latest = self.latest_version
            if installed and latest:
                return installed != latest
        
        return bool(update_needed)

    @property
    def progress(self) -> int | None:
        return self._update_data.get("progress")

    @property
    def update_percentage(self) -> int | None:
        try:
            return int(self._update_data.get("progress", 0))
        except (TypeError, ValueError):
            return None

    @property
    def update_progress(self) -> int | None:
        try:
            return int(self._update_data.get("progress", 0))
        except (TypeError, ValueError):
            return None

    @property
    def release_summary(self) -> str | None:
        # Frontend expects a short excerpt (max 255 chars).
        return self._release_summary or "Bugfixes und Verbesserungen"

    async def async_release_notes(self) -> str | None:
        """Return full release notes shown under 'Versionshinweise lesen'."""
        await self._fetch_release_notes()
        return self._release_notes

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    @property
    def release_url(self) -> str | None:
        tag = self.latest_version
        if tag:
            return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tag/{tag}"
        return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

    async def async_update(self):
        """Aktualisiere Update-Status."""
        await self.coordinator.async_request_refresh()
        await self._fetch_update_status()
        await self._fetch_release_notes()

    async def async_install(self, version: str, backup: bool) -> None:
        """Installiere die neue Firmware auf dem Gerät mit Retry-Logik."""
        _LOGGER.info("Starte Firmware-Update für %s (Version: %s)", self.station_name, version or "latest")
        # Make sure release notes are populated for UI before starting.
        await self._fetch_release_notes()
        
        # Prüfe ob bereits ein Update läuft
        if self._in_progress:
            _LOGGER.warning("Update bereits in Progress für %s", self.station_name)
            return
        
        # Prüfe ob Gerät erreichbar ist
        if not await self._check_device_reachable():
            _LOGGER.error("Gerät %s ist nicht erreichbar, Update abgebrochen", self._station_ip)
            return
        
        self._in_progress = True
        self._attr_in_progress = True
        self._progress = 0
        self._update_start_time = time.time()
        self._consecutive_errors = 0
        self.async_write_ha_state()

        # Versuche Update zu starten (mit Retry)
        update_started = False
        for attempt in range(UPDATE_START_RETRIES):
            try:
                if await self._start_update(version):
                    update_started = True
                    _LOGGER.info("Update-Befehl erfolgreich gesendet (Versuch %d/%d)", attempt + 1, UPDATE_START_RETRIES)
                    break
            except Exception as e:
                _LOGGER.warning("Fehler beim Starten des Updates (Versuch %d/%d): %s", 
                               attempt + 1, UPDATE_START_RETRIES, e)
                if attempt < UPDATE_START_RETRIES - 1:
                    await asyncio.sleep(UPDATE_START_RETRY_DELAY)
        
        if not update_started:
            _LOGGER.error("Update konnte nicht gestartet werden nach %d Versuchen", UPDATE_START_RETRIES)
            self._set_in_progress(False)
            return
        
        # Warte kurz und prüfe ob Update wirklich gestartet wurde
        await asyncio.sleep(2)
        await self._fetch_update_status()
        status = self._update_data.get("status", "").lower()
        
        if status not in ["installing", "started", "downloading"]:
            _LOGGER.warning("Update-Status nach Start: %s (erwartet: installing/started)", status)
            # Prüfe nochmal nach kurzer Wartezeit
            await asyncio.sleep(3)
            await self._fetch_update_status()
            status = self._update_data.get("status", "").lower()
            if status not in ["installing", "started", "downloading"]:
                _LOGGER.error("Update scheint nicht gestartet zu sein (Status: %s)", status)
                self._set_in_progress(False)
                return
        
        # Überwache Update-Status (inkl. Reboot – async_install blockiert bis dahin)
        await self._monitor_update_progress()
        
        self._set_in_progress(False)
        _LOGGER.info("Update-Prozess beendet für %s", self.station_name)

    def _set_in_progress(self, value: bool) -> None:
        """in_progress für Property und _attr_ synchron halten (HA-Frontend)."""
        self._in_progress = value
        self._attr_in_progress = value
        self.async_write_ha_state()

    async def _check_device_reachable(self) -> bool:
        """Prüfe Erreichbarkeit über MQTT-Availability (kein HTTP /status)."""
        station = (self.coordinator.data or {}).get(self.station_id) or {}
        if station.get("available", False):
            return True
        if not self._station_ip:
            return False
        try:
            await self.coordinator.request_status_snapshot(self._station_ip)
            await asyncio.sleep(2)
            station = (self.coordinator.data or {}).get(self.station_id) or {}
            return bool(station.get("available", False))
        except Exception as e:
            _LOGGER.debug("Gerät per MQTT nicht erreichbar: %s", e)
            return False

    async def _fetch_release_notes(self) -> None:
        """Fetch release notes (GitHub release body) for latest_version and cache them."""
        tag = self.latest_version
        if not tag:
            self._release_notes = None
            self._release_notes_tag = None
            self._release_summary = None
            self._release_summary_tag = None
            return

        # Normalize tag to avoid invisible whitespace causing 404s.
        raw_tag = tag
        tag = str(tag).strip()
        if raw_tag != tag:
            _LOGGER.debug("Release notes tag normalized: raw=%r -> tag=%r", raw_tag, tag)

        # Already loaded for this tag.
        if (
            self._release_notes_tag == tag
            and self._release_notes
            and self._release_summary_tag == tag
            and self._release_summary
        ):
            return

        # Cache hit.
        cached = self._release_notes_cache.get(tag)
        cached_summary = self._release_summary_cache.get(tag)
        if cached is not None and cached_summary is not None:
            self._release_notes = cached
            self._release_notes_tag = tag
            self._release_summary = cached_summary
            self._release_summary_tag = tag
            _LOGGER.debug(
                "Release notes cache hit for tag=%r (notes_len=%d summary_len=%d)",
                tag,
                len(cached),
                len(cached_summary),
            )
            return

        url = f"{GITHUB_API_BASE}/releases/tags/{tag}"
        timeout = aiohttp.ClientTimeout(total=8, connect=4)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "homeassistant-plantbot",
        }

        try:
            _LOGGER.debug("Fetching release notes from GitHub: tag=%r url=%s", tag, url)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        text_snippet = ""
                        try:
                            text_snippet = (await resp.text())[:300]
                        except Exception:
                            pass
                        _LOGGER.warning(
                            "Konnte Release Notes nicht laden (tag=%r): HTTP %s body=%r",
                            tag,
                            resp.status,
                            text_snippet,
                        )
                        self._release_notes = None
                        self._release_notes_tag = tag
                        return
                    data = await resp.json()
                    body = (data.get("body") or "").strip()
                    if not body:
                        _LOGGER.warning("GitHub Release Notes leer (tag=%r).", tag)
                        self._release_notes = None
                        self._release_notes_tag = tag
                        self._release_summary = None
                        self._release_summary_tag = tag
                        return

                    def _normalize_summary(text: str) -> str:
                        # Single-line, collapsed whitespace, hard-capped to 255 chars.
                        text = " ".join((text or "").strip().split())
                        if len(text) > RELEASE_SUMMARY_MAX_CHARS:
                            text = text[:RELEASE_SUMMARY_MAX_CHARS].rstrip()
                        return text

                    def _extract_summary(md: str) -> str | None:
                        m = _HA_SUMMARY_COMMENT_RE.search(md)
                        if m:
                            return _normalize_summary(m.group(1))
                        m = _HA_SUMMARY_LINE_RE.search(md)
                        if m:
                            return _normalize_summary(m.group(1))
                        m = _HA_SUMMARY_HEADING_RE.search(md)
                        if m:
                            after = md[m.end():]
                            for line in after.splitlines():
                                if line.strip():
                                    return _normalize_summary(line)
                        for line in md.splitlines():
                            if line.strip():
                                return _normalize_summary(line)
                        return None

                    summary = _extract_summary(body) or "Bugfixes und Verbesserungen"

                    # Truncate notes to keep UI responsive.
                    notes = body
                    if len(notes) > RELEASE_NOTES_MAX_CHARS:
                        notes = notes[:RELEASE_NOTES_MAX_CHARS].rstrip() + "\n\n…"
                    self._release_notes_cache[tag] = notes
                    self._release_notes = notes
                    self._release_notes_tag = tag
                    self._release_summary_cache[tag] = summary
                    self._release_summary = summary
                    self._release_summary_tag = tag
                    _LOGGER.info(
                        "GitHub Release Notes geladen: tag=%r notes_len=%d summary_len=%d",
                        tag,
                        len(notes),
                        len(summary),
                    )
        except Exception as e:
            _LOGGER.exception("Fehler beim Laden der Release Notes (tag=%r url=%s): %s", tag, url, e)
            # Keep old notes if any; otherwise leave None.
            if self._release_notes_tag != tag:
                self._release_notes = None
                self._release_notes_tag = tag
                self._release_summary = None
                self._release_summary_tag = tag

    async def _start_update(self, version: Optional[str] = None) -> bool:
        """Starte Update mit Retry-Logik."""
        url = f"http://{self._station_ip}/Github_update"
        timeout = aiohttp.ClientTimeout(total=30, connect=5)
        
        params = {}
        if version:
            params["version"] = version
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, params=params) as resp:
                    if resp.status == 200:
                        # Validiere Antwort
                        try:
                            data = await resp.json()
                            response_status = data.get("status", "").lower()
                            if response_status in ["started", "ok", "accepted"]:
                                _LOGGER.debug("Update-Start bestätigt: %s", data)
                                return True
                            else:
                                _LOGGER.warning("Unerwarteter Update-Status in Antwort: %s", response_status)
                                return False
                        except Exception:
                            # Wenn keine JSON-Antwort, aber 200 OK, akzeptieren wir es
                            text = await resp.text()
                            _LOGGER.debug("Update-Start-Antwort (kein JSON): %s", text[:100])
                            return True
                    else:
                        _LOGGER.error("Update-Start fehlgeschlagen: HTTP %s", resp.status)
                        return False
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout beim Starten des Updates")
            return False
        except Exception as e:
            _LOGGER.error("Fehler beim Starten des Updates: %s", e)
            return False

    def _hold_reboot_progress(self) -> None:
        """UI während Reboot/Offline bei installierend halten (kein Sprung zurück)."""
        try:
            current = int(self._update_data.get("progress", self._progress or 0) or 0)
        except (TypeError, ValueError):
            current = int(self._progress or 0)
        hold = max(current, UPDATE_REBOOT_PROGRESS_HOLD)
        if hold >= 100:
            hold = UPDATE_REBOOT_PROGRESS_HOLD
        self._progress = hold
        self._update_data = {
            **(self._update_data or {}),
            "status": "rebooting",
            "progress": hold,
        }
        self.async_write_ha_state()

    async def _monitor_update_progress(self):
        """Überwache Update-Fortschritt mit robustem Error-Handling."""
        max_duration = UPDATE_STATUS_TIMEOUT
        check_interval = UPDATE_STATUS_INTERVAL
        last_successful_check = time.time()
        offline_since: float | None = None
        saw_ota_activity = False
        saw_offline = False
        target_version = self.latest_version

        while True:
            elapsed = time.time() - self._update_start_time
            
            # Timeout prüfen (nach OTA etwas mehr Spielraum für Reboot)
            effective_timeout = max_duration
            if saw_ota_activity or saw_offline:
                effective_timeout = max(max_duration, UPDATE_REBOOT_WAIT_SECONDS + 60)
            if elapsed > effective_timeout:
                _LOGGER.error("Update-Timeout nach %d Sekunden", int(elapsed))
                break
            
            try:
                await self._fetch_update_status()
                self._consecutive_errors = 0  # Reset bei Erfolg
                last_successful_check = time.time()
                
                progress = self._update_data.get("progress", 0)
                status = self._update_data.get("status", "").lower()
                try:
                    progress_int = int(progress or 0)
                except (TypeError, ValueError):
                    progress_int = 0

                if status in ("installing", "started", "downloading", "rebooting") or progress_int > 0:
                    saw_ota_activity = True

                station = (self.coordinator.data or {}).get(self.station_id) or {}
                station_online = bool(station.get("available", True))
                if not station_online:
                    saw_offline = True
                    if offline_since is None:
                        offline_since = time.time()
                    # Reboot-Lücke: Progress einfrieren, in_progress bleibt aktiv
                    self._hold_reboot_progress()
                else:
                    was_offline = offline_since is not None or saw_offline
                    offline_since = None
                    # Nach Reboot oft kein / leerer MQTT-Update-Status → Progress nicht auf 0 zurücksetzen
                    if was_offline and (
                        progress_int < UPDATE_REBOOT_PROGRESS_HOLD
                        or status in ("", "idle", "unknown", "rebooting")
                    ):
                        self._hold_reboot_progress()
                    else:
                        self._progress = progress_int
                        self.async_write_ha_state()
                
                _LOGGER.debug("Update-Status: %s, Fortschritt: %d%%, Elapsed: %ds", 
                             status, progress_int, int(elapsed))

                # OTA-Topic "done" = Flash fertig, Gerät rebootet danach noch.
                # UI darf hier NICHT auf "Update verfügbar" zurückfallen.
                if status in ["done", "complete", "success"]:
                    saw_ota_activity = True
                    self._progress = 100
                    self._update_data = {
                        **(self._update_data or {}),
                        "status": "rebooting",
                        "progress": 100,
                    }
                    self.async_write_ha_state()
                    _LOGGER.info(
                        "OTA-Flash meldet done – warte auf Post-Reboot (MQTT update_needed=false)"
                    )
                    # nicht breaken; unten auf Post-Reboot prüfen
                
                if status in ["failed", "error"]:
                    error_msg = self._update_data.get("error", "Unbekannter Fehler")
                    _LOGGER.error("Update fehlgeschlagen: %s", error_msg)
                    break

                # Erst nach Reboot: MQTT status mit update_needed=false → Erfolg
                if self._post_reboot_update_success(
                    target_version=target_version,
                    saw_ota_activity=saw_ota_activity,
                    saw_offline=saw_offline,
                    elapsed=elapsed,
                ):
                    self._progress = 100
                    self._update_data = {
                        **(self._update_data or {}),
                        "status": "done",
                        "progress": 100,
                    }
                    self.async_write_ha_state()
                    _LOGGER.info(
                        "Update erfolgreich (Post-Reboot MQTT update_needed=false) nach %d Sekunden",
                        int(elapsed),
                    )
                    break
                
                # Prüfe ob Update noch läuft
                if status not in ["installing", "started", "downloading", "idle", "rebooting", "done", "complete", "success", ""]:
                    _LOGGER.warning("Unbekannter Update-Status: %s", status)
                
                await asyncio.sleep(check_interval)
                
            except Exception as e:
                self._consecutive_errors += 1
                saw_offline = True
                if offline_since is None:
                    offline_since = time.time()
                self._hold_reboot_progress()
                _LOGGER.warning(
                    "Fehler beim Statusabruf (Fehler %d): %s – warte auf Reboot/Online",
                    self._consecutive_errors,
                    e,
                )

                # Erfolg schon erkennbar?
                if self._post_reboot_update_success(
                    target_version=target_version,
                    saw_ota_activity=True,
                    saw_offline=True,
                    elapsed=elapsed,
                ):
                    self._progress = 100
                    self._update_data = {
                        **(self._update_data or {}),
                        "status": "done",
                        "progress": 100,
                    }
                    self.async_write_ha_state()
                    _LOGGER.info("Update erfolgreich nach Verbindungsabbrüchen (Post-Reboot)")
                    break

                # Nach OTA-Aktivität: Offline bis UPDATE_REBOOT_WAIT_SECONDS aushalten
                offline_for = time.time() - (offline_since or last_successful_check)
                if saw_ota_activity and offline_for < UPDATE_REBOOT_WAIT_SECONDS:
                    wait_time = min(check_interval * (2 ** min(self._consecutive_errors - 1, 3)), 10)
                    await asyncio.sleep(wait_time)
                    continue

                # Ohne OTA-Aktivität oder Reboot-Wartezeit erschöpft
                if offline_for >= UPDATE_REBOOT_WAIT_SECONDS or (
                    not saw_ota_activity and self._consecutive_errors >= self._max_consecutive_errors
                ):
                    _LOGGER.error(
                        "Update-Überwachung abgebrochen (offline %.0fs, ota_activity=%s)",
                        offline_for,
                        saw_ota_activity,
                    )
                    break
                
                wait_time = min(check_interval * (2 ** (self._consecutive_errors - 1)), 10)
                await asyncio.sleep(wait_time)

    def _post_reboot_update_success(
        self,
        target_version: Optional[str],
        saw_ota_activity: bool,
        saw_offline: bool,
        elapsed: float,
    ) -> bool:
        """Erfolg erst nach Reboot: Station online + update_needed=false (oder Version passt)."""
        # Vermeide False-Positive ganz am Anfang der Install-Schleife
        if not (saw_ota_activity or saw_offline or elapsed >= 20):
            return False

        station = (self.coordinator.data or {}).get(self.station_id) or {}
        if not station.get("available", False):
            return False

        current = station.get("current_version") or station.get("firmware_version")
        version_ok = bool(
            target_version
            and current
            and str(current).strip() == str(target_version).strip()
        )

        # Flash kann "done" melden bevor der Reboot beginnt – ohne Offline/Versionswechsel
        # noch nicht als fertig werten (sonst wieder "Update verfügbar" + Aktualisieren).
        if saw_ota_activity and not saw_offline and not version_ok:
            return False

        update_needed = station.get("update_needed")
        if update_needed is None and not version_ok:
            return False
        if update_needed is not None and bool(update_needed) and not version_ok:
            return False

        if version_ok:
            return True

        # update_needed=false nach Offline, Versionen ggf. noch nicht synchron
        _LOGGER.debug(
            "Post-Reboot: update_needed=false, Versionen differieren (ist=%s ziel=%s) – trotzdem Erfolg",
            current,
            target_version,
        )
        return True

    async def _fetch_update_status(self):
        """Hole Update-Status ausschließlich aus MQTT (Coordinator)."""
        try:
            station = (self.coordinator.data or {}).get(self.station_id) or {}
            mqtt_update = station.get("firmware_update")
            if isinstance(mqtt_update, dict) and mqtt_update:
                self._update_data = mqtt_update
                _LOGGER.debug("Update-Status aus MQTT (Coordinator) verwendet: %s", self._update_data)
                return
        except Exception as e:
            _LOGGER.debug("MQTT Update-Status aus Coordinator nicht nutzbar: %s", e)

        # Kein HTTP-Fallback – letzten bekannten Stand behalten
        if not self._update_data:
            self._update_data = {"status": "unknown", "progress": self._progress or 0}
