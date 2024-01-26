import os
import re
import json
import urllib.parse
import logging
from functools import partial
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Tuple
from dateutil import tz
from itertools import dropwhile, takewhile
import aiohttp
from dataclasses import dataclass
from collections.abc import Callable
from homeassistant.helpers.storage import Store


from homeassistant.const import Platform, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from .const import (
    DOMAIN,
)
from .api import GeoveloApi, GeoveloApiError


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    # here we store the coordinator for future access
    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]["geovelo_coordinator"] = GeoveloAPICoordinator(
        hass, dict(entry.data)
    )

    # will make sure async_setup_entry from sensor.py is called
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    # subscribe to config updates
    entry.async_on_unload(entry.add_update_listener(update_entry))

    return True


async def update_entry(hass, entry):
    """
    This method is called when options are updated
    We trigger the reloading of entry (that will eventually call async_unload_entry)
    """
    _LOGGER.debug("update_entry method called")
    # will make sure async_setup_entry from sensor.py is called
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """This method is called to clean all sensors before re-adding them"""
    _LOGGER.debug("async_unload_entry method called")
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform.SENSOR]
    )
    if unload_ok:
        old_entry = hass.data[DOMAIN].pop(entry.entry_id)
        if "geovelo_coordinator" in old_entry:
            coordinator = old_entry["geovelo_coordinator"]
            await coordinator.clean_cache()
    return unload_ok


class GeoveloAPICoordinator(DataUpdateCoordinator):
    """A coordinator to fetch data from the api only once"""

    STORE_VERSION = 1

    def __init__(self, hass, config: ConfigType):
        super().__init__(
            hass,
            _LOGGER,
            name="geovelo api",  # for logging purpose
            update_interval=timedelta(hours=1),
            update_method=self.update_method,
        )
        self.config = config
        self.hass = hass
        self._custom_store = Store(
            hass, self.STORE_VERSION, f"geovelo_traces_{self.config['user_id']}"
        )

    def parse_date(self, string):
        try:
            return datetime.strptime(string, "%Y-%m-%dT%H:%M:%S.%f%z")
        except Exception:
            return datetime.strptime(string, "%Y-%m-%dT%H:%M:%S%z")

    async def clean_cache(self):
        self._custom_store.async_remove()

    async def update_method(self):
        """Fetch data from API endpoint."""
        try:
            _LOGGER.debug(
                f"Calling update method, {len(self._listeners)} listeners subscribed"
            )
            if "GEOVELO_APIFAIL" in os.environ:
                raise UpdateFailed(
                    "Failing update on purpose to test state restoration"
                )
            _LOGGER.debug("Starting collecting data")

            username = self.config["username"]
            password = self.config["password"]
            user_id = self.config["user_id"]

            start_date = datetime.now() - timedelta(days=360 * 10)
            end_date = datetime.now()
            traces = []
            try:
                previous_data = await self._custom_store.async_load()
                if previous_data is not None:
                    traces = previous_data
                    last = max(
                        [self.parse_date(trace["end_datetime"]) for trace in traces]
                    )
                    # we assume nobody update their trips more than 1 week in the past
                    start_date = last - timedelta(days=7)
            except Exception as e:
                import traceback

                _LOGGER.warn(
                    f"Impossible to load previous data from {self._custom_store.path}: {type(e).__name__} {e.args}"
                )

            session = async_get_clientsession(self.hass)
            geovelo_api = GeoveloApi(session)
            try:
                auth_token = await geovelo_api.get_authorization_header(
                    username, password
                )
                new_traces = await geovelo_api.get_traces(
                    user_id, auth_token, start_date, end_date
                )
            except GeoveloApiError as e:
                raise UpdateFailed(f"Failed fetching geovelo data: {e}")

            existing_ids = {trace["id"] for trace in traces}
            for new_trace in new_traces:
                if new_trace["id"] not in existing_ids:
                    existing_ids.add(new_trace["id"])
                    traces.append(new_trace)
            try:
                await self._custom_store.async_save(traces)
            except Exception as e:
                _LOGGER.exception(
                    f"Error while caching traces: {e}, will re-query same data next time"
                )
            return traces
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


@dataclass(frozen=True, kw_only=True)
class GeoveloEntityDescription(SensorEntityDescription):
    on_receive: Callable | None = None


class GeoveloSensorEntity(CoordinatorEntity, SensorEntity):
    def __init__(
        self,
        coordinator: GeoveloAPICoordinator,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: GeoveloEntityDescription,
    ):
        super().__init__(coordinator)
        self.entity_description = description
        self.hass = hass
        self._attr_unique_id = (
            f"{config_entry.data.get('user_id')}-sensor-{description.key}"
        )

        self._attr_device_info = DeviceInfo(
            name=f"Cycle for {config_entry.data.get('user_id')}",
            entry_type=DeviceEntryType.SERVICE,
            identifiers={
                (
                    DOMAIN,
                    str(config_entry.data.get("user_id")),
                )
            },
            manufacturer="geovelo",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug(f"Receiving an update for {self.unique_id} sensor")
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        if self.entity_description.on_receive is not None:
            self._attr_native_value = self.entity_description.on_receive(
                self.coordinator.data
            )
            self.async_write_ha_state()


def sum_on_attribute(attribute_name, entries):
    return sum([el[attribute_name] for el in entries])


def build_sensors():
    return [
        GeoveloEntityDescription(
            key="distance",
            name="Total cycled distance",
            native_unit_of_measurement="m",
            device_class=SensorDeviceClass.DISTANCE,
            on_receive=partial(sum_on_attribute, "distance"),
        ),
    ]
