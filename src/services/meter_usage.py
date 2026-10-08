"""Electricity meter usage analytics.

Readings are cumulative kWh totals. Daily usage is derived by spreading each
interval's delta evenly across the calendar days it spans (half-open interval
[prev_date, cur_date)). The full delta therefore exactly equals the sum of all
daily usage values, which makes the daily series consistent with the monthly
totals already produced by the analytics endpoint.
"""
from datetime import date, datetime, timedelta, timezone
from statistics import mean

SRI_LANKA_TIME = timezone(timedelta(hours=5, minutes=30), name="Asia/Colombo")
METERS = ("meter_1", "meter_2")


def _lkt_date(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SRI_LANKA_TIME).date()


def _round(value: float) -> float:
    return round(float(value), 3)


def _linear_regression(values):
    """Least-squares fit y = a + b*x over point indices. Returns fit dict or None."""
    n = len(values)
    if n < 2 or all(v == values[0] for v in values):
        return None
    xs = list(range(n))
    ys = [float(v) for v in values]
    mean_x = mean(xs)
    mean_y = mean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx == 0:
        return None
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    r_squared = (sxy * sxy) / (sxx * syy) if sxx and syy else 1.0
    return {
        "kwh_per_day": _round(slope),
        "r_squared": _round(max(0.0, min(1.0, r_squared))),
        "direction": "up" if slope > 1e-9 else ("down" if slope < -1e-9 else "flat"),
    }


def _moving_average(values, window: int):
    """Trailing moving average of `window` points; values before window are None."""
    result = []
    for index, value in enumerate(values):
        if index + 1 < window:
            result.append(None)
        else:
            result.append(_round(mean(values[index - window + 1 : index + 1])))
    return result


def _meter_metrics(meter_id: str, usage: dict[date, float], readings, names: dict[str, str]):
    metrics = {
        "meter_id": meter_id,
        "name": names.get(meter_id) or f"Meter {meter_id[-1]}",
        "readings": len(readings),
        "reset_detected": False,
        "warnings": [],
        "total_usage_kwh": 0.0,
        "measured_days": 0,
        "span_days": 0,
        "avg_daily_kwh": None,
        "peak_day": None,
        "latest_reading_kwh": None,
        "latest_reading_date": None,
        "previous_reading_kwh": None,
        "last_interval_kwh": None,
        "trend": None,
    }
    if not readings:
        return metrics
    if len(readings) < 2:
        metrics["warnings"].append("Add another reading for this meter to see daily usage and trend.")

    latest = readings[-1]
    metrics["latest_reading_kwh"] = float(latest["reading_value"])
    metrics["latest_reading_date"] = _lkt_date(latest["recorded_at"]).isoformat()
    if len(readings) >= 2:
        metrics["previous_reading_kwh"] = float(readings[-2]["reading_value"])
        metrics["last_interval_kwh"] = _round(
            metrics["latest_reading_kwh"] - metrics["previous_reading_kwh"]
        )

    dates = sorted(usage)
    if not dates:
        return metrics
    values = [usage[d] for d in dates]
    total = sum(values)
    metrics["total_usage_kwh"] = _round(total)
    metrics["measured_days"] = len(dates)
    metrics["span_days"] = (dates[-1] - dates[0]).days + 1
    metrics["avg_daily_kwh"] = _round(total / len(dates))

    peak_index = max(range(len(values)), key=values.__getitem__)
    metrics["peak_day"] = {"date": dates[peak_index].isoformat(), "kwh": _round(values[peak_index])}
    metrics["trend"] = _linear_regression(values)
    return metrics


def compute_meter_usage(readings: list[dict], names: dict[str, str] | None = None) -> dict:
    """Compute daily usage, moving averages, trends and summary stats.

    readings: dicts with meter_id, reading_value, recorded_at (datetime).
    names: optional mapping meter_id -> display name.
    """
    names = names or {}
    by_meter: dict[str, list[dict]] = {"meter_1": [], "meter_2": []}
    for doc in readings:
        meter_id = doc.get("meter_id")
        if meter_id in by_meter:
            by_meter[meter_id].append(doc)

    usage_by_meter = {"meter_1": {}, "meter_2": {}}
    reset_by_meter = {"meter_1": False, "meter_2": False}
    interval_notes = {"meter_1": [], "meter_2": []}

    for meter_id in METERS:
        docs = sorted(by_meter[meter_id], key=lambda d: d["recorded_at"])
        previous = None
        for doc in docs:
            current_value = float(doc["reading_value"])
            current_date = _lkt_date(doc["recorded_at"])
            if previous is None:
                previous = (current_value, current_date)
                continue
            previous_value, previous_date = previous
            delta = current_value - previous_value
            if delta < 0:
                reset_by_meter[meter_id] = True
                interval_notes[meter_id].append(
                    f"A reading on {current_date} was lower than its previous reading; "
                    "this interval was ignored (possible meter reset)."
                )
                previous = (current_value, current_date)
                continue
            days_between = (current_date - previous_date).days
            interval_usage = usage_by_meter[meter_id]
            if days_between <= 0:
                interval_usage[current_date] = interval_usage.get(current_date, 0.0) + delta
            else:
                per_day = delta / days_between
                for offset in range(days_between):
                    day = previous_date + timedelta(days=offset)
                    interval_usage[day] = interval_usage.get(day, 0.0) + per_day
            previous = (current_value, current_date)

        if reset_by_meter[meter_id]:
            interval_notes[meter_id].append("One or more readings indicated a meter reset.")
        by_meter[meter_id] = docs

    meter_stats = {
        meter_id: _meter_metrics(meter_id, usage_by_meter[meter_id], by_meter[meter_id], names)
        for meter_id in METERS
    }
    for meter_id in METERS:
        meter_stats[meter_id]["reset_detected"] = reset_by_meter[meter_id]
        meter_stats[meter_id]["warnings"].extend(interval_notes[meter_id])

    all_dates = sorted(set().union(*[set(usage_by_meter[meter_id]) for meter_id in METERS]))
    days = []
    for day in all_dates:
        rows = {}
        for meter_id in METERS:
            rows[meter_id] = usage_by_meter[meter_id].get(day)
        days.append({"date": day.isoformat(), "meter_1": rows["meter_1"], "meter_2": rows["meter_2"]})

    ma_windows = 7
    for meter_id in METERS:
        values = [usage_by_meter[meter_id].get(date.fromisoformat(row["date"])) for row in days]
        moving = _moving_average([v if v is not None else 0.0 for v in values], ma_windows)
        for row, average in zip(days, moving):
            row[f"{meter_id}_ma{ma_windows}"] = average

    combined = {}
    for row in days:
        value = (row["meter_1"] or 0.0) + (row["meter_2"] or 0.0)
        combined[row["date"]] = value
    for row in days:
        row["combined"] = _round(combined[row["date"]])

    combined_values = [combined[row["date"]] for row in days]
    combined_ma = _moving_average(combined_values, ma_windows)
    for row, average in zip(days, combined_ma):
        row[f"combined_ma{ma_windows}"] = average

    combined_peak_date = max(days, key=lambda row: row["combined"]).get("date", None) if days else None
    combined_peak = max(combined_values) if combined_values else 0.0
    combined_total = sum(combined_values) if combined_values else 0.0
    combined_days = len(days)

    return {
        "meters": {
            meter_id: {**meter_stats[meter_id], "warnings": list(dict.fromkeys(meter_stats[meter_id]["warnings"]))}
            for meter_id in METERS
        },
        "days": days,
        "combined": {
            "total_usage_kwh": _round(combined_total),
            "measured_days": combined_days,
            "avg_daily_kwh": _round(combined_total / combined_days) if combined_days else None,
            "peak_day": {"date": combined_peak_date, "kwh": _round(combined_peak)} if combined_peak_date else None,
            "trend": _linear_regression(combined_values) if len(combined_values) >= 2 else None,
        },
    }