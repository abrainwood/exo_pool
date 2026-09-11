import logging
import urllib.parse
import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client

from .const import DOMAIN
from .redact import redact, scrub_text

_LOGGER = logging.getLogger(__name__)

LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)

LOGIN_URL = "https://prod.zodiac-io.com/users/v1/login"
API_KEY_PROD = "EOOEMOW4YR6QNB11"
API_KEY_R = "EOOEMOW4YR6QNB07"


class ExoPoolConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Exo Pool."""

    VERSION = 1

    def __init__(self):
        self.email = None
        self.password = None
        self.auth_token = None
        self.id_token = None
        self.user_id = None

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step of the config flow."""
        errors = {}

        if user_input is not None:
            self.email = user_input["email"]
            self.password = user_input["password"]
            session = aiohttp_client.async_get_clientsession(self.hass)

            payload = {
                "api_key": API_KEY_PROD,
                "email": self.email,
                "password": self.password,
            }

            try:
                async with session.post(
                    LOGIN_URL,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "okhttp/3.14.7",
                    },
                ) as resp:
                    _LOGGER.debug("Login raw response status: %s", resp.status)

                    # Parse JSON response
                    try:
                        result = await resp.json()
                    except Exception as e:
                        _LOGGER.error("Failed to parse login response JSON: %s", e)
                        errors["base"] = "unknown"
                        return self.async_show_form(
                            step_id="user",
                            data_schema=LOGIN_SCHEMA,
                            errors=errors,
                        )

                    _LOGGER.debug("Login response parsed: %s", redact(result))
                    _LOGGER.debug(
                        "Condition check: status=%s, auth_token=%s, userPoolOAuth=%s",
                        resp.status == 200,
                        "authentication_token" in result,
                        "userPoolOAuth" in result,
                    )

                    if (
                        resp.status == 200
                        and "authentication_token" in result
                        and "userPoolOAuth" in result
                    ):
                        self.auth_token = result["authentication_token"]
                        self.id_token = result["userPoolOAuth"].get("IdToken")
                        self.user_id = result["id"]
                        if not self.id_token:
                            _LOGGER.error(
                                "No userPoolOAuth.IdToken in response: %s", redact(result)
                            )
                            errors["base"] = "auth_failed"
                        else:
                            _LOGGER.debug(
                                "Authentication successful, proceeding to select_system"
                            )
                            return await self.async_step_select_system()
                    else:
                        _LOGGER.error(
                            "Login response invalid: Status=%s, Result=%s",
                            resp.status,
                            redact(result),
                        )
                        errors["base"] = "auth_failed"

            except Exception as e:
                _LOGGER.exception("Unexpected error during login: %s", e)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=LOGIN_SCHEMA,
            errors=errors,
        )

    async def async_step_select_system(self, user_input=None) -> FlowResult:
        """Handle the system selection step of the config flow."""
        errors = {}
        session = aiohttp_client.async_get_clientsession(self.hass)

        params = {
            "api_key": API_KEY_R,
            "authentication_token": self.auth_token,
            "user_id": self.user_id,
            "timestamp": 1663228298244,  # Required dummy value
        }

        system_url = (
            f"https://r-api.iaqualink.net/devices.json?{urllib.parse.urlencode(params)}"
        )

        try:
            async with session.get(system_url) as resp:
                result = await resp.json()
                _LOGGER.debug("System discovery result: %s", redact(result))

            if not isinstance(result, list) or not result:
                errors["base"] = "no_systems"
                return self.async_show_form(step_id="select_system", errors=errors)

            self.systems = {
                f"{s.get('serial_number')} ({s.get('name', 'Unnamed')})": s.get(
                    "serial_number"
                )
                for s in result
                if s.get("device_type") == "exo"
            }

            if user_input:
                serial_number = self.systems[user_input["system"]]
                return self.async_create_entry(
                    title=f"Exo Pool ({serial_number})",
                    data={
                        "email": self.email,
                        "password": self.password,
                        "auth_token": self.auth_token,
                        "id_token": self.id_token,
                        "user_id": self.user_id,
                        "serial_number": serial_number,
                    },
                )

            return self.async_show_form(
                step_id="select_system",
                data_schema=vol.Schema(
                    {vol.Required("system"): vol.In(list(self.systems.keys()))}
                ),
                errors={},
            )

        except aiohttp.ClientResponseError as err:
            # ClientResponseError's own __str__ embeds the request URL,
            # which carries authentication_token and api_key as query
            # params - log status/message only, not str(err) or exc_info.
            _LOGGER.error(
                "Error during system selection: HTTP %s %s", err.status, err.message
            )
            errors["base"] = "unknown"
            return self.async_show_form(step_id="select_system", errors=errors)
        except Exception as err:
            # Several aiohttp exceptions (ServerTimeoutError, InvalidUrlClientError,
            # ...) embed the full request URL in their own __str__, so the secrets
            # in it are scrubbed at the log call rather than by exception type.
            _LOGGER.error(
                "Error during system selection: %s: %s",
                type(err).__name__,
                scrub_text(str(err), (self.auth_token, API_KEY_R)),
            )
            errors["base"] = "unknown"
            return self.async_show_form(step_id="select_system", errors=errors)
