import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from datetime import timedelta

from .const import DOMAIN
from . import build_sensors, GeoveloSensorEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    geovelo_coordinator = hass.data[DOMAIN][entry.entry_id]["geovelo_coordinator"]
    sensors = [
        GeoveloSensorEntity(
            geovelo_coordinator, hass, entry, description, async_add_entities
        )
        for description in build_sensors()
    ]

    async_add_entities(sensors)
    await geovelo_coordinator.async_config_entry_first_refresh()
