from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_FIXED_ADDITION,
    DOMAIN,
)
from . import export


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    energy_sensor_id = entry.data[CONF_ENERGY_SENSOR]
    buttons = [ExportMonthButton(hass, entry, energy_sensor_id)]

    cost_sensors = hass.data[DOMAIN][entry.entry_id].get("cost_sensors", {})
    for period, cost_sensor in cost_sensors.items():
        buttons.append(ResetCostButton(hass, entry, energy_sensor_id, period, cost_sensor))

    async_add_entities(buttons)


class ExportMonthButton(ButtonEntity):
    """Button that exports last completed month's bookkeeping report."""

    _attr_icon = "mdi:file-export"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, energy_sensor_id: str) -> None:
        self.hass = hass
        self._entry = entry
        self._energy_sensor_id = energy_sensor_id

        source_name = energy_sensor_id.split(".")[-1].replace("_", " ").title()
        self._attr_name = f"{source_name} Export Month"
        self._attr_unique_id = f"{entry.entry_id}_{energy_sensor_id.replace('.', '_')}_export_button"

    async def async_press(self) -> None:
        """Export the last completed month directly via the export module."""
        now = dt_util.now()
        if now.month == 1:
            target = now.replace(year=now.year - 1, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(month=now.month - 1, day=1, hour=0, minute=0, second=0, microsecond=0)

        price_sensor_id = self._entry.data[CONF_PRICE_SENSOR]
        fixed_addition = self._entry.options.get(
            CONF_FIXED_ADDITION, self._entry.data.get(CONF_FIXED_ADDITION, 0.0)
        )

        result = await export.export_month(
            self.hass,
            self._entry.entry_id,
            self._energy_sensor_id,
            price_sensor_id,
            fixed_addition,
            target.date().isoformat(),
            "both",
        )

        last_export = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("last_export")
        if last_export is not None:
            last_export.apply_export_result(result)


class ResetCostButton(ButtonEntity):
    """Button that resets a specific period cost sensor to zero."""

    _attr_icon = "mdi:undo"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        energy_sensor_id: str,
        period: str,
        cost_sensor,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._period = period
        self._cost_sensor = cost_sensor

        source_name = energy_sensor_id.split(".")[-1].replace("_", " ").title()
        self._attr_name = f"{source_name} Reset {period}"
        self._attr_unique_id = (
            f"{entry.entry_id}_{energy_sensor_id.replace('.', '_')}_reset_{period.lower()}"
        )

    async def async_press(self) -> None:
        """Reset the target cost sensor to zero."""
        await self._cost_sensor.async_reset()