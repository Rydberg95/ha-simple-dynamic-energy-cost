import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from .const import (
    DOMAIN,
    CONF_PRICE_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_PERIOD_HOURLY,
    CONF_PERIOD_DAILY,
    CONF_PERIOD_MONTHLY,
    CONF_PERIOD_YEARLY,
    CONF_FIXED_ADDITION,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TOPICS,
    CONF_NOTIFY_SERVER,
    CONF_NOTIFY_TIME,
    CONF_NOTIFY_VERIFY_SSL,
    DEFAULT_NOTIFY_SERVER,
    DEFAULT_NOTIFY_TIME,
)

class DynamicEnergyCostConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return DynamicEnergyCostOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title=f"Energy Cost ({user_input[CONF_ENERGY_SENSOR]})", 
                data=user_input
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ENERGY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(device_class="energy")
                ),
                vol.Required(CONF_PRICE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_FIXED_ADDITION, default=0.0): vol.Coerce(float),
                vol.Optional(CONF_PERIOD_HOURLY, default=False): bool,
                vol.Optional(CONF_PERIOD_DAILY, default=True): bool,
                vol.Optional(CONF_PERIOD_MONTHLY, default=True): bool,
                vol.Optional(CONF_PERIOD_YEARLY, default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

class DynamicEnergyCostOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Dynamic Energy Cost."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_fixed_addition = self._config_entry.options.get(
            CONF_FIXED_ADDITION,
            self._config_entry.data.get(CONF_FIXED_ADDITION, 0.0)
        )

        current_notify_enabled = self._config_entry.options.get(
            CONF_NOTIFY_ENABLED,
            self._config_entry.data.get(CONF_NOTIFY_ENABLED, False)
        )
        current_notify_topics = self._config_entry.options.get(
            CONF_NOTIFY_TOPICS,
            self._config_entry.data.get(CONF_NOTIFY_TOPICS, [])
        )
        current_notify_server = self._config_entry.options.get(
            CONF_NOTIFY_SERVER,
            self._config_entry.data.get(CONF_NOTIFY_SERVER, DEFAULT_NOTIFY_SERVER)
        )
        current_notify_time = self._config_entry.options.get(
            CONF_NOTIFY_TIME,
            self._config_entry.data.get(CONF_NOTIFY_TIME, DEFAULT_NOTIFY_TIME)
        )
        current_verify_ssl = self._config_entry.options.get(
            CONF_NOTIFY_VERIFY_SSL,
            self._config_entry.data.get(CONF_NOTIFY_VERIFY_SSL, True)
        )

        options_schema = vol.Schema(
            {
                vol.Optional(CONF_FIXED_ADDITION, default=current_fixed_addition): vol.Coerce(float),
                vol.Optional(CONF_NOTIFY_ENABLED, default=current_notify_enabled): bool,
                vol.Optional(CONF_NOTIFY_TOPICS, description={"suggested_value": current_notify_topics}): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        multiple=True,
                        custom_value=True,
                        options=[],
                    )
                ),
                vol.Optional(CONF_NOTIFY_SERVER, description={"suggested_value": current_notify_server}): str,
                vol.Optional(CONF_NOTIFY_TIME, description={"suggested_value": current_notify_time}): selector.TimeSelector(),
                vol.Optional(CONF_NOTIFY_VERIFY_SSL, default=current_verify_ssl): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema
        )