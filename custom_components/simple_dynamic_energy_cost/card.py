import logging

from homeassistant.core import HomeAssistant

from . import export

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "energy_cost_report_card.yaml"
CARD_URL = f"/local/{CARD_FILENAME}"

_CARD_TEMPLATE = r"""type: markdown
title: Energy cost reports
content: |
  {%- set report_sensor = states('__ENTITY_ID__') -%}
  {%- set reports = report_sensor.attributes.get('reports') if report_sensor else none -%}
  {%- if not reports %}
  No report exported yet. Press the **Export Month** button once to create a
  report for last month, or wait for the automatic export on the 1st.
  {%- else %}
  {%- for month in reports.keys() | sort(reverse=true) -%}
  {%- set report = reports[month] -%}
  {%- set total = report.get('total_cost') -%}
  {%- set energy = report.get('energy_consumed_kwh') -%}
  {%- set links = [] -%}
  {%- if report.get('pdf') -%}{%- set links = links + ['[PDF](' ~ report.get('pdf') ~ ')'] -%}{%- endif -%}
  {%- if report.get('csv') -%}{%- set links = links + ['[CSV](' ~ report.get('csv') ~ ')'] -%}{%- endif -%}
  {{- '- **' ~ month ~ '** - ' ~ (total ~ ' ' ~ report.get('currency', '') if total is not none else 'unknown cost') ~ ' - ' ~ (energy ~ ' kWh' if energy is not none else 'unknown consumption') }}{{ ' - incomplete data' if report.get('complete', true) is false else '' }}{{ ' - no spot/fixed split' if report.get('split_recovered', true) is false else '' }}{{ ': ' ~ (links | join(' | ') if links else 'no file') }}{{ '\n' -}}
  {%- endfor -%}
  {%- endif %}
"""


def render_card(entity_id: str) -> str:
    """Render the Lovelace markdown card for a Last Export sensor."""
    return _CARD_TEMPLATE.replace("__ENTITY_ID__", entity_id)


async def write_card(hass: HomeAssistant, entity_id: str) -> str:
    """Write the dashboard card snippet to www/, returning its URL."""
    if not entity_id:
        return CARD_URL

    content = render_card(entity_id).encode("utf-8")

    def _write() -> None:
        if export.www_file_exists(hass, CARD_FILENAME, content):
            return
        export.write_www_file(hass, CARD_FILENAME, content)

    try:
        await hass.async_add_executor_job(_write)
    except OSError as err:
        _LOGGER.error("Failed to write the dashboard card snippet: %s", err)

    return CARD_URL
