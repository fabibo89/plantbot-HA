from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN
import re
import logging
import aiohttp

_LOGGER = logging.getLogger(__name__)

class PlantbotHAConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step - connection type selection."""
        if user_input is not None:
            connection_type = user_input.get("connection_type")
            if connection_type == "server":
                return await self.async_step_server()
            else:
                return await self.async_step_device()
        
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("connection_type"): vol.In({
                    "server": "Server-URL",
                    "device": "PlantBot IP-Adresse"
                }),
            }),
        )

    async def async_step_server(self, user_input=None):
        """Handle server URL input."""
        errors = {}
        
        if user_input is not None:
            server_url = user_input.get("server_url", "").strip()
            if not server_url:
                errors["server_url"] = "server_url_required"
            elif not self._is_valid_url(server_url):
                errors["server_url"] = "invalid_url"
            else:
                # Normalisiere URL (füge http:// hinzu falls nicht vorhanden)
                if not server_url.startswith(("http://", "https://")):
                    server_url = f"http://{server_url}"
                
                # Speichere Server-URL für nächsten Schritt
                self.server_url = server_url
                return await self.async_step_server_auth()
        
        return self.async_show_form(
            step_id="server",
            data_schema=vol.Schema({
                vol.Required("server_url"): str,
            }),
            errors=errors,
        )

    async def async_step_server_auth(self, user_input=None):
        """Handle server authentication."""
        errors = {}
        
        if user_input is not None:
            email = user_input.get("email", "").strip()
            password = user_input.get("password", "")
            
            if not email:
                errors["email"] = "email_required"
            elif "@" not in email:
                errors["email"] = "invalid_email"
            elif not password:
                errors["password"] = "password_required"
            else:
                # Versuche Login
                try:
                    from homeassistant.helpers.aiohttp_client import async_get_clientsession
                    from urllib.parse import urlencode, urljoin
                    
                    session = async_get_clientsession(self.hass)
                    
                    # Normalisiere URL - entferne trailing slash und /api/v1 falls vorhanden
                    base_url = self.server_url.rstrip('/')
                    if base_url.endswith('/api/v1'):
                        base_url = base_url[:-7]  # Entferne '/api/v1'
                    elif '/api/v1' in base_url:
                        # Falls /api/v1 irgendwo in der URL, entferne alles danach
                        base_url = base_url.split('/api/v1')[0]
                    
                    # Versuche verschiedene URL-Varianten
                    # 1. Über nginx (Port 3001) - /api/v1/auth/login
                    # 2. Direkt Backend (Port 8000) - /api/v1/auth/login
                    # 3. Falls Port 3001, versuche auch Port 8000
                    login_urls = [f"{base_url}/api/v1/auth/login"]
                    
                    # Falls Port 3001 (nginx), versuche auch direkt Backend Port 8000
                    if ':3001' in base_url:
                        backend_url = base_url.replace(':3001', ':8000')
                        login_urls.insert(0, f"{backend_url}/api/v1/auth/login")
                        _LOGGER.info("Port 3001 erkannt, versuche auch Backend direkt auf Port 8000")
                    
                    login_url = None
                    last_error = None
                    
                    # OAuth2PasswordRequestForm erwartet application/x-www-form-urlencoded
                    # Verwende URLSearchParams-äquivalentes Format
                    form_data = urlencode({
                        "username": email,
                        "password": password
                    })
                    
                    _LOGGER.info("Login-Versuch:")
                    _LOGGER.info("  Server URL (roh): %s", self.server_url)
                    _LOGGER.info("  Base URL (normalisiert): %s", base_url)
                    _LOGGER.info("  Email: %s", email)
                    _LOGGER.info("  Form Data: username=%s&password=***", email)
                    
                    headers = {"Content-Type": "application/x-www-form-urlencoded"}
                    
                    # Versuche alle URL-Varianten
                    for attempt_url in login_urls:
                        _LOGGER.info("  Versuche Login-URL: %s", attempt_url)
                        try:
                            async with session.post(
                                attempt_url,
                                data=form_data,
                                headers=headers,
                                ssl=False,
                                timeout=15
                            ) as response:
                                response_text = await response.text()
                                _LOGGER.info("  Response Status: %s", response.status)
                                _LOGGER.info("  Response Body (erste 200 Zeichen): %s", response_text[:200])
                                
                                if response.status == 200:
                                    try:
                                        token_data = await response.json()
                                        access_token = token_data.get("access_token")
                                        refresh_token = token_data.get("refresh_token")
                                        
                                        if access_token:
                                            _LOGGER.info("Login erfolgreich für %s über %s", email, attempt_url)
                                            # Speichere die erfolgreiche URL als server_url
                                            successful_url = base_url
                                            if ':8000' in attempt_url:
                                                # Wenn Backend direkt funktioniert, verwende das
                                                successful_url = base_url.replace(':3001', ':8000') if ':3001' in base_url else base_url
                                            
                                            # Speichere Daten für MQTT-Config Schritt
                                            self.server_url = successful_url
                                            self.email = email
                                            self.access_token = access_token
                                            self.refresh_token = refresh_token
                                            return await self.async_step_mqtt_config()
                                        else:
                                            _LOGGER.error("Kein Access Token in Response: %s", token_data)
                                            last_error = "invalid_response"
                                    except Exception as json_err:
                                        _LOGGER.error("Fehler beim Parsen der JSON-Response: %s, Response: %s", json_err, response_text)
                                        last_error = "invalid_response"
                                elif response.status == 401:
                                    _LOGGER.warning("Login fehlgeschlagen: Ungültige Anmeldedaten (401)")
                                    errors["base"] = "invalid_auth"
                                    break  # Kein Grund, andere URLs zu versuchen
                                elif response.status == 403:
                                    _LOGGER.warning("Login fehlgeschlagen: Account inaktiv (403)")
                                    errors["base"] = "account_inactive"
                                    break
                                elif response.status == 404:
                                    _LOGGER.warning("Login-Endpoint nicht gefunden (404): %s", attempt_url)
                                    last_error = "endpoint_not_found"
                                    continue  # Versuche nächste URL
                                elif response.status == 405:
                                    _LOGGER.warning("Method Not Allowed (405) für %s - versuche nächste URL", attempt_url)
                                    last_error = "connection_error"
                                    continue  # Versuche nächste URL
                                else:
                                    _LOGGER.warning("Login fehlgeschlagen: HTTP %s für %s", response.status, attempt_url)
                                    last_error = "connection_error"
                                    continue  # Versuche nächste URL
                        except aiohttp.ClientConnectorError as e:
                            _LOGGER.warning("Verbindungsfehler für %s: %s", attempt_url, e)
                            last_error = "connection_error"
                            continue
                        except Exception as e:
                            _LOGGER.warning("Fehler für %s: %s", attempt_url, e)
                            last_error = "connection_error"
                            continue
                    
                    # Wenn alle Versuche fehlgeschlagen sind
                    if "base" not in errors:
                        errors["base"] = last_error or "connection_error"
                        _LOGGER.error("Alle Login-Versuche fehlgeschlagen")
                except aiohttp.ClientConnectorError as e:
                    _LOGGER.error("Verbindungsfehler beim Login: %s", e)
                    errors["base"] = "connection_error"
                except aiohttp.ServerTimeoutError as e:
                    _LOGGER.error("Timeout beim Login: %s", e)
                    errors["base"] = "timeout_error"
                except Exception as e:
                    _LOGGER.exception("Unerwarteter Fehler beim Login: %s", e)
                    errors["base"] = "connection_error"
        
        return self.async_show_form(
            step_id="server_auth",
            data_schema=vol.Schema({
                vol.Required("email"): str,
                vol.Required("password"): str,
            }),
            errors=errors,
        )

    async def async_step_device(self, user_input=None):
        """Handle device IP input."""
        errors = {}
        
        if user_input is not None:
            device_ip = user_input.get("device_ip", "").strip()
            if not device_ip:
                errors["device_ip"] = "device_ip_required"
            elif not self._is_valid_ip(device_ip):
                errors["device_ip"] = "invalid_ip"
            else:
                # Speichere Device-IP für nächsten Schritt
                self.device_ip = device_ip
                return await self.async_step_mqtt_config()
        
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({
                vol.Required("device_ip"): str,
            }),
            errors=errors,
        )

    async def async_step_mqtt_config(self, user_input=None):
        """Handle MQTT broker configuration."""
        errors = {}
        
        if user_input is not None:
            mqtt_broker = user_input.get("mqtt_broker", "").strip()
            mqtt_port = user_input.get("mqtt_port", 1883)
            mqtt_username = user_input.get("mqtt_username", "").strip()
            mqtt_password = user_input.get("mqtt_password", "")
            
            if not mqtt_broker:
                errors["mqtt_broker"] = "mqtt_broker_required"
            elif not self._is_valid_ip(mqtt_broker) and "." not in mqtt_broker:
                errors["mqtt_broker"] = "invalid_mqtt_broker"
            else:
                # Erstelle Config Entry
                data = {}
                if hasattr(self, 'server_url'):
                    data = {
                        "connection_type": "server",
                        "server_url": self.server_url,
                        "email": getattr(self, 'email', ''),
                        "access_token": getattr(self, 'access_token', ''),
                        "refresh_token": getattr(self, 'refresh_token', ''),
                    }
                    title = f"PlantBot Server ({self.server_url})"
                else:
                    data = {
                        "connection_type": "device",
                        "device_ip": self.device_ip,
                    }
                    title = f"PlantBot Device ({self.device_ip})"
                
                # Füge MQTT-Einstellungen hinzu
                data["mqtt_broker"] = mqtt_broker
                data["mqtt_port"] = mqtt_port
                if mqtt_username:
                    data["mqtt_username"] = mqtt_username
                if mqtt_password:
                    data["mqtt_password"] = mqtt_password
                
                return self.async_create_entry(title=title, data=data)
        
        return self.async_show_form(
            step_id="mqtt_config",
            data_schema=vol.Schema({
                vol.Required("mqtt_broker"): str,
                vol.Optional("mqtt_port", default=1883): int,
                vol.Optional("mqtt_username"): str,
                vol.Optional("mqtt_password"): str,
            }),
            errors=errors,
        )
    
    def _is_valid_url(self, url):
        """Einfache URL-Validierung."""
        # Entferne http:// oder https:// für die Validierung
        url_clean = url.replace("http://", "").replace("https://", "")
        # Prüfe ob es eine IP oder Domain ist
        if self._is_valid_ip(url_clean.split(":")[0]):
            return True
        # Domain-Validierung (einfach)
        if "." in url_clean and len(url_clean.split(".")) >= 2:
            return True
        return False
    
    def _is_valid_ip(self, ip):
        """Einfache IP-Validierung."""
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        try:
            for part in parts:
                num = int(part)
                if num < 0 or num > 255:
                    return False
            return True
        except ValueError:
            return False

