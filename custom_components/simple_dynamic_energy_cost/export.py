import csv
import io
import logging
import os
from datetime import datetime, timedelta

from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MONTHLY_SUMMARIES_KEY

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.{MONTHLY_SUMMARIES_KEY}"

_MONTH_FMT = "%Y-%m"

_COVERAGE_TOLERANCE = timedelta(hours=36)
_PRICE_LOOKBACK = timedelta(days=7)


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
    value = value.strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = dt_util.parse_datetime(value)
    if parsed is None:
        date_only = dt_util.parse_date(value)
        if date_only is not None:
            parsed = datetime.combine(date_only, datetime.min.time())
    if parsed is None:
        raise ValueError(f"Could not parse date: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    else:
        parsed = parsed.astimezone(dt_util.get_default_time_zone())
    return parsed


def _month_bounds(start_dt: datetime) -> tuple[datetime, datetime]:
    start = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def build_live_summary(
    hass: HomeAssistant,
    energy_sensor_id: str,
    month_start: datetime,
    energy_kwh: float,
    spot_cost: float,
    fixed_cost: float,
    total_cost: float,
    data_from: datetime | None = None,
) -> dict:
    """Build a month summary from live-tracked sensor totals (source of truth)."""
    period_start, period_end = _month_bounds(month_start)
    complete = data_from is not None and data_from <= period_start + _COVERAGE_TOLERANCE
    return {
        "month_key": _month_key(period_start),
        "period_start": period_start.date().isoformat(),
        "period_end": (period_end - timedelta(days=1)).date().isoformat(),
        "energy_sensor": energy_sensor_id,
        "device_name": _device_name(hass, energy_sensor_id),
        "currency": hass.config.currency or "SEK",
        "energy_consumed_kwh": round(energy_kwh, 4),
        "spot_cost": round(spot_cost, 2),
        "fixed_cost": round(fixed_cost, 2),
        "total_cost": round(total_cost, 2),
        "data_from": data_from.isoformat() if data_from else None,
        "complete": complete,
        "source": "live",
    }


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
        history.get_significant_states,
        hass,
        start_dt - _PRICE_LOOKBACK,
        end_dt,
        [price_sensor_id],
    )
    price_list = price_states.get(price_sensor_id, []) if price_states else []
    price_timeline = _build_price_timeline(price_list)

    total_energy = 0.0
    total_spot = 0.0
    total_fixed = 0.0
    first_ts = None
    last_ts = None
    prev_val = None
    prev_ts = None

    for st in energy_list:
        val = _to_float(st.state)
        if val is None:
            continue
        ts = st.last_updated
        if first_ts is None:
            first_ts = ts
        last_ts = ts
        if prev_val is not None:
            if val >= prev_val:
                delta = val - prev_val
            else:
                delta = val
            if delta > 0:
                result = _segment_cost(
                    delta, prev_ts, ts, price_timeline, fixed_addition
                )
                if result is not None:
                    total_energy += result[0]
                    total_spot += result[1]
                    total_fixed += result[2]
        prev_val = val
        prev_ts = ts

    head_covered = first_ts is not None and first_ts <= start_dt + _COVERAGE_TOLERANCE
    tail_covered = last_ts is not None and last_ts >= end_dt - _COVERAGE_TOLERANCE
    price_head_covered = (
        bool(price_timeline) and price_timeline[0][0] <= start_dt + _COVERAGE_TOLERANCE
    )
    complete = head_covered and tail_covered and price_head_covered

    total = total_spot + total_fixed

    return {
        "energy_consumed_kwh": round(total_energy, 4),
        "spot_cost": round(total_spot, 2),
        "fixed_cost": round(total_fixed, 2),
        "total_cost": round(total, 2),
        "complete": complete,
        "data_from": first_ts.isoformat() if first_ts else None,
        "data_to": last_ts.isoformat() if last_ts else None,
    }


def _store(hass: HomeAssistant) -> storage.Store:
    return storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)


async def _stats_energy(
    hass: HomeAssistant,
    energy_sensor_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> float | None:
    """Total energy consumed in [start_dt, end_dt) from long-term statistics."""
    try:
        from homeassistant.components.recorder import statistics as stats_mod
    except ImportError:
        return None
    end_inclusive = end_dt - timedelta(seconds=1)
    try:
        rows = await hass.async_add_executor_job(
            stats_mod.statistics_during_period,
            hass,
            start_dt,
            end_inclusive,
            {energy_sensor_id},
            "day",
            None,
            {"change"},
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to fetch statistics for %s", energy_sensor_id)
        return None
    stat_rows = (rows or {}).get(energy_sensor_id) or []
    total = 0.0
    found = False
    for row in stat_rows:
        change = row.get("change")
        if change is None:
            continue
        total += change
        found = True
    if not found or total < 0:
        return None
    return total


async def _reconstruct_from_cost_sensor(
    hass: HomeAssistant,
    entry_id: str,
    energy_sensor_id: str,
    fixed_addition: float,
    month_start: datetime,
    month_end: datetime,
) -> dict | None:
    """Rebuild the month from the Monthly cost sensor's own recorded history.

    The Monthly sensor is a running total that starts at 0 at the month
    boundary, so its last recorded state before month end is the month total
    regardless of how much history the recorder has purged. Only the tail of
    the month needs to survive.
    """
    cost_sensor = (
        hass.data.get(DOMAIN, {}).get(entry_id, {}).get("cost_sensors", {}).get("Monthly")
    )
    entity_id = getattr(cost_sensor, "entity_id", None)
    if not entity_id:
        return None

    states = await hass.async_add_executor_job(
        history.get_significant_states, hass, month_start, month_end, [entity_id]
    )
    state_list = states.get(entity_id, []) if states else []

    last_val = None
    last_ts = None
    last_attrs = None
    for st in state_list:
        val = _to_float(st.state)
        if val is None:
            continue
        last_val = val
        last_ts = st.last_updated
        last_attrs = st.attributes

    if last_val is None or last_ts is None:
        return None
    if last_ts < month_end - _COVERAGE_TOLERANCE:
        return None

    total_cost = last_val
    energy = None
    spot = None
    fixed = None
    split_recovered = False

    if last_attrs:
        energy = _to_float(last_attrs.get("energy_consumed_kwh"))
        spot = _to_float(last_attrs.get("spot_cost"))
        fixed = _to_float(last_attrs.get("fixed_cost"))
        if energy is not None and spot is not None and fixed is not None:
            split_recovered = True

    if not split_recovered:
        energy = await _stats_energy(hass, energy_sensor_id, month_start, month_end)
        if energy is not None:
            fixed = energy * fixed_addition
            spot = total_cost - fixed
            split_recovered = True
        else:
            energy = None
            spot = None
            fixed = None

    return {
        "energy_consumed_kwh": round(energy, 4) if energy is not None else None,
        "spot_cost": round(spot, 2) if spot is not None else None,
        "fixed_cost": round(fixed, 2) if fixed is not None else None,
        "total_cost": round(total_cost, 2),
        "complete": True,
        "split_recovered": split_recovered,
        "data_from": month_start.isoformat(),
        "data_to": last_ts.isoformat(),
    }


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

    if existing is not None and existing.get("source") == "live":
        return existing

    reconstructed = await _reconstruct_from_cost_sensor(
        hass, entry_id, energy_sensor_id, fixed_addition, month_start, month_end
    )
    source = "cost_sensor"
    if reconstructed is None:
        reconstructed = await _reconstruct(
            hass, energy_sensor_id, price_sensor_id, fixed_addition, month_start, month_end
        )
        source = "recorder"

    summary = {
        "month_key": month_key,
        "period_start": month_start.date().isoformat(),
        "period_end": (month_end - timedelta(days=1)).date().isoformat(),
        "energy_sensor": energy_sensor_id,
        "device_name": _device_name(hass, energy_sensor_id),
        "currency": hass.config.currency or "SEK",
        "source": source,
        **reconstructed,
    }

    if not summary["complete"]:
        _LOGGER.warning(
            "Reconstructed summary for %s has incomplete data coverage (%s to %s)",
            month_key,
            summary["data_from"],
            summary["data_to"],
        )

    if existing is not None:
        if existing.get("complete") and not summary["complete"]:
            return existing
        if (
            existing.get("complete")
            and existing.get("split_recovered", True)
            and summary.get("split_recovered") is False
        ):
            return existing
        if summary["energy_consumed_kwh"] == 0.0:
            return existing

    current = await get_summary(hass, entry_id, month_key)
    if current is not None and current.get("source") == "live":
        return current

    await save_summary(hass, entry_id, summary)
    return summary


def _coverage_warning(summary: dict) -> str | None:
    if summary.get("complete", False):
        return None
    data_from = summary.get("data_from")
    data_to = summary.get("data_to")
    if data_from and data_to:
        return f"Ofullständig historik: {data_from} till {data_to}"
    return "Ofullständig historik för perioden"


def _split_warning(summary: dict) -> str | None:
    if summary.get("split_recovered") is False:
        return "Fördelningen mellan spotpris och fast avgift kunde inte återställas"
    return None


def _report_warnings(summary: dict) -> list[str]:
    return [w for w in (_coverage_warning(summary), _split_warning(summary)) if w]


def _fmt_kwh(value) -> str:
    if value is None:
        return "okänd"
    return f"{str(value).replace('.', ',')} kWh"


def _fmt_cur(value, cur: str) -> str:
    if value is None:
        return "okänd"
    return f"{str(value).replace('.', ',')} {cur}"


def _csv_bytes(summary: dict) -> bytes:
    cur = summary["currency"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Period", f"{summary['period_start']} till {summary['period_end']}"])
    writer.writerow(["Enhet", summary["device_name"]])
    writer.writerow(["Total förbrukning", _fmt_kwh(summary["energy_consumed_kwh"])])
    writer.writerow(["Totalkostnad elhandel (Spotpris)", _fmt_cur(summary["spot_cost"], cur)])
    writer.writerow(["Totalkostnad rörlig elnätsavgift & energiskatt", _fmt_cur(summary["fixed_cost"], cur)])
    writer.writerow(["Totalt belopp", _fmt_cur(summary["total_cost"], cur)])
    for warning in _report_warnings(summary):
        writer.writerow(["Varning", warning])
    return buf.getvalue().encode("utf-8-sig")


def _pdf_bytes(summary: dict) -> bytes:
    from fpdf import FPDF

    cur = summary["currency"]
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"Elförbrukning {summary['month_key']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    rows = [
        ("Period", f"{summary['period_start']} till {summary['period_end']}"),
        ("Enhet", summary["device_name"]),
        ("Total förbrukning", _fmt_kwh(summary["energy_consumed_kwh"])),
        ("Totalkostnad elhandel (Spotpris)", _fmt_cur(summary["spot_cost"], cur)),
        ("Totalkostnad rörlig elnätsavgift & energiskatt", _fmt_cur(summary["fixed_cost"], cur)),
        ("Totalt belopp", _fmt_cur(summary["total_cost"], cur)),
    ]
    for warning in _report_warnings(summary):
        rows.append(("Varning", warning))

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


async def write_month_files(
    hass: HomeAssistant, summary: dict, fmt: str = "both"
) -> dict:
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

    return results


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

    files = await write_month_files(hass, summary, fmt)
    return {"summary": summary, "files": files}