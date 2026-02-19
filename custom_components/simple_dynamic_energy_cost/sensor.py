import logging
from homeassistant.components.sensor import SensorStateClass, RestoreSensor
from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_PRICE_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_PERIOD_HOURLY,
    CONF_PERIOD_DAILY,
    CONF_PERIOD_MONTHLY,
    CONF_PERIOD_YEARLY,
    CONF_FIXED_ADDITION,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up the sensor platform."""
    energy_sensor_id = entry.data[CONF_ENERGY_SENSOR]
    price_sensor_id = entry.data[CONF_PRICE_SENSOR]
    
    # Fetch from options first, fallback to initial setup data
    fixed_addition = entry.options.get(
        CONF_FIXED_ADDITION, 
        entry.data.get(CONF_FIXED_ADDITION, 0.0)
    )
    
    sensors = []
    
    sensors.append(DynamicCostSensor(hass, entry.entry_id, "Accumulated", energy_sensor_id, price_sensor_id, fixed_addition))
    
    if entry.data.get(CONF_PERIOD_HOURLY):
        sensors.append(DynamicCostSensor(hass, entry.entry_id, "Hourly", energy_sensor_id, price_sensor_id, fixed_addition))
        
    if entry.data.get(CONF_PERIOD_DAILY):
        sensors.append(DynamicCostSensor(hass, entry.entry_id, "Daily", energy_sensor_id, price_sensor_id, fixed_addition))
        
    if entry.data.get(CONF_PERIOD_MONTHLY):
        sensors.append(DynamicCostSensor(hass, entry.entry_id, "Monthly", energy_sensor_id, price_sensor_id, fixed_addition))

    if entry.data.get(CONF_PERIOD_YEARLY):
        sensors.append(DynamicCostSensor(hass, entry.entry_id, "Yearly", energy_sensor_id, price_sensor_id, fixed_addition))

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "reset",
        {},
        "async_reset",
    )

    async_add_entities(sensors)


class DynamicCostSensor(RestoreSensor):
    """Representation of a Dynamic Cost Sensor."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:currency-usd"
    _attr_should_poll = False

    def __init__(self, hass, entry_id, period, energy_sensor_id, price_sensor_id, fixed_addition):
        """Initialize the sensor."""
        self.hass = hass
        self._period = period
        self._energy_sensor_id = energy_sensor_id
        self._price_sensor_id = price_sensor_id
        self._fixed_addition = fixed_addition
        
        source_name = energy_sensor_id.split(".")[-1].replace("_", " ").title()
        
        self._attr_name = f"{source_name} Cost {period}"
        self._attr_unique_id = f"{entry_id}_{energy_sensor_id.replace('.', '_')}_{period.lower()}"
        self._state = 0.0

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return round(self._state, 4)

    @property
    def native_unit_of_measurement(self):
        """Use the default currency of the Home Assistant instance."""
        return self.hass.config.currency

    async def async_added_to_hass(self):
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        
        # Restore previous state
        state = await self.async_get_last_sensor_data()
        if state and state.native_value is not None:
            try:
                self._state = float(state.native_value)
            except ValueError:
                self._state = 0.0

        # Listen for energy sensor changes
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._energy_sensor_id], self._energy_state_changed
            )
        )

        # Set up reset timers based on period (Accumulated is left without a reset timer)
        if self._period == "Hourly":
            self.async_on_remove(async_track_time_change(self.hass, self._reset, minute=0, second=0))
        elif self._period == "Daily":
            self.async_on_remove(async_track_time_change(self.hass, self._reset, hour=0, minute=0, second=0))
        elif self._period == "Monthly":
            self.async_on_remove(async_track_time_change(self.hass, self._monthly_reset, hour=0, minute=0, second=0))
        elif self._period == "Yearly":
            self.async_on_remove(async_track_time_change(self.hass, self._yearly_reset, hour=0, minute=0, second=0))

    async def async_reset(self):
        """Manually reset the sensor state to zero."""
        self._state = 0.0
        self.async_write_ha_state()
        
    @callback
    def _energy_state_changed(self, event):
        """Handle energy sensor state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if old_state is None or new_state is None:
            return

        try:
            old_val = float(old_state.state)
            new_val = float(new_state.state)
        except ValueError:
            return

        # Calculate energy delta
        if new_val >= old_val:
            energy_delta = new_val - old_val
        else:
            # Handle case where the source energy sensor resets itself to 0
            energy_delta = new_val

        if energy_delta <= 0:
            return

        # Fetch current price
        price_state = self.hass.states.get(self._price_sensor_id)
        if price_state is None or price_state.state in ("unknown", "unavailable"):
            return

        try:
            current_price = float(price_state.state)
        except ValueError:
            return

        # Calculate cost and add to total
        cost_delta = energy_delta * (current_price + self._fixed_addition)
        self._state += cost_delta
        self.async_write_ha_state()

    @callback
    def _reset(self, time):
        """Reset the sensor state to zero."""
        self._state = 0.0
        self.async_write_ha_state()

    @callback
    def _monthly_reset(self, time):
        """Reset the sensor state if it is the first day of the month."""
        if time.day == 1:
            self._reset(time)
            
    @callback
    def _yearly_reset(self, time):
        """Reset the sensor state if it is the first day of the year."""
        if time.month == 1 and time.day == 1:
            self._reset(time)