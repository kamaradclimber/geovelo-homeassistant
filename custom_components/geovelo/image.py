import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN
from . import build_images, GeoveloImageEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    geovelo_coordinator = hass.data[DOMAIN][entry.entry_id]["geovelo_coordinator"]
    images = [
        GeoveloImageEntity(geovelo_coordinator, hass, entry, description)
        for description in build_images()
    ]

    async_add_entities(images)
    await geovelo_coordinator.async_config_entry_first_refresh()
