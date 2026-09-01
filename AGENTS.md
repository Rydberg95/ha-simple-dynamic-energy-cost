# AGENTS.md

Guidance for AI agents working in this repository.

## Project Overview

Home Assistant custom integration (`simple_dynamic_energy_cost`) that calculates accumulated energy cost from an accumulating energy sensor (kWh) and a dynamic price sensor (e.g. Nordpool). Produces one or more cost sensors per config entry: `Accumulated` (no reset), `Hourly`, `Daily`, `Monthly`, `Yearly`. Distributed via HACS.

## Repository Layout

```
custom_components/simple_dynamic_energy_cost/
  __init__.py        # Config entry setup/unload + options update listener
  config_flow.py     # Config + options flows (voluptuous schemas, entity selectors)
  const.py           # DOMAIN, CONF_*, and service/attribute constants — single source of truth
  sensor.py          # DynamicCostSensor (RestoreSensor), state tracking + resets;
                     # LastExportSensor (bookkeeping report link); entity services reset + export_month
  button.py          # ExportMonthButton — one-click export of last completed month
  export.py          # Monthly bookkeeping: HA storage persistence, recorder reconstruction,
                     # CSV + PDF writers (fpdf2), file output to www/
  manifest.json      # HA integration metadata (domain, version, iot_class, requirements)
  translations/en.json  # UI strings for config/options steps + services + button entity
hacs.json            # HACS metadata
README.md            # User-facing docs
```

There is no `tests/`, `requirements.txt`, `pyproject.toml`, lint, or typecheck configuration in this repo. Code targets the Home Assistant runtime — do not add standalone dependencies beyond what is already in `manifest.json` (`fpdf2` for PDF export). The HA core APIs are available at runtime via `homeassistant.*` imports; there is no local venv to install.

## Conventions

- **Language/runtime**: Python 3 (async). All entity/setup code is `async def`.
- **Constants**: Define new config keys in `const.py` and reference them everywhere via imports — never hardcode string keys in `sensor.py`/`config_flow.py`.
- **Schemas**: Use `voluptuous` and `homeassistant.helpers.selector` for config/options schemas. Mirror any new option in both `config_flow.py` and `translations/en.json` (under both `config.step.user.data` and, if user-configurable post-setup, `options.step.init.data`).
- **Sensors**: Subclass `RestoreSensor`. Persist state via `async_get_last_sensor_data()` in `async_added_to_hass`. Subscribe to state changes with `async_track_state_change_event` and time resets with `async_track_time_change`; always register cleanup via `self.async_on_remove(...)`.
- **Options flow**: Values that can change after setup (currently only `CONF_FIXED_ADDITION`) must be read from `entry.options` with fallback to `entry.data` (see pattern in `sensor.py:28-31` and `config_flow.py:66-69`). Adding a new editable option requires updating both places and the options schema, plus registering the `update_listener` reload (already wired in `__init__.py:15`).
- **Services**: Entity services are registered in `async_setup_entry` via `entity_platform.async_get_current_platform().async_register_entity_service(...)`. Existing services: `reset` -> `async_reset`; `export_month` -> `LastExportSensor.async_export_month` (writes CSV/PDF to `www/` and exposes download URLs on the sensor).
- **Energy sensor resets**: When the source energy sensor decreases (self-reset), treat `new_val` as the delta (see `sensor.py:140-144`).
- **Units**: Currency comes from `hass.config.currency`; do not hardcode a currency string.
- **Monthly bookkeeping**: `DynamicCostSensor` tracks running `_spot_total`/`_fixed_total`/`_energy_total` counters (persisted via `extra_state_attributes` and restored in `async_added_to_hass`). At the monthly boundary reset (and via a missed-boundary boot check), the Monthly sensor snapshots those live totals to storage via `export.build_live_summary` + `export.save_summary` with `"source": "live"` — this is the authoritative summary and never depends on recorder retention. `export.py` can also reconstruct a month by replaying recorder history (spot = energy × price, fixed = energy × fixed_addition); recorder replay is a fallback only. Summaries carry `complete`/`data_from`/`data_to` coverage fields; `compute_month_summary` never overwrites a `live` summary, never overwrites a `complete` summary with a partial reconstruction, and reports are annotated with a coverage warning row when incomplete (PDF/CSV, `LastExportSensor` attributes, ntfy message). Report files are written to `config/www/` and exposed as `/local/<filename>` URLs on the `LastExportSensor`.
- **No comments** in code unless requested.
- **No new dependencies**: `manifest.json` `requirements` contains `fpdf2` for PDF export — do not add other third-party packages without asking the user first.

## Making Changes

1. Read `const.py`, `config_flow.py`, `sensor.py`, and the relevant section of `translations/en.json` before editing — they are tightly coupled.
2. After editing config keys, confirm the same key name appears consistently in `const.py`, both schemas in `config_flow.py`, the `translations/en.json` data blocks, and the `sensor.py` reads.
3. Keep `manifest.json` `version` in sync if a release is intended (user will bump it).

## Verification

There is no automated test/lint/typecheck setup in this repo. To validate changes:

- Syntax check changed Python files:
  ```
  python -m py_compile custom_components/simple_dynamic_energy_cost/*.py
  ```
- Validate JSON files are well-formed:
  ```
  python -m json.tool custom_components/simple_dynamic_energy_cost/manifest.json
  python -m json.tool custom_components/simple_dynamic_energy_cost/translations/en.json
  python -m json.tool hacs.json
  ```
- For functional testing, load the integration in a Home Assistant instance (copy `custom_components/simple_dynamic_energy_cost/` into an HA config's `custom_components/` dir, restart, and add via Settings → Devices & Services). Confirm:
  - Config flow appears and accepts the energy/price selectors and period toggles.
  - Configured cost sensors are created with unique IDs of form `<entry_id>_<energy_sensor>_<period>`.
  - Energy sensor state changes increment the cost by `energy_delta * (current_price + fixed_addition)`.
  - Period reset triggers fire at the expected boundaries; `Accumulated` never resets.
  - The `reset` entity service sets state to `0.0`.
  - Options flow updates `fixed_addition` and reloads the entry.

If the user wants formal tests or linting added, ask before introducing tooling.