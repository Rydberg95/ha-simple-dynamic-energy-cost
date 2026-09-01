import asyncio
import logging
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_SENSOR,
    CONF_FIXED_ADDITION,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_SERVER,
    CONF_NOTIFY_TIME,
    CONF_NOTIFY_TOPICS,
    CONF_PRICE_SENSOR,
    DEFAULT_NOTIFY_SERVER,
    DEFAULT_NOTIFY_TIME,
    DOMAIN,
)
from . import export

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.notify_state"

_MONTH_FMT = "%Y-%m"


def _store(hass: HomeAssistant) -> storage.Store:
    return storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)


async def _load_state(hass: HomeAssistant) -> dict:
    data = await _store(hass).async_load() or {}
    return data if isinstance(data, dict) else {}


async def _was_notified(hass: HomeAssistant, entry_id: str, month_key: str) -> bool:
    data = await _load_state(hass)
    return data.get(entry_id, {}).get("notified_month") == month_key


async def _mark_notified(hass: HomeAssistant, entry_id: str, month_key: str) -> None:
    data = await _load_state(hass)
    entry_state = data.setdefault(entry_id, {})
    entry_state["notified_month"] = month_key
    await _store(hass).async_save(data)


def previous_month_start(now: datetime) -> datetime:
    if now.month == 1:
        return now.replace(year=now.year - 1, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month - 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def parse_notify_time(value) -> tuple[int, int]:
    if hasattr(value, "hour"):
        return value.hour, value.minute
    try:
        parts = str(value).split(":")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, AttributeError):
        default = DEFAULT_NOTIFY_TIME.split(":")
        return int(default[0]), int(default[1])


async def async_send_ntfy(
    hass: HomeAssistant,
    server: str,
    topics: list[str],
    title: str,
    message: str,
    click_url: str | None = None,
    verify_ssl: bool = True,
) -> bool:
    session = async_get_clientsession(hass)
    base = (server or DEFAULT_NOTIFY_SERVER).rstrip("/")
    all_ok = True

    for topic in topics:
        url = f"{base}/{topic}"
        headers = {"Title": title, "Tags": "money"}
        if click_url:
            headers["Actions"] = f"view, Open report, {click_url}, clear=true"
        try:
            async with asyncio.timeout(30):
                resp = await session.post(
                    url,
                    data=message.encode("utf-8"),
                    headers=headers,
                    ssl=False if not verify_ssl else None,
                )
                resp.raise_for_status()
        except Exception as err:  # noqa: BLE001
            all_ok = False
            _LOGGER.error("Failed to send ntfy notification to %s: %s", url, err)

    return all_ok


async def async_run_monthly_notify(
    hass: HomeAssistant, entry, mark_notified: bool = False
) -> None:
    entry_id = entry.entry_id
    energy_sensor_id = entry.data[CONF_ENERGY_SENSOR]
    price_sensor_id = entry.data[CONF_PRICE_SENSOR]
    fixed_addition = entry.options.get(
        CONF_FIXED_ADDITION, entry.data.get(CONF_FIXED_ADDITION, 0.0)
    )

    topics = entry.options.get(CONF_NOTIFY_TOPICS, entry.data.get(CONF_NOTIFY_TOPICS, []))
    if not topics:
        _LOGGER.info("Notify enabled but no ntfy topics configured; skipping")
        return
    server = entry.options.get(
        CONF_NOTIFY_SERVER, entry.data.get(CONF_NOTIFY_SERVER, DEFAULT_NOTIFY_SERVER)
    )
    verify_ssl = entry.options.get(
        CONF_NOTIFY_VERIFY_SSL, entry.data.get(CONF_NOTIFY_VERIFY_SSL, True)
    )

    now = dt_util.now()
    month_start = previous_month_start(now)

    summary = await export.compute_month_summary(
        hass, entry_id, energy_sensor_id, price_sensor_id, fixed_addition, month_start
    )
    files = await export.write_month_files(hass, summary, "both")

    last_export = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("last_export")
    if last_export is not None:
        last_export.apply_export_result({"summary": summary, "files": files})

    urls = {f: info["absolute_url"] for f, info in files.items()}
    pdf_url = urls.get("pdf")
    csv_url = urls.get("csv")
    cur = summary["currency"]
    title = f"{summary['device_name']} energy cost {summary['month_key']}: {summary['total_cost']} {cur}"
    lines = [
        f"Consumption: {summary['energy_consumed_kwh']} kWh",
        f"Spot price cost: {summary['spot_cost']} {cur}",
        f"Grid & tax: {summary['fixed_cost']} {cur}",
        f"Total: {summary['total_cost']} {cur}",
    ]
    if pdf_url:
        lines.append(f"Report (PDF): {pdf_url}")
    if csv_url:
        lines.append(f"Report (CSV): {csv_url}")
    message = "\n".join(lines)

    ok = await async_send_ntfy(
        hass, server, list(topics), title, message, pdf_url, verify_ssl
    )
    if not ok:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message + "\n\n(ntfy delivery failed; see logs)",
            },
            blocking=False,
        )

    if mark_notified:
        await _mark_notified(hass, entry_id, summary["month_key"])


async def async_send_test_notification(hass: HomeAssistant, entry) -> None:
    """Send the latest monthly export notification immediately, as if it were the 1st."""
    await async_run_monthly_notify(hass, entry, mark_notified=False)


async def async_catch_up_if_needed(hass: HomeAssistant, entry) -> None:
    enabled = entry.options.get(
        CONF_NOTIFY_ENABLED, entry.data.get(CONF_NOTIFY_ENABLED, False)
    )
    if not enabled:
        return

    now = dt_util.now()
    if now.day != 1:
        return

    hour, minute = parse_notify_time(
        entry.options.get(CONF_NOTIFY_TIME, entry.data.get(CONF_NOTIFY_TIME, DEFAULT_NOTIFY_TIME))
    )
    if (now.hour, now.minute) < (hour, minute):
        return

    month_start = previous_month_start(now)
    month_key = month_start.strftime(_MONTH_FMT)
    if await _was_notified(hass, entry.entry_id, month_key):
        return

    await async_run_monthly_notify(hass, entry, mark_notified=True)