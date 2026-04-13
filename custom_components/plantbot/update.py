from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
import aiohttp
import asyncio
import time
from typing import Optional
import re

from .const import DOMAIN
import logging

_LOGGER = logging.getLogger(__name__)

# Konstanten für Update-Timeout
UPDATE_STATUS_TIMEOUT = 300  # 5 Minuten max für Update
UPDATE_STATUS_INTERVAL = 2  # Status alle 2 Sekunden abfragen
UPDATE_START_RETRIES = 3  # 3 Versuche Update zu starten
UPDATE_START_RETRY_DELAY = 2  # 2 Sekunden zwischen Versuchen

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
            identifiers={(DOMAIN, f"station_{self.station_id}")},
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
            self._in_progress = False
            self.async_write_ha_state()
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
                self._in_progress = False
                self.async_write_ha_state()
                return
        
        # Überwache Update-Status
        await self._monitor_update_progress()
        
        self._in_progress = False
        self.async_write_ha_state()
        _LOGGER.info("Update-Prozess beendet für %s", self.station_name)

    async def _check_device_reachable(self) -> bool:
        """Prüfe ob das Gerät erreichbar ist."""
        try:
            url = f"http://{self._station_ip}/status"
            timeout = aiohttp.ClientTimeout(total=5, connect=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    return resp.status == 200
        except Exception as e:
            _LOGGER.debug("Gerät nicht erreichbar: %s", e)
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

    async def _monitor_update_progress(self):
        """Überwache Update-Fortschritt mit robustem Error-Handling."""
        max_duration = UPDATE_STATUS_TIMEOUT
        check_interval = UPDATE_STATUS_INTERVAL
        last_successful_check = time.time()
        
        while True:
            elapsed = time.time() - self._update_start_time
            
            # Timeout prüfen
            if elapsed > max_duration:
                _LOGGER.error("Update-Timeout nach %d Sekunden", max_duration)
                break
            
            try:
                await self._fetch_update_status()
                self._consecutive_errors = 0  # Reset bei Erfolg
                last_successful_check = time.time()
                
                progress = self._update_data.get("progress", 0)
                status = self._update_data.get("status", "").lower()
                
                self._progress = progress
                self.async_write_ha_state()
                
                _LOGGER.debug("Update-Status: %s, Fortschritt: %d%%, Elapsed: %ds", 
                             status, progress, int(elapsed))

                # Prüfe ob Update abgeschlossen ist
                if status in ["done", "complete", "success"]:
                    _LOGGER.info("Update erfolgreich abgeschlossen nach %d Sekunden", int(elapsed))
                    break
                
                if status in ["failed", "error"]:
                    error_msg = self._update_data.get("error", "Unbekannter Fehler")
                    _LOGGER.error("Update fehlgeschlagen: %s", error_msg)
                    break
                
                # Prüfe ob Update noch läuft
                if status not in ["installing", "started", "downloading", "idle"]:
                    _LOGGER.warning("Unbekannter Update-Status: %s", status)
                
                await asyncio.sleep(check_interval)
                
            except Exception as e:
                self._consecutive_errors += 1
                _LOGGER.warning("Fehler beim Statusabruf (Fehler %d/%d): %s", 
                               self._consecutive_errors, self._max_consecutive_errors, e)
                
                # Wenn zu viele aufeinanderfolgende Fehler, abbrechen
                if self._consecutive_errors >= self._max_consecutive_errors:
                    _LOGGER.error("Zu viele aufeinanderfolgende Fehler beim Statusabruf, Update-Überwachung abgebrochen")
                    break
                
                # Wenn letzter erfolgreicher Check zu lange her, abbrechen
                if time.time() - last_successful_check > 60:  # 1 Minute ohne erfolgreichen Check
                    _LOGGER.error("Kein erfolgreicher Status-Check seit 60 Sekunden, Update-Überwachung abgebrochen")
                    break
                
                # Warte länger bei Fehlern (exponentielles Backoff)
                wait_time = min(check_interval * (2 ** (self._consecutive_errors - 1)), 10)
                await asyncio.sleep(wait_time)

    async def _fetch_update_status(self):
        """Hole Update-Status vom PlantBot-Gerät mit besserem Error-Handling."""
        # Prefer MQTT-pushed update status from coordinator (avoids polling)
        try:
            station = (self.coordinator.data or {}).get(self.station_id) or {}
            mqtt_update = station.get("firmware_update")
            if isinstance(mqtt_update, dict) and mqtt_update:
                self._update_data = mqtt_update
                _LOGGER.debug("Update-Status aus MQTT (Coordinator) verwendet: %s", self._update_data)
                return
        except Exception as e:
            _LOGGER.debug("MQTT Update-Status aus Coordinator nicht nutzbar: %s", e)

        url = f"http://{self._station_ip}/update_status"
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        try:
                            self._update_data = await resp.json()
                            _LOGGER.debug("Update-Status erhalten: %s", self._update_data)
                        except Exception as e:
                            _LOGGER.warning("Ungültiges JSON in Update-Status: %s", e)
                            self._update_data = {}
                    elif resp.status == 503:
                        # Service Unavailable - Update läuft möglicherweise
                        _LOGGER.debug("Update-Status-Endpoint antwortet mit 503 (Service Unavailable)")
                        self._update_data = {"status": "installing", "progress": self._progress or 0}
                    else:
                        _LOGGER.debug("Update-Status-Endpoint antwortete mit HTTP %s", resp.status)
                        self._update_data = {}
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout beim Abrufen des Update-Status")
            # Behalte letzten bekannten Status bei
            if not self._update_data:
                self._update_data = {"status": "unknown", "progress": self._progress or 0}
        except aiohttp.ClientError as e:
            _LOGGER.warning("Client-Fehler beim Abrufen des Update-Status: %s", e)
            if not self._update_data:
                self._update_data = {"status": "unknown", "progress": self._progress or 0}
        except Exception as e:
            _LOGGER.warning("Unerwarteter Fehler beim Abrufen des Update-Status: %s", e)
            if not self._update_data:
                self._update_data = {"status": "unknown", "progress": self._progress or 0}
