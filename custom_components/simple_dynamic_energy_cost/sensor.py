import logging
import voluptuous as vol
from homeassistant.components.sensor import SensorStateClass, RestoreSensor
from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change, async_call_later
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
    SERVICE_EXPORT_MONTH,
    SERVICE_SEND_TEST_NOTIFICATION,
    ATTR_START_DATE,
)
from . import export, notify

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
    
    cost_sensors = {}

    cost_sensors["Accumulated"] = DynamicCostSensor(hass, entry.entry_id, "Accumulated", energy_sensor_id, price_sensor_id, fixed_addition)

    if entry.data.get(CONF_PERIOD_HOURLY):
        cost_sensors["Hourly"] = DynamicCostSensor(hass, entry.entry_id, "Hourly", energy_sensor_id, price_sensor_id, fixed_addition)

    if entry.data.get(CONF_PERIOD_DAILY):
        cost_sensors["Daily"] = DynamicCostSensor(hass, entry.entry_id, "Daily", energy_sensor_id, price_sensor_id, fixed_addition)

    if entry.data.get(CONF_PERIOD_MONTHLY):
        cost_sensors["Monthly"] = DynamicCostSensor(hass, entry.entry_id, "Monthly", energy_sensor_id, price_sensor_id, fixed_addition)

    if entry.data.get(CONF_PERIOD_YEARLY):
        cost_sensors["Yearly"] = DynamicCostSensor(hass, entry.entry_id, "Yearly", energy_sensor_id, price_sensor_id, fixed_addition)

    sensors = list(cost_sensors.values())

    last_export = LastExportSensor(hass, entry.entry_id, energy_sensor_id)
    last_export.bind(price_sensor_id, fixed_addition)
    sensors.append(last_export)

    hass.data[DOMAIN][entry.entry_id]["cost_sensors"] = cost_sensors
    hass.data[DOMAIN][entry.entry_id]["last_export"] = last_export

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "reset",
        {},
        "async_reset",
    )

    platform.async_register_entity_service(
        SERVICE_EXPORT_MONTH,
        {
            vol.Required(ATTR_START_DATE): str,
            vol.Optional("format", default="both"): vol.In(["csv", "pdf", "both"]),
        },
        "async_export_month",
    )

    platform.async_register_entity_service(
        SERVICE_SEND_TEST_NOTIFICATION,
        {},
        "async_send_test_notification",
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
        self._entry_id = entry_id

        source_name = energy_sensor_id.split(".")[-1].replace("_", " ").title()

        self._attr_name = f"{source_name} Cost {period}"
        self._attr_unique_id = f"{entry_id}_{energy_sensor_id.replace('.', '_')}_{period.lower()}"
        self._state = 0.0
        self._last_known_price = None
        self._last_energy_ts = None
        self._price_changes = []

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

        # Seed last-known price from the current price sensor state, if any
        price_state = self.hass.states.get(self._price_sensor_id)
        if price_state is not None and price_state.state not in ("unknown", "unavailable"):
            try:
                self._last_known_price = float(price_state.state)
            except ValueError:
                self._last_known_price = None

        # Listen for energy sensor changes
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._energy_sensor_id], self._energy_state_changed
            )
        )

        # Track price sensor changes to keep last-known price fresh and to
        # record price-change timestamps for interval splitting.
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._price_sensor_id], self._price_state_changed
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
    def _price_state_changed(self, event):
        """Track price sensor updates to keep last-known price and record change times."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            price = float(new_state.state)
        except ValueError:
            return
        self._last_known_price = price
        self._price_changes.append((new_state.last_updated, price))

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

        interval_start = old_state.last_updated
        interval_end = new_state.last_updated

        # Use last-known price fallback when current price is unavailable
        price_state = self.hass.states.get(self._price_sensor_id)
        current_price = None
        if price_state is not None and price_state.state not in ("unknown", "unavailable"):
            try:
                current_price = float(price_state.state)
            except ValueError:
                current_price = None
        if current_price is None:
            current_price = self._last_known_price
        if current_price is None:
            # No price has ever been seen; drop this delta to match
            # the reconstruction's behavior when no price timeline exists.
            return

        # Split the interval at price changes recorded between the previous
        # and current energy readings. Energy is apportioned uniformly over
        # the interval by elapsed time.
        cost_delta = self._cost_for_interval(
            energy_delta, interval_start, interval_end, current_price
        )

        # Keep the latest price change at or before interval_start (so the next
        # interval knows its starting price) and any changes at/after interval_end.
        kept = []
        for ts, px in self._price_changes:
            if ts <= interval_start:
                kept = [(ts, px)]
            elif ts >= interval_end:
                kept.append((ts, px))
        self._price_changes = kept

        self._state += cost_delta
        self.async_write_ha_state()

    def _cost_for_interval(self, energy_delta, interval_start, interval_end, fallback_price):
        """Apportion energy_delta across price-change sub-intervals.

        Energy is assumed uniformly consumed over [interval_start, interval_end].
        The starting price is the last recorded price change at or before
        interval_start (or fallback_price if none). Price-change timestamps
        falling strictly inside the interval split it; each subsequent
        sub-interval uses the price set at its starting boundary.
        """
        total_seconds = (interval_end - interval_start).total_seconds()
        if total_seconds <= 0:
            return energy_delta * (fallback_price + self._fixed_addition)

        # Determine the price effective at interval_start
        starting_price = fallback_price
        for ts, price in self._price_changes:
            if ts <= interval_start:
                starting_price = price
            else:
                break

        # Build segment boundaries: (timestamp, price_effective_from_this_timestamp)
        segments = [(interval_start, starting_price)]
        for ts, price in self._price_changes:
            if interval_start < ts < interval_end:
                segments.append((ts, price))
        segments.append((interval_end, None))

        cost = 0.0
        for i in range(1, len(segments)):
            seg_start = segments[i - 1][0]
            seg_price = segments[i - 1][1]
            seg_end = segments[i][0]
            seg_seconds = (seg_end - seg_start).total_seconds()
            if seg_seconds <= 0:
                continue
            seg_energy = energy_delta * (seg_seconds / total_seconds)
            cost += seg_energy * (seg_price + self._fixed_addition)
        return cost

    @callback
    def _reset(self, time):
        """Reset the sensor state to zero."""
        if self._period == "Monthly":
            self._save_monthly_summary(time)
        self._state = 0.0
        self.async_write_ha_state()

    def _save_monthly_summary(self, time):
        """Save the just-ended month's summary to storage (fire-and-forget).

        Delayed slightly so the recorder has committed the final state change
        of the month before we query history.
        """
        now = dt_util.now()

        @callback
        def _do_save(_now):
            if now.month == 1:
                month_start = now.replace(year=now.year - 1, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)
            else:
                month_start = now.replace(month=now.month - 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            try:
                self.hass.async_create_task(
                    export.compute_month_summary(
                        self.hass,
                        self._entry_id,
                        self._energy_sensor_id,
                        self._price_sensor_id,
                        self._fixed_addition,
                        month_start,
                    )
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to save monthly summary")

        unsub = async_call_later(self.hass, 5, _do_save)
        self.async_on_remove(unsub)

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


class LastExportSensor(RestoreSensor):
    """Sensor showing the last exported month report total and download links."""

    _attr_icon = "mdi:file-download"
    _attr_should_poll = False

    def __init__(self, hass, entry_id, energy_sensor_id):
        self.hass = hass
        self._entry_id = entry_id
        self._energy_sensor_id = energy_sensor_id
        self._price_sensor_id = None
        self._fixed_addition = 0.0

        source_name = energy_sensor_id.split(".")[-1].replace("_", " ").title()
        self._attr_name = f"{source_name} Last Export"
        self._attr_unique_id = f"{entry_id}_{energy_sensor_id.replace('.', '_')}_last_export"
        self._attr_extra_state_attributes = {}

    def bind(self, price_sensor_id: str, fixed_addition: float) -> None:
        self._price_sensor_id = price_sensor_id
        self._fixed_addition = fixed_addition

    @property
    def native_value(self):
        return self._attr_extra_state_attributes.get("total_cost")

    @property
    def native_unit_of_measurement(self):
        return self.hass.config.currency

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        state = await self.async_get_last_sensor_data()
        if state and state.native_value is not None:
            self._attr_extra_state_attributes["total_cost"] = float(state.native_value)
            self.async_write_ha_state()

    async def async_export_month(self, **kwargs) -> None:
        """Service handler: export a month report to CSV/PDF in www/."""
        start_date = kwargs[ATTR_START_DATE]
        fmt = kwargs.get("format", "both")
        result = await export.export_month(
            self.hass,
            self._entry_id,
            self._energy_sensor_id,
            self._price_sensor_id,
            self._fixed_addition,
            start_date,
            fmt,
        )
        self.apply_export_result(result)

    async def async_send_test_notification(self, **kwargs) -> None:
        """Service handler: send the latest monthly export notification now."""
        entry = self.hass.data[DOMAIN][self._entry_id].get("entry")
        if entry is None:
            return
        await notify.async_send_test_notification(self.hass, entry)

    def apply_export_result(self, result: dict) -> None:
        """Update sensor state/attributes from an export result."""
        summary = result["summary"]
        files = result["files"]
        attrs = dict(self._attr_extra_state_attributes)
        attrs["month_key"] = summary["month_key"]
        attrs["period"] = f"{summary['period_start']} till {summary['period_end']}"
        attrs["device_name"] = summary["device_name"]
        attrs["energy_consumed_kwh"] = summary["energy_consumed_kwh"]
        attrs["spot_cost"] = summary["spot_cost"]
        attrs["fixed_cost"] = summary["fixed_cost"]
        attrs["total_cost"] = summary["total_cost"]
        attrs["currency"] = summary["currency"]
        urls = {}
        for f, info in files.items():
            urls[f] = info["absolute_url"]
        attrs["files"] = urls
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()