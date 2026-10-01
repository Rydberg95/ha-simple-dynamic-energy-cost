import asyncio
import logging
import re
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
    CONF_NOTIFY_VERIFY_SSL,
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

_INVALID_TOPIC_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_TOPIC_NAME_LIMIT = 64


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


def _option(entry, key, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _notify_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Serialise sends so the scheduled and hourly timers cannot double-send."""
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
    lock = entry_data.get("notify_lock")
    if lock is None:
        lock = asyncio.Lock()
        entry_data["notify_lock"] = lock
    return lock


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


def _clean_topics(topics) -> list[str]:
    """Keep only topic names ntfy accepts: [-_A-Za-z0-9], up to 64 characters."""
    if isinstance(topics, str):
        topics = [topics]

    cleaned: list[str] = []
    for topic in topics or []:
        name = _INVALID_TOPIC_CHARS.sub("-", str(topic).strip())[:_TOPIC_NAME_LIMIT]
        if not name or not re.search(r"[A-Za-z0-9_]", name):
            _LOGGER.warning(
                "Skipping invalid ntfy topic %r: use only letters, numbers, "
                "underscores and dashes",
                topic,
            )
            continue
        if name != str(topic):
            _LOGGER.warning("Adjusted ntfy topic %r to %r", topic, name)
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def _ascii_header(value: str) -> str:
    """HTTP headers only carry latin-1, so keep them ASCII and single line."""
    cleaned = " ".join(value.encode("ascii", "ignore").decode("ascii").split())
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return "Energy cost report"
    return cleaned


def _headers(title: str, files: dict) -> dict:
    headers = {"Title": _ascii_header(title), "Tags": "money"}
    actions = []
    primary = None
    for key, label in (("pdf", "Open PDF report"), ("csv", "Open CSV report")):
        info = files.get(key) or {}
        if not info.get("is_absolute"):
            continue
        if primary is None:
            primary = info["absolute_url"]
        actions.append(f"view, {label}, {info['absolute_url']}, clear=true")
    if primary is not None:
        headers["Click"] = primary
    if actions:
        headers["Actions"] = "; ".join(actions)
    return headers


async def async_send_ntfy(
    hass: HomeAssistant,
    server: str,
    topics: list[str],
    title: str,
    message: str,
    files: dict | None = None,
    verify_ssl: bool = True,
) -> bool:
    session = async_get_clientsession(hass)
    base = (server or DEFAULT_NOTIFY_SERVER).rstrip("/")
    headers = _headers(title, files or {})
    all_ok = True

    for topic in topics:
        url = f"{base}/{topic}"
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


async def _record_notify(
    hass: HomeAssistant,
    entry,
    last_export,
    entry_state: dict,
    ok: bool,
    error: str | None,
    month_key: str | None,
    mark_notified: bool,
) -> bool:
    if error and entry_state.get("last_notify_error") != error:
        _LOGGER.error("%s: %s", entry.title, error)
    entry_state["last_notify_error"] = error

    if last_export is not None:
        last_export.apply_notify_result(
            {
                "ok": ok,
                "error": error,
                "month_key": month_key,
                "attempt": dt_util.now().isoformat(),
            }
        )

    if ok and mark_notified and month_key:
        await _mark_notified(hass, entry.entry_id, month_key)

    return ok


async def async_run_monthly_notify(
    hass: HomeAssistant, entry, mark_notified: bool = False
) -> bool:
    """Export the previous month and push it to ntfy. Returns delivery success."""
    entry_id = entry.entry_id
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
    last_export = entry_data.get("last_export")

    topics = _clean_topics(_option(entry, CONF_NOTIFY_TOPICS, []))
    if not topics:
        return await _record_notify(
            hass,
            entry,
            last_export,
            entry_data,
            False,
            "no valid ntfy topics configured",
            None,
            mark_notified,
        )

    energy_sensor_id = entry.data[CONF_ENERGY_SENSOR]
    price_sensor_id = entry.data[CONF_PRICE_SENSOR]
    fixed_addition = _option(entry, CONF_FIXED_ADDITION, 0.0)
    server = _option(entry, CONF_NOTIFY_SERVER, DEFAULT_NOTIFY_SERVER)
    verify_ssl = _option(entry, CONF_NOTIFY_VERIFY_SSL, True)

    month_start = previous_month_start(dt_util.now())
    summary = await export.compute_month_summary(
        hass, entry_id, energy_sensor_id, price_sensor_id, fixed_addition, month_start
    )
    files = await export.write_month_files(hass, summary, "both")
    await export.record_export_files(hass, entry_id, summary["month_key"], files)

    if last_export is not None:
        await last_export.apply_export_result({"summary": summary, "files": files})

    url_warning = export.missing_url_warning(hass)
    if url_warning:
        _LOGGER.warning("%s: %s", summary["month_key"], url_warning)

    cur = summary["currency"]
    title = (
        f"{summary['device_name']} energy cost {summary['month_key']}: "
        f"{summary['total_cost']} {cur}"
    )
    lines = []
    if summary.get("energy_consumed_kwh") is not None:
        lines.append(f"Consumption: {summary['energy_consumed_kwh']} kWh")
    if summary.get("spot_cost") is not None:
        lines.append(f"Spot price cost: {summary['spot_cost']} {cur}")
    if summary.get("fixed_cost") is not None:
        lines.append(f"Grid & tax: {summary['fixed_cost']} {cur}")
    lines.append(f"Total: {summary['total_cost']} {cur}")
    if summary.get("split_recovered") is False:
        lines.append("Warning: spot/fixed cost split could not be recovered")
    if not summary.get("complete", False):
        lines.append("Warning: data coverage for this report is incomplete")
    if url_warning:
        lines.append(f"Warning: {url_warning}")
    for fmt, label in (("pdf", "Report (PDF)"), ("csv", "Report (CSV)")):
        info = files.get(fmt)
        if info:
            lines.append(f"{label}: {info['absolute_url']}")
    message = "\n".join(lines)

    ok = await async_send_ntfy(hass, server, topics, title, message, files, verify_ssl)

    if not ok and mark_notified:
        _LOGGER.warning(
            "Report for %s was not delivered and will be retried",
            summary["month_key"],
        )

    return await _record_notify(
        hass,
        entry,
        last_export,
        entry_data,
        ok,
        None if ok else "ntfy delivery failed, see the log for details",
        summary["month_key"],
        mark_notified,
    )


async def async_send_test_notification(hass: HomeAssistant, entry) -> None:
    """Send the latest monthly export notification immediately, as if it were the 1st."""
    await async_run_monthly_notify(hass, entry, mark_notified=False)


async def async_check_and_notify(hass: HomeAssistant, entry) -> bool:
    """Send last month's report if it has not been delivered yet.

    Runs on start-up and hourly, so a missed notification window is picked up as
    soon as Home Assistant is back. The month is only marked as notified after a
    confirmed delivery, which makes a failed send retry instead of being lost.
    """
    if not _option(entry, CONF_NOTIFY_ENABLED, False):
        return False

    hour, minute = parse_notify_time(
        _option(entry, CONF_NOTIFY_TIME, DEFAULT_NOTIFY_TIME)
    )
    now = dt_util.now()
    if (now.hour, now.minute) < (hour, minute):
        return False

    month_key = previous_month_start(now).strftime(_MONTH_FMT)

    async with _notify_lock(hass, entry.entry_id):
        if await _was_notified(hass, entry.entry_id, month_key):
            return False

        _LOGGER.debug(
            "Monthly report for %s is still undelivered, sending now", month_key
        )
        return await async_run_monthly_notify(hass, entry, mark_notified=True)
