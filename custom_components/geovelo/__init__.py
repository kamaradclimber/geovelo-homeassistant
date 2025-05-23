import os
import re
import json
import gzip
import copy
import base64
import urllib.parse
import logging
from functools import partial
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Tuple, List
from dateutil import tz
from itertools import dropwhile, takewhile
import aiohttp
from dataclasses import dataclass
from collections.abc import Callable

from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.helpers.storage import Store
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import Platform, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.util import dt as dt_util
from .const import DOMAIN, GEOVELO_API_URL
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
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform.SENSOR, Platform.IMAGE]
    )

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


def parse_date(string):
    try:
        return datetime.strptime(string, "%Y-%m-%dT%H:%M:%S.%f%z")
    except Exception:
        return datetime.strptime(string, "%Y-%m-%dT%H:%M:%S%z")


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
            hass,
            version=self.STORE_VERSION,
            minor_version=2,
            key=f"geovelo_traces_{self.config['user_id']}",
        )
        self._has_loaded_once = False

    async def clean_cache(self):
        self._custom_store.async_remove()

    def _compress_key(self, d, key):
        """
        Compress <key> from d, in place
        """
        s = json.dumps(d[key])
        d[key] = base64.b64encode(gzip.compress(s.encode())).decode()

    def _decompress_key(self, d, key, i):
        """
        Decompress <key> from d, in place
        """
        if isinstance(d[key], dict):
            _LOGGER.warn(
                f"For some reason {key} ({i}) had not been compressed when storing the file"
            )
            return
        binary = base64.b64decode(d[key].encode())
        uncompressed = gzip.decompress(binary)
        d[key] = json.loads(uncompressed)

    COMPRESSED_KEYS = ["geometry", "elevations", "speeds"]

    async def _load_traces(self) -> Optional[list]:
        if self.data is not None:
            # don't load from store if we already ran once
            return self.data["traces"]
        traces = await self._custom_store.async_load()
        if traces is None:
            _LOGGER.warn(
                "No traces loaded from cache, it should only happen when installing this integration"
            )
            return None
        traces = copy.deepcopy(traces)
        for i, trace in enumerate(traces):
            for key in self.COMPRESSED_KEYS:
                if key in trace:
                    self._decompress_key(trace, key, i)
        return traces

    async def _store_traces(self, traces):
        compressed_traces = copy.deepcopy(traces)
        for trace in compressed_traces:
            for key in self.COMPRESSED_KEYS:
                # we decide to remove those keys instead of compressing
                # it saves data on disk
                if key in trace:
                    trace.pop(key)
        try:
            await self._custom_store.async_save(compressed_traces)
        except Exception as e:
            _LOGGER.exception(
                f"Error while caching traces: {e}, will re-query same data next time"
            )

    async def update_method(self):
        """Fetch geovelo data from API endpoint."""
        try:
            _LOGGER.debug(
                f"Calling update method, {len(self._listeners)} listeners subscribed"
            )
            if "GEOVELO_APIFAIL" in os.environ:
                raise UpdateFailed(
                    "Failing update on purpose to test state restoration"
                )
            _LOGGER.debug("Starting collecting geovelo data")

            username = self.config["username"]
            password = self.config["password"]
            user_id = self.config["user_id"]

            start_date = datetime.now() - timedelta(days=360 * 10)
            if "GEOVELO_FAST" in os.environ:
                start_date = datetime.now() - timedelta(days=30)
            end_date = datetime.now()
            traces = []
            try:
                previous_data = await self._load_traces()
                if previous_data is not None:
                    traces = previous_data
                    last = max([parse_date(trace["end_datetime"]) for trace in traces])
                    # we assume nobody update their trips more than 1 week in the past
                    start_date = last - timedelta(days=7)
            except Exception as e:
                _LOGGER.warn(
                    f"Impossible to load previous traces from {self._custom_store.path}: {type(e).__name__} {e.args}"
                )

            session = async_get_clientsession(self.hass)
            geovelo_api = GeoveloApi(session)
            try:
                await geovelo_api.authenticate(username, password)
                new_traces = await geovelo_api.get_traces(start_date, end_date)
            except GeoveloApiError as e:
                raise UpdateFailed(f"Failed fetching geovelo traces: {e}")

            existing_ids = {trace["id"] for trace in traces}
            for new_trace in new_traces:
                if new_trace["id"] not in existing_ids:
                    existing_ids.add(new_trace["id"])
                    traces.append(new_trace)
            await self._store_traces(traces)

            # we don't need to store zones on disk, it's a single call
            try:
                zones = await geovelo_api.get_zones()
            except GeoveloApiError as e:
                raise UpdateFailed(f"Failed fetching geovelo zones: {e}")


            return {"traces": traces, "zones": zones}
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


class GeoveloUtilityMeterSensor(UtilityMeterSensor):
    def __init__(self, icon, device_class, **args):
        super().__init__(**args)
        self._attr_device_class = device_class
        self._attr_icon = icon

    @property
    def device_class(self) -> SensorDeviceClass | None:
        return self._attr_device_class


@dataclass(frozen=True, kw_only=True)
class GeoveloSensorEntityDescription(SensorEntityDescription):
    # callable that will be called to compute state
    compute_value: Callable | None = None
    # additional hook to receive value computed by `compute_value`. Will be used mostly for achievements
    post_compute_value: Callable | None = None
    monthly_utility: bool = False


class GeoveloSensorEntity(CoordinatorEntity, SensorEntity):
    def __init__(
        self,
        coordinator: GeoveloAPICoordinator,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: GeoveloSensorEntityDescription,
        async_add_entities: AddEntitiesCallback,
    ):
        super().__init__(coordinator)
        self.entity_description = description
        self.config_entry = config_entry
        self.hass = hass
        self._async_add_entities = async_add_entities
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
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.entity_description.monthly_utility:
            name = self.name.replace("Total ", "")
            monthly = GeoveloUtilityMeterSensor(
                icon=self.entity_description.icon,
                device_class=self.entity_description.device_class,
                meter_type="monthly",
                name=f"Monthly {name}",
                source_entity=self.entity_id,
                unique_id=f"{self.unique_id}_monthly",
                cron_pattern=None,
                delta_values=None,
                meter_offset=timedelta(seconds=0),
                net_consumption=None,
                parent_meter=self.config_entry.entry_id,  # not sure of what it does!
                periodically_resetting=False,
                tariff_entity=None,
                tariff=None,
                sensor_always_available=True,
                device_info=self.device_info,
            )
            self._async_add_entities([monthly])

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug(f"Receiving an update for {self.unique_id} sensor")
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        if self.entity_description.compute_value is not None:
            self._attr_native_value = self.entity_description.compute_value(
                self.coordinator.data
            )
            # post processing, possibly to create an event
            if self.entity_description.post_compute_value is not None:
                self.entity_description.post_compute_value(self._attr_native_value)
            self.async_write_ha_state()


def sum_on_attribute(attribute_name, entries) -> int:
    return sum([el[attribute_name] for el in entries])


def sum_on_attribute_with_none(attribute_name, entries) -> int:
    return sum([el[attribute_name] or 0 for el in entries])


def average_speed(entries: list) -> Optional[float]:
    total_time = sum_on_attribute("duration", entries)
    if total_time == 0:
        return None
    total_distance = sum_on_attribute("distance", entries)
    return total_distance / 1000 / (total_time / 3600)


def count_nightowl(entries) -> int:
    count = 0
    for t in entries:
        if "usertracegameprogress" not in t or t["usertracegameprogress"] is None:
            continue
        if "during_night" not in t["usertracegameprogress"]:
            continue
        if t["usertracegameprogress"]["during_night"]:
            count += 1
    return count


def compute_co2(entries):
    # this value is the one used by geovelo
    CO2_PER_KM = 1 / (4.594843462246778 * 1000)
    return sum_on_attribute("distance", entries) * CO2_PER_KM


def consecutive_days(timezone, traces) -> Optional[int]:
    today = datetime.now(tz=timezone).date()
    days_of_cycling = set()
    for t in traces:
        d = parse_date(t["start_datetime"]).date()
        days_of_cycling.add(d)
    last_day_cycled = max(days_of_cycling)
    if today - last_day_cycled > timedelta(days=1):
        return 0
    checked_day = last_day_cycled
    while checked_day in days_of_cycling:
        checked_day -= timedelta(days=1)
    return int((last_day_cycled - checked_day).total_seconds() / 3600 / 24)

def explorer_achievement(hass, explored_zone_count):
    if explored_zone_count >= 1000:
        hass.bus.fire(
            "achievement_granted",
            {
                "major_version": 0,
                "minor_version": 1,
                "achievement": {
                    "title": "Magellan",
                    "description": "You've explored more than 1000 zones",
                    "source": "geovelo",
                    "id": "109e4dcd-f83a-40d2-b61f-89e94ecf16ad",
                },
            },
        )
    if explored_zone_count >= 100:
        hass.bus.fire(
            "achievement_granted",
            {
                "major_version": 0,
                "minor_version": 1,
                "achievement": {
                    "title": "Columbus",
                    "description": "You've explored more than 100 zones",
                    "source": "geovelo",
                    "id": "123e4def-fffa-36d2-b65f-89e94ecf16ae",
                },
            },
        )


def non_stop_achievements(hass, consecutive_days):
    if consecutive_days >= 8:
        hass.bus.fire(
            "achievement_granted",
            {
                "major_version": 0,
                "minor_version": 1,
                "achievement": {
                    "title": "8 days a week 🎵",
                    "description": "You've cycled every day, for more than a week.",
                    "source": "geovelo",
                    "id": "009e4dcd-f83a-40d2-b61f-89e94ecf07fa",
                },
            },
        )


def check_distance_achievement(hass, total_cycled_meters):
    if total_cycled_meters > 3_500_000:
        hass.bus.fire(
            "achievement_granted",
            {
                "major_version": 0,
                "minor_version": 1,
                "achievement": {
                    "title": "Tour de France",
                    "description": "3500km is the average length of the Tour de France competition, could you do it in 23 days?",
                    "source": "geovelo",
                    "id": "009e4dcd-f83a-40d2-b61f-89e94ecf07fa",
                },
            },
        )
    if total_cycled_meters > 6_371_000:
        hass.bus.fire(
            "achievement_granted",
            {
                "major_version": 0,
                "minor_version": 1,
                "achievement": {
                    "title": "First journey to the center of the Earth",
                    "description": "Going on a straight line, you would have reached center of the earth",
                    "source": "geovelo",
                    "id": "94d28dbc-c616-486f-bd3e-0cfad6aa2497",
                },
            },
        )
    # this one is quite unlikely!
    if total_cycled_meters > 384_400_000:
        hass.bus.fire(
            "achievement_granted",
            {
                "major_version": 0,
                "minor_version": 1,
                "achievement": {
                    "title": "To the Moon",
                    "description": "Going on a straight line, you would have reached the Moon",
                    "source": "geovelo",
                    "id": "af9fa29c-4b70-45dd-800c-ef87d3509058",
                },
            },
        )

def ontraces[F: Any](f: Callable[[list], F]) -> Callable[[dict], F]:
    def w(data: dict) -> F:
        return f(data["traces"])
    return w

def onzones[F: Any](f: Callable[[list], F]) -> Callable[[dict], F]:
    def w(data: dict) -> F:
        return f(data["zones"])
    return w

def build_sensors(hass: HomeAssistant) -> list[GeoveloSensorEntityDescription]:
    return [
        GeoveloSensorEntityDescription(
            key="distance",
            name="Total cycled distance",
            native_unit_of_measurement="m",
            suggested_unit_of_measurement="km",
            icon="mdi:map-marker-distance",
            device_class=SensorDeviceClass.DISTANCE,
            compute_value=ontraces(partial(sum_on_attribute, "distance")),
            post_compute_value=partial(check_distance_achievement, hass),
            monthly_utility=True,
            state_class=SensorStateClass.TOTAL,
        ),
        GeoveloSensorEntityDescription(
            key="co2",
            name="Total saved co2",
            native_unit_of_measurement="kg",
            device_class=SensorDeviceClass.WEIGHT,
            icon="mdi:leaf",
            compute_value=ontraces(compute_co2),
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        GeoveloSensorEntityDescription(
            key="trip_count",
            name="Number of trips",
            icon="mdi:counter",
            compute_value=ontraces(len),
            monthly_utility=True,
            state_class=SensorStateClass.TOTAL,
        ),
        GeoveloSensorEntityDescription(
            key="night_owl_stats",
            name="Night trips",
            icon="mdi:owl",
            compute_value=ontraces(count_nightowl),
            state_class=SensorStateClass.TOTAL,
        ),
        GeoveloSensorEntityDescription(
            key="consecutive_days_of_cycling",
            name="Consecutive days of cycling",
            icon="mdi:medal",
            compute_value=ontraces(partial(
                consecutive_days, dt_util.get_default_time_zone()
            )),
            post_compute_value=partial(non_stop_achievements, hass),
            state_class=SensorStateClass.TOTAL,
        ),
        GeoveloSensorEntityDescription(
            key="cycle_time",
            name="Time cycling",
            compute_value=ontraces(partial(sum_on_attribute, "duration")),
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement="s",
            monthly_utility=True,
            state_class=SensorStateClass.TOTAL,
        ),
        GeoveloSensorEntityDescription(
            key="vertical_gain",
            name="Vertical gain",
            icon="mdi:summit",
            compute_value=ontraces(partial(sum_on_attribute_with_none, "vertical_gain")),
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement="m",
            monthly_utility=True,
            state_class=SensorStateClass.TOTAL,
        ),
        GeoveloSensorEntityDescription(
            key="average_speed",
            name="Average speed",
            compute_value=ontraces(average_speed),
            device_class=SensorDeviceClass.SPEED,
            native_unit_of_measurement="km/h",
            suggested_display_precision=0,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        GeoveloSensorEntityDescription(
            key="h3_zones",
            name="Explored zones",
            compute_value=onzones(len),
            post_compute_value=partial(explorer_achievement, hass),
            icon="mdi:map",
            state_class=SensorStateClass.TOTAL,
        ),
    ]


@dataclass(frozen=True, kw_only=True)
class GeoveloImageEntityDescription(ImageEntityDescription):
    compute_value: Callable | None = None


class GeoveloImageEntity(CoordinatorEntity, ImageEntity):
    def __init__(
        self,
        coordinator: GeoveloAPICoordinator,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: GeoveloSensorEntityDescription,
    ):
        super().__init__(coordinator)
        ImageEntity.__init__(self, hass)
        self.entity_description = description
        self.hass = hass
        self._attr_unique_id = (
            f"{config_entry.data.get('user_id')}-sensor-{description.key}"
        )
        self.image_url = None

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
        _LOGGER.debug(f"Receiving an update for {self.unique_id} image")
        if not self.coordinator.last_update_success:
            _LOGGER.debug("Last coordinator failed, assuming state has not changed")
            return
        if self.entity_description.compute_value is not None:
            (image_last_updated, image_url) = self.entity_description.compute_value(
                self.coordinator.data
            )
            self._attr_image_last_updated = image_last_updated
            self._attr_image_url = image_url
            self.async_write_ha_state()


def extract_last_trip_info(traces) -> Tuple[Optional[datetime], Optional[str]]:
    def extract_end_date(trace):
        return parse_date(trace["end_datetime"])

    latest_trace = max(traces, default=None, key=extract_end_date)
    if latest_trace is None:
        return (None, None)
    image_url = f"{GEOVELO_API_URL}/{latest_trace['preview']}"
    return (extract_end_date(latest_trace), image_url)


def build_images():
    return [
        GeoveloImageEntityDescription(
            key="last_trace",
            name="Last Trip",
            compute_value=ontraces(extract_last_trip_info),
        )
    ]
