from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change

from .const import (
    DOMAIN,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TIME,
    DEFAULT_NOTIFY_TIME,
)
from . import notify

PLATFORMS = ["sensor", "button"]


def _get_notify_option(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"data": entry.data, "entry": entry}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options flow
    entry.async_on_unload(entry.add_update_listener(update_listener))

    if _get_notify_option(entry, CONF_NOTIFY_ENABLED, False):
        hour, minute = notify.parse_notify_time(
            _get_notify_option(entry, CONF_NOTIFY_TIME, DEFAULT_NOTIFY_TIME)
        )

        @callback
        def _notify_check(now):
            hass.async_create_task(notify.async_check_and_notify(hass, entry))

        entry.async_on_unload(
            async_track_time_change(hass, _notify_check, hour=hour, minute=minute, second=0)
        )
        entry.async_on_unload(
            async_track_time_change(hass, _notify_check, minute=0, second=0)
        )

        hass.async_create_task(notify.async_check_and_notify(hass, entry))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
