"""Billing-period helpers and month-end projections.

A billing period is defined by the day of the month it starts on
(``electricity_billing_cycle_start_day``, 1-28). ``1`` means calendar months.
For example a start day of 15 means periods run 15th-14th. The projection
estimates the full-period consumption from the elapsed days: a least-squares
trend line fitted to the daily usage so far forecasts the remaining days
(trend-adjusted); with fewer than 3 elapsed days it falls back to the plain
run-rate average. Forecast days are floored at zero and actuals are always
kept as-is. For an ongoing window today's still-accumulating bucket is
excluded from the pace/trend fit and counted as part of the forecast.
"""
from datetime import date, timedelta

from .electricity_slabs import calculate_slab_cost
from .meter_usage import METERS, compute_meter_usage

MAX_RANGE_DAYS = 366


class ElectricityProjectionError(ValueError):
    """Raised when a projection window is invalid."""


def clamp_start_day(raw) -> int:
    try:
        day = int(raw)
    except (TypeError, ValueError):
        return 1
    return min(28, max(1, day))


def add_one_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, day.day)
    return date(day.year, day.month + 1, day.day)


def period_bounds(day: date, start_day: int) -> tuple:
    """Return the (start, end) inclusive billing period containing `day`."""
    start_day = clamp_start_day(start_day)
    if day.day >= start_day:
        start = date(day.year, day.month, start_day)
    else:
        previous = date(day.year, day.month, 1) - timedelta(days=1)
        start = date(previous.year, previous.month, start_day)
    end = add_one_month(start) - timedelta(days=1)
    return start, end


def parse_day(raw: str, label: str) -> date:
    try:
        parts = [int(p) for p in str(raw).split("-")]
        return date(parts[0], parts[1], parts[2])
    except (ValueError, IndexError, TypeError):
        raise ElectricityProjectionError(
            f"{label} must be a date like 2026-10-01."
        ) from None


def _fit_trend(values: list) -> tuple | None:
    """Least-squares slope/intercept over point indices; None if unusable."""
    n = len(values)
    if n < 3 or all(v == values[0] for v in values):
        return None
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in range(n))
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(values))
    if sxx == 0:
        return None
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    mean_y2 = sum(y * y for y in values) / n
    variance = max(mean_y2 - mean_y * mean_y, 0.0)
    r_squared = (slope * slope * sxx / n / variance) if variance > 0 else 1.0
    direction = "up" if slope > 1e-9 else ("down" if slope < -1e-9 else "flat")
    return slope, intercept, max(0.0, min(1.0, r_squared)), direction


def _round2(value: float) -> float:
    return round(float(value), 2)


def compute_projection(
    readings: list,
    names: dict,
    slabs: list | None,
    tax_rate: float,
    start_day: int,
    start: date | None,
    end: date | None,
    today: date,
) -> dict:
    """Project a billing window's month-end consumption and slab cost."""
    start_day = clamp_start_day(start_day)
    if start is None or end is None:
        start, end = period_bounds(today, start_day)
    if start > end:
        raise ElectricityProjectionError("The start date must not be after the end date.")
    total_days = (end - start).days + 1
    if total_days > MAX_RANGE_DAYS:
        raise ElectricityProjectionError(
            f"Choose a window of {MAX_RANGE_DAYS} days or fewer."
        )
    as_of = min(today, end)
    elapsed_days = (as_of - start).days + 1 if as_of >= start else 0
    if end >= today and as_of >= start:
        # Ongoing window: today's bucket is still accumulating, so pace and
        # trend use complete days only; today is part of the forecast.
        data_end = as_of - timedelta(days=1)
        remaining_days = (end - today).days + 1
    else:
        data_end = min(as_of, end)
        remaining_days = 0
    measured_days = (data_end - start).days + 1 if data_end >= start else 0

    usage = compute_meter_usage(readings, names)
    by_date = {row["date"]: row for row in usage["days"]}

    meters = {}
    for meter_id in METERS:
        daily = [
            float((by_date.get((start + timedelta(days=offset)).isoformat()) or {}).get(meter_id) or 0.0)
            for offset in range(measured_days)
        ]
        units_so_far = sum(daily)
        run_rate = units_so_far / measured_days if measured_days else 0.0
        fit = _fit_trend(daily)
        if fit is None:
            method = "run_rate"
            trend = None
            slope = None
            intercept = None
            forecast_days = [round(run_rate, 3)] * remaining_days
        else:
            slope, intercept, r_squared, direction = fit
            method = "trend"
            trend = {
                "kwh_per_day": round(slope, 3),
                "r_squared": round(r_squared, 3),
                "direction": direction,
            }
            forecast_days = [
                round(max(0.0, intercept + slope * (measured_days + ahead)), 3)
                for ahead in range(remaining_days)
            ]
        forecast = sum(forecast_days)
        projected_units = units_so_far + forecast
        projected_run_rate_units = units_so_far + run_rate * remaining_days
        series = []
        for offset in range(total_days):
            day_iso = (start + timedelta(days=offset)).isoformat()
            actual = round(daily[offset], 3) if offset < len(daily) else None
            trend_value = (
                round(max(0.0, intercept + slope * offset), 3)
                if slope is not None
                else None
            )
            ahead = offset - measured_days
            forecast_value = (
                forecast_days[ahead]
                if remaining_days and 0 <= ahead < len(forecast_days)
                else None
            )
            series.append(
                {
                    "date": day_iso,
                    "actual": actual,
                    "trend": trend_value,
                    "forecast": forecast_value,
                }
            )
        latest = usage["meters"][meter_id]
        entry = {
            "meter_id": meter_id,
            "name": latest["name"],
            "units_so_far": round(units_so_far, 3),
            "run_rate_kwh_per_day": _round2(run_rate),
            "method": method,
            "trend": trend,
            "projected_units": round(projected_units, 3),
            "projected_units_run_rate": round(projected_run_rate_units, 3),
            "last_reading_kwh": latest["latest_reading_kwh"],
            "last_reading_date": latest["latest_reading_date"],
            "cost_so_far": None,
            "projected_cost": None,
            "series": series,
        }
        if slabs:
            entry["cost_so_far"] = calculate_slab_cost(units_so_far, slabs, tax_rate)
            entry["projected_cost"] = calculate_slab_cost(projected_units, slabs, tax_rate)
        meters[meter_id] = entry

    def _sum(key: str) -> float:
        return round(sum(m[key] for m in meters.values()), 3)

    def _sum_cost(key: str, amount: str):
        costs = [m[key] for m in meters.values() if m[key]]
        if not costs:
            return None
        return _round2(sum(c[amount] for c in costs))

    combined_series = []
    for offset in range(total_days):
        row = {"date": (start + timedelta(days=offset)).isoformat()}
        for key in ("actual", "trend", "forecast"):
            values = [meters[meter_id]["series"][offset][key] for meter_id in METERS]
            row[key] = (
                round(sum(v for v in values if v is not None), 3)
                if any(v is not None for v in values)
                else None
            )
        combined_series.append(row)

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "billing_cycle_start_day": start_day,
        "as_of": as_of.isoformat() if elapsed_days else None,
        "elapsed_days": elapsed_days,
        "measured_days": measured_days,
        "total_days": total_days,
        "remaining_days": remaining_days,
        "is_current": start <= today <= end,
        "has_data": elapsed_days > 0 and any(m["units_so_far"] > 0 for m in meters.values()),
        "meters": meters,
        "combined": {
            "units_so_far": _sum("units_so_far"),
            "projected_units": _sum("projected_units"),
            "projected_units_run_rate": _sum("projected_units_run_rate"),
            "cost_so_far_lkr": _sum_cost("cost_so_far", "amount_lkr"),
            "projected_cost_lkr": _sum_cost("projected_cost", "amount_lkr"),
            "series": combined_series,
        },
    }
