import voluptuous as vol
from homeassistant import config_entries
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
)

class DynamicEnergyCostConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

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