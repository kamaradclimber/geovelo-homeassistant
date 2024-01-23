import logging
from typing import Any, Optional, Tuple
import voluptuous as vol
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant import config_entries
from .api import GeoveloApi
from .const import (
    DOMAIN,
)
from homeassistant import config_entries

_LOGGER = logging.getLogger(__name__)

# Description of the config flow:
# async_step_user is called when user starts to configure the integration
# we follow with a flow of form/menu
# eventually we call async_create_entry with a dictionnary of data
# HA calls async_setup_entry with a ConfigEntry which wraps this data (defined in __init__.py)
# in async_setup_entry we call hass.config_entries.async_forward_entry_setups to setup each relevant platform (sensor in our case)
# HA calls async_setup_entry from sensor.py

CREDS_SCHEMA = vol.Schema({
    vol.Required("username", default="my_email@example.org"): cv.string,
    vol.Required("password", default="aZ2@@1!78aRaLA"): cv.string,
    vol.Required("user_id", default="12345"): cv.string,
    })


class SetupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        """Initialize"""
        self.data = {}

    @callback
    def _show_setup_form(self, step_id=None, user_input=None, schema=None, errors=None):
        """Show the setup form to the user."""

        if user_input is None:
            user_input = {}

        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors or {},
        )

    async def async_step_user(self, user_input: Optional[dict[str, Any]] = None):
        """Called once with None as user_input, then a second time with user provided input"""
        errors = {}
        if user_input is not None:
            self.data["username"] = user_input["username"]
            self.data["password"] = user_input["password"]
            self.data["user_id"] = user_input["user_id"]
            return self.async_create_entry(title="geovelo", data=self.data)

        return self._show_setup_form("user", user_input, CREDS_SCHEMA, errors)
