import asyncio
import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MONTHLY_SUMMARIES_KEY

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.{MONTHLY_SUMMARIES_KEY}"

_MONTH_FMT = "%Y-%m"


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).lower()


def _device_name(hass: HomeAssistant, energy_sensor_id: str) -> str:
    state = hass.states.get(energy_sensor_id)
    if state is not None and state.name:
        return state.name
    return energy_sensor_id.split(".")[-1].replace("_", " ").title()


def _month_key(start_dt: datetime) -> str:
    return start_dt.strftime(_MONTH_FMT)


def _parse_date(value: str) -> datetime:
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return parsed


def _month_bounds(start_dt: datetime) -> tuple[datetime, datetime]:
    start = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _build_price_timeline(states) -> list[tuple[datetime, float]]:
    timeline = []
    for st in states:
        price = _to_float(st.state)
        if price is not None:
            timeline.append((st.last_updated, price))
    timeline.sort(key=lambda x: x[0])
    return timeline


def _price_at(timeline, when) -> float | None:
    result = None
    for ts, price in timeline:
        if ts > when:
            break
        result = price
    return result


def _segment_cost(
    energy_delta: float,
    interval_start: datetime,
    interval_end: datetime,
    price_timeline: list[tuple[datetime, float]],
    fixed_addition: float,
) -> tuple[float, float, float] | None:
    """Apportion energy_delta over [interval_start, interval_end] split by price changes.

    Energy is assumed uniformly consumed over the interval. The price effective
    for a sub-interval is the last known price at its start (from price_timeline);
    if none is known, the sub-interval is dropped entirely (matching the live
    sensor's behavior when no price has ever been seen).

    Returns (energy, spot, fixed) contributions, or None if no pricing is
    available for any part of the interval.
    """
    total_seconds = (interval_end - interval_start).total_seconds()
    if total_seconds <= 0:
        price = _price_at(price_timeline, interval_end)
        if price is None:
            return None
        return (energy_delta, energy_delta * price, energy_delta * fixed_addition)

    # Collect price changes that fall strictly inside the interval; each marks
    # a boundary. The price for a sub-interval starting at boundary ts is the
    # last known price at ts (i.e. the price set at ts if it's a change, or the
    # most recent prior price otherwise).
    boundaries = [interval_start]
    for ts, _price in price_timeline:
        if interval_start < ts < interval_end:
            boundaries.append(ts)
    boundaries.append(interval_end)

    energy = 0.0
    spot = 0.0
    fixed = 0.0
    for i in range(1, len(boundaries)):
        seg_start = boundaries[i - 1]
        seg_end = boundaries[i]
        seg_seconds = (seg_end - seg_start).total_seconds()
        if seg_seconds <= 0:
            continue
        price = _price_at(price_timeline, seg_start)
        if price is None:
            continue
        seg_energy = energy_delta * (seg_seconds / total_seconds)
        energy += seg_energy
        spot += seg_energy * price
        fixed += seg_energy * fixed_addition

    if energy == 0.0:
        return None
    return (energy, spot, fixed)


async def _reconstruct(
    hass: HomeAssistant,
    energy_sensor_id: str,
    price_sensor_id: str,
    fixed_addition: float,
    start_dt: datetime,
    end_dt: datetime,
) -> dict:
    energy_states = await hass.async_add_executor_job(
        history.get_significant_states, hass, start_dt, end_dt, [energy_sensor_id]
    )
    energy_list = energy_states.get(energy_sensor_id, []) if energy_states else []

    price_states = await hass.async_add_executor_job(
        history.get_significant_states, hass, start_dt, end_dt, [price_sensor_id]
    )
    price_list = price_states.get(price_sensor_id, []) if price_states else []
    price_timeline = _build_price_timeline(price_list)

    total_energy = 0.0
    total_spot = 0.0
    total_fixed = 0.0

    for i in range(1, len(energy_list)):
        old_val = _to_float(energy_list[i - 1].state)
        new_val = _to_float(energy_list[i].state)
        if old_val is None or new_val is None:
            continue
        if new_val >= old_val:
            delta = new_val - old_val
        else:
            delta = new_val
        if delta <= 0:
            continue
        result = _segment_cost(
            delta,
            energy_list[i - 1].last_updated,
            energy_list[i].last_updated,
            price_timeline,
            fixed_addition,
        )
        if result is None:
            continue
        total_energy += result[0]
        total_spot += result[1]
        total_fixed += result[2]

    total = total_spot + total_fixed

    return {
        "energy_consumed_kwh": round(total_energy, 4),
        "spot_cost": round(total_spot, 2),
        "fixed_cost": round(total_fixed, 2),
        "total_cost": round(total, 2),
    }


def _store(hass: HomeAssistant) -> storage.Store:
    return storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)


async def load_summaries(hass: HomeAssistant) -> dict:
    data = await _store(hass).async_load() or {}
    return data.get(MONTHLY_SUMMARIES_KEY, {}) if isinstance(data, dict) else {}


async def save_summary(hass: HomeAssistant, entry_id: str, summary: dict) -> None:
    data = await _store(hass).async_load() or {}
    if not isinstance(data, dict):
        data = {}
    summaries = data.setdefault(MONTHLY_SUMMARIES_KEY, {})
    entry_summaries = summaries.setdefault(entry_id, {})
    entry_summaries[summary["month_key"]] = summary
    await _store(hass).async_save(data)


async def get_summary(
    hass: HomeAssistant, entry_id: str, month_key: str
) -> dict | None:
    summaries = await load_summaries(hass)
    return summaries.get(entry_id, {}).get(month_key)


async def compute_month_summary(
    hass: HomeAssistant,
    entry_id: str,
    energy_sensor_id: str,
    price_sensor_id: str,
    fixed_addition: float,
    start_dt: datetime,
) -> dict:
    month_start, month_end = _month_bounds(start_dt)
    month_key = _month_key(month_start)
    existing = await get_summary(hass, entry_id, month_key)

    reconstructed = await _reconstruct(
        hass, energy_sensor_id, price_sensor_id, fixed_addition, month_start, month_end
    )

    if reconstructed["energy_consumed_kwh"] == 0.0 and existing is not None:
        return existing

    summary = {
        "month_key": month_key,
        "period_start": month_start.date().isoformat(),
        "period_end": (month_end - timedelta(days=1)).date().isoformat(),
        "energy_sensor": energy_sensor_id,
        "device_name": _device_name(hass, energy_sensor_id),
        "currency": hass.config.currency or "SEK",
        **reconstructed,
    }
    await save_summary(hass, entry_id, summary)
    return summary


def _csv_bytes(summary: dict) -> bytes:
    cur = summary["currency"]
    energy = str(summary["energy_consumed_kwh"]).replace(".", ",")
    spot = str(summary["spot_cost"]).replace(".", ",")
    fixed = str(summary["fixed_cost"]).replace(".", ",")
    total = str(summary["total_cost"]).replace(".", ",")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Period", f"{summary['period_start']} till {summary['period_end']}"])
    writer.writerow(["Enhet", summary["device_name"]])
    writer.writerow(["Total förbrukning", f"{energy} kWh"])
    writer.writerow(["Totalkostnad elhandel (Spotpris)", f"{spot} {cur}"])
    writer.writerow(["Totalkostnad rörlig elnätsavgift & energiskatt", f"{fixed} {cur}"])
    writer.writerow(["Totalt belopp", f"{total} {cur}"])
    return buf.getvalue().encode("utf-8-sig")


def _pdf_bytes(summary: dict) -> bytes:
    from fpdf import FPDF

    cur = summary["currency"]
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"Elförbrukning {summary['month_key']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    def _fmt(value: float, unit: str) -> str:
        return f"{value}".replace(".", ",") + " " + unit

    rows = [
        ("Period", f"{summary['period_start']} till {summary['period_end']}"),
        ("Enhet", summary["device_name"]),
        ("Total förbrukning", _fmt(summary["energy_consumed_kwh"], "kWh")),
        ("Totalkostnad elhandel (Spotpris)", _fmt(summary["spot_cost"], cur)),
        ("Totalkostnad rörlig elnätsavgift & energiskatt", _fmt(summary["fixed_cost"], cur)),
        ("Totalt belopp", _fmt(summary["total_cost"], cur)),
    ]

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(95, 9, "Fält", border=1, fill=True)
    pdf.cell(0, 9, "Värde", border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 12)
    for label, value in rows:
        if label == "Totalt belopp":
            pdf.set_font("Helvetica", "B", 12)
            pdf.set_fill_color(245, 245, 245)
            pdf.cell(95, 9, label, border=1, fill=True)
            pdf.cell(0, 9, value, border=1, fill=True)
            pdf.ln()
            pdf.set_font("Helvetica", "", 12)
            pdf.set_fill_color(255, 255, 255)
        else:
            pdf.cell(95, 9, label, border=1)
            pdf.cell(0, 9, value, border=1)
            pdf.ln()

    out = pdf.output()
    return bytes(out)


def _write_file(hass: HomeAssistant, filename: str, content: bytes) -> str:
    www_dir = hass.config.path("www")
    os.makedirs(www_dir, exist_ok=True)
    path = os.path.join(www_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return f"/local/{filename}"


def _public_url(hass: HomeAssistant, relative: str) -> str:
    base = hass.config.external_url or hass.config.internal_url or ""
    if base.endswith("/"):
        base = base[:-1]
    return f"{base}{relative}" if base else relative


async def export_month(
    hass: HomeAssistant,
    entry_id: str,
    energy_sensor_id: str,
    price_sensor_id: str,
    fixed_addition: float,
    start_date: str,
    fmt: str = "csv",
) -> dict:
    start_dt = _parse_date(start_date)
    summary = await compute_month_summary(
        hass, entry_id, energy_sensor_id, price_sensor_id, fixed_addition, start_dt
    )

    device_slug = _sanitize(summary["device_name"])
    base = f"energy_cost_{device_slug}_{summary['month_key']}"

    results = {}
    formats = [fmt] if fmt == "csv" or fmt == "pdf" else ["csv", "pdf"]
    for f in formats:
        if f == "csv":
            content = _csv_bytes(summary)
            ext = "csv"
        else:
            content = _pdf_bytes(summary)
            ext = "pdf"
        filename = f"{base}.{ext}"
        rel = await hass.async_add_executor_job(_write_file, hass, filename, content)
        results[f] = {"relative_url": rel, "absolute_url": _public_url(hass, rel)}

    return {"summary": summary, "files": results}