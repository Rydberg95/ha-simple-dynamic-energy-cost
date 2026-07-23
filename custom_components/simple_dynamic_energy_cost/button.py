from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_ENERGY_SENSOR,
    DOMAIN,
    SERVICE_EXPORT_MONTH,
    ATTR_START_DATE,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    energy_sensor_id = entry.data[CONF_ENERGY_SENSOR]
    async_add_entities([ExportMonthButton(entry, energy_sensor_id)])


class ExportMonthButton(ButtonEntity):
    """Button that exports last completed month's bookkeeping report."""

    _attr_icon = "mdi:file-export"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, energy_sensor_id: str) -> None:
        self._entry = entry
        self._energy_sensor_id = energy_sensor_id

        source_name = energy_sensor_id.split(".")[-1].replace("_", " ").title()
        self._attr_name = f"{source_name} Export Month"
        self._attr_unique_id = f"{entry.entry_id}_{energy_sensor_id.replace('.', '_')}_export_button"
        self._last_export_entity_id = f"sensor.{slugify(f'{source_name} Last Export')}"

    async def async_press(self) -> None:
        """Export the last completed month."""
        now = dt_util.now()
        if now.month == 1:
            target = now.replace(year=now.year - 1, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(month=now.month - 1, day=1, hour=0, minute=0, second=0, microsecond=0)

        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_MONTH,
            {
                ATTR_START_DATE: target.date().isoformat(),
                "format": "both",
            },
            target={"entity_id": self._last_export_entity_id},
            blocking=True,
        )