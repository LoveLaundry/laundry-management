from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import require_capability
from ..database.main_db import (
    company_settings_collection,
    electricity_meter_readings_collection,
)
from ..models import CompanySettingsUpdate, ElectricityMeterReadingCreate, ElectricityMeterReadingUpdate
from ..router_utils import serialize, log_audit, parse_object_id
from ..services.electricity_slabs import (
    ElectricitySlabError,
    calculate_slab_cost,
    normalize_unit_slabs,
)
from ..services.electricity_projection import (
    ElectricityProjectionError,
    clamp_start_day,
    compute_projection,
    parse_day,
    period_bounds,
)
from ..services.meter_usage import compute_meter_usage

router = APIRouter(tags=["Company Settings"])
SRI_LANKA_TIME = timezone(timedelta(hours=5, minutes=30), name="Asia/Colombo")

DEFAULT_SETTINGS = {
    "company_name": "Love Laundry",
    "working_days_per_week": 6,
    "working_days_pattern": [0, 1, 2, 3, 4, 5],
    "default_overtime_rate": 0,
    "salary_basis_days": 30,
    "electricity_meter_1_name": "Chilaw Connection Line",
    "electricity_meter_2_name": "Madampe Connection Line",
    "electricity_unit_slabs": [],
    "electricity_tax_rate": 0.0,
    "electricity_billing_cycle_start_day": 1,
}

ADMIN_ELECTRICITY_SETTINGS = {
    "electricity_meter_1_name",
    "electricity_meter_2_name",
    "electricity_unit_slabs",
    "electricity_tax_rate",
    "electricity_billing_cycle_start_day",
}


def _meter_reading_response(doc: dict) -> dict:
    recorded_at = doc["recorded_at"]
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    corrections = []
    for c in doc.get("corrections") or []:
        corrected_at = c.get("corrected_at")
        if isinstance(corrected_at, datetime):
            if corrected_at.tzinfo is None:
                corrected_at = corrected_at.replace(tzinfo=timezone.utc)
            corrected_at = corrected_at.astimezone(SRI_LANKA_TIME)
        corrections.append(
            {
                "old_value": c.get("old_value"),
                "new_value": c.get("new_value"),
                "reason": c.get("reason"),
                "corrected_by": c.get("corrected_by"),
                "corrected_at": corrected_at,
            }
        )
    last = corrections[-1] if corrections else None
    return {
        "id": str(doc["_id"]),
        "meter_id": doc["meter_id"],
        "meter_name": doc["meter_name"],
        "reading_value": doc["reading_value"],
        "recorded_at": recorded_at.astimezone(SRI_LANKA_TIME),
        "created_by": doc.get("created_by"),
        "correction_reason": last.get("reason") if last else None,
        "corrections": corrections,
    }


def _recorded_at_utc(doc: dict) -> datetime:
    recorded_at = doc["recorded_at"]
    if recorded_at.tzinfo is None:
        return recorded_at.replace(tzinfo=timezone.utc)
    return recorded_at.astimezone(timezone.utc)


def _period_id(doc: dict, start_day: int) -> str:
    day = _recorded_at_utc(doc).astimezone(SRI_LANKA_TIME).date()
    return period_bounds(day, start_day)[0].isoformat()


def _new_month_summary(period_start: str, period_end: str) -> dict:
    return {
        "month": period_start,
        "period_start": period_start,
        "period_end": period_end,
        "meter_1_current": 0.0,
        "meter_1_previous": 0.0,
        "meter_1_units": 0.0,
        "meter_2_current": 0.0,
        "meter_2_previous": 0.0,
        "meter_2_units": 0.0,
        "has_interval": False,
        "meter_1_has_interval": False,
        "meter_2_has_interval": False,
        "errors": [],
        "warnings": [],
        "amount_lkr": None,
        "meter_1_amount_lkr": None,
        "meter_2_amount_lkr": None,
        "meter_1_cost": None,
        "meter_2_cost": None,
    }


@router.get("/company-settings")
async def get_settings(
    current_user: dict = Depends(require_capability("employee:read")),
):
    doc = await company_settings_collection().find_one({"key": "main"})
    if doc:
        return {**DEFAULT_SETTINGS, **serialize(doc, [])}
    return {
        "key": "main",
        **DEFAULT_SETTINGS,
    }


@router.put("/company-settings")
async def update_settings(
    payload: CompanySettingsUpdate,
    current_user: dict = Depends(require_capability("employee:write")),
):
    fields_set = payload.model_fields_set
    if ADMIN_ELECTRICITY_SETTINGS & fields_set and str(current_user.get("role", "")).upper() != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can configure electricity meters and their tariff slabs.",
        )

    existing = await company_settings_collection().find_one({"key": "main"})
    updates = payload.model_dump(exclude_none=True)
    unset_fields = {}
    if "electricity_unit_slabs" in updates and payload.electricity_unit_slabs is not None:
        slab_dicts = [slab.model_dump() for slab in payload.electricity_unit_slabs]
        try:
            normalize_unit_slabs(slab_dicts)
        except ElectricitySlabError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        updates["electricity_unit_slabs"] = slab_dicts

    for field in ("electricity_meter_1_name", "electricity_meter_2_name"):
        if field in updates:
            updates[field] = updates[field].strip()
            if not updates[field]:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Electricity meter names cannot be blank.",
                )

    if not existing:
        doc = {"key": "main", **DEFAULT_SETTINGS, **updates, "updated_at": datetime.now(timezone.utc)}
        result = await company_settings_collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        await log_audit(
            str(current_user.get("user_id", "")),
            "create", "company_settings", "main", details=updates,
        )
        return serialize(doc, [])

    updates["updated_at"] = datetime.now(timezone.utc)
    update_operation = {"$set": updates}
    if unset_fields:
        update_operation["$unset"] = unset_fields
    await company_settings_collection().update_one({"key": "main"}, update_operation)
    audit_details = {**updates, **{field: None for field in unset_fields}}
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "company_settings", "main", details=audit_details,
    )
    updated = await company_settings_collection().find_one({"key": "main"})
    return {**DEFAULT_SETTINGS, **serialize(updated, [])}


@router.get("/company-settings/electricity-meter-readings")
async def list_electricity_meter_readings(
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(require_capability("employee:read")),
):
    cursor = (
        electricity_meter_readings_collection()
        .find({})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    return [_meter_reading_response(doc) async for doc in cursor]


@router.get("/company-settings/electricity-meter-analytics")
async def electricity_meter_analytics(
    current_user: dict = Depends(require_capability("employee:read")),
):
    settings = await company_settings_collection().find_one({"key": "main"}) or {}
    slabs_raw = settings.get("electricity_unit_slabs") or []
    tax_rate = float(settings.get("electricity_tax_rate", 0) or 0)
    start_day = clamp_start_day(settings.get("electricity_billing_cycle_start_day", 1))
    configuration_error = None
    try:
        slabs = normalize_unit_slabs(slabs_raw)
    except ElectricitySlabError as exc:
        slabs = []
        configuration_error = str(exc)

    cursor = electricity_meter_readings_collection().find({}).sort(
        [("meter_id", 1), ("recorded_at", 1)]
    )
    readings = [doc async for doc in cursor]
    grouped: dict[str, dict] = {}
    readings_by_meter = {"meter_1": [], "meter_2": []}
    seen_months_by_meter = {"meter_1": set(), "meter_2": set()}

    for doc in readings:
        readings_by_meter[doc["meter_id"]].append(doc)
        month = _period_id(doc, start_day)
        if month not in grouped:
            bounds = period_bounds(date.fromisoformat(month), start_day)
            grouped[month] = _new_month_summary(bounds[0].isoformat(), bounds[1].isoformat())

    for meter_id, docs in readings_by_meter.items():
        current_key = f"{meter_id}_current"
        previous_key = f"{meter_id}_previous"
        units_key = f"{meter_id}_units"
        previous_doc = None

        for doc in docs:
            month = _period_id(doc, start_day)
            summary = grouped[month]
            current_value = float(doc["reading_value"])
            if month not in seen_months_by_meter[meter_id]:
                summary[previous_key] = (
                    float(previous_doc["reading_value"])
                    if previous_doc is not None
                    else current_value
                )
                if previous_doc is None:
                    summary["warnings"].append(
                        f"{settings.get(f'electricity_{meter_id}_name', meter_id)} "
                        "has no earlier reading baseline."
                    )
                seen_months_by_meter[meter_id].add(month)

            summary[current_key] = current_value

            if previous_doc is not None:
                previous_value = float(previous_doc["reading_value"])
                delta = current_value - previous_value
                if delta < 0:
                    summary["errors"].append(
                        f"{settings.get(f'electricity_{meter_id}_name', meter_id)} "
                        "reading is lower than its previous reading; check for a meter reset."
                    )
                else:
                    summary[units_key] += delta
                summary["has_interval"] = True
                if delta >= 0:
                    summary[f"{meter_id}_has_interval"] = True

            previous_doc = doc

    month_keys = sorted(grouped)
    for month in month_keys:
        summary = grouped[month]
        for meter_id, docs in readings_by_meter.items():
            if any(_period_id(doc, start_day) == month for doc in docs):
                continue
            earlier_docs = [doc for doc in docs if _period_id(doc, start_day) < month]
            if earlier_docs:
                latest = float(earlier_docs[-1]["reading_value"])
                summary[f"{meter_id}_current"] = latest
                summary[f"{meter_id}_previous"] = latest

        if configuration_error:
            summary["errors"].append(configuration_error)
        elif summary["has_interval"] and not summary["errors"]:
            meter_amounts = []
            for meter_id in ("meter_1", "meter_2"):
                if not summary[f"{meter_id}_has_interval"]:
                    continue
                cost = calculate_slab_cost(summary[f"{meter_id}_units"], slabs, tax_rate)
                summary[f"{meter_id}_cost"] = cost
                summary[f"{meter_id}_amount_lkr"] = cost["amount_lkr"]
                meter_amounts.append(cost["amount_lkr"])
            summary["amount_lkr"] = round(sum(meter_amounts), 2) if meter_amounts else None

        summary["total_units"] = round(
            summary["meter_1_units"] + summary["meter_2_units"], 3
        )
        summary["meter_1_units"] = round(summary["meter_1_units"], 3)
        summary["meter_2_units"] = round(summary["meter_2_units"], 3)
        summary["warnings"] = list(dict.fromkeys(summary["warnings"]))

    chart_readings = []
    for doc in readings:
        chart_readings.append(
            {
                "meter_id": doc["meter_id"],
                "meter_name": doc["meter_name"],
                "reading_value": doc["reading_value"],
                "recorded_at": _recorded_at_utc(doc).astimezone(SRI_LANKA_TIME),
            }
        )
    chart_readings.sort(key=lambda reading: reading["recorded_at"])

    return {
        "unit_slabs": slabs,
        "tax_rate": tax_rate,
        "billing_cycle_start_day": start_day,
        "configuration_error": configuration_error,
        "months": [grouped[month] for month in month_keys],
        "readings": chart_readings,
    }


@router.get("/company-settings/electricity-meter-usage")
async def electricity_meter_usage(
    current_user: dict = Depends(require_capability("employee:read")),
):
    settings = await company_settings_collection().find_one({"key": "main"}) or {}
    names = {
        "meter_1": settings.get("electricity_meter_1_name") or "Meter 1",
        "meter_2": settings.get("electricity_meter_2_name") or "Meter 2",
    }
    cursor = electricity_meter_readings_collection().find({}).sort(
        [("meter_id", 1), ("recorded_at", 1)]
    )
    readings = [doc async for doc in cursor]
    return compute_meter_usage(readings, names)


@router.get("/company-settings/electricity-meter-projection")
async def electricity_meter_projection(
    start: str | None = Query(default=None, description="Window start as YYYY-MM-DD (LKT)"),
    end: str | None = Query(default=None, description="Window end as YYYY-MM-DD (LKT)"),
    current_user: dict = Depends(require_capability("employee:read")),
):
    settings = await company_settings_collection().find_one({"key": "main"}) or {}
    names = {
        "meter_1": settings.get("electricity_meter_1_name") or "Meter 1",
        "meter_2": settings.get("electricity_meter_2_name") or "Meter 2",
    }
    slabs_raw = settings.get("electricity_unit_slabs") or []
    tax_rate = float(settings.get("electricity_tax_rate", 0) or 0)
    start_day = clamp_start_day(settings.get("electricity_billing_cycle_start_day", 1))
    configuration_error = None
    try:
        slabs = normalize_unit_slabs(slabs_raw)
    except ElectricitySlabError as exc:
        slabs = []
        configuration_error = str(exc)

    try:
        window_start = parse_day(start, "Start date") if start else None
        window_end = parse_day(end, "End date") if end else None
        if (window_start is None) != (window_end is None):
            raise ElectricityProjectionError(
                "Set both the start and end dates, or leave both empty for the current billing month."
            )
    except ElectricityProjectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    cursor = electricity_meter_readings_collection().find({}).sort(
        [("meter_id", 1), ("recorded_at", 1)]
    )
    readings = [doc async for doc in cursor]
    today = datetime.now(SRI_LANKA_TIME).date()
    try:
        projection = compute_projection(
            readings, names, slabs or None, tax_rate,
            start_day, window_start, window_end, today,
        )
    except ElectricityProjectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    projection["configuration_error"] = configuration_error
    return projection


@router.post(
    "/company-settings/electricity-meter-readings",
    status_code=status.HTTP_201_CREATED,
)
async def create_electricity_meter_reading(
    payload: ElectricityMeterReadingCreate,
    current_user: dict = Depends(require_capability("employee:write")),
):
    if str(current_user.get("role", "")).upper() != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can add electricity meter readings.",
        )

    settings = await company_settings_collection().find_one({"key": "main"}) or {}
    meter_number = payload.meter_id[-1]
    meter_name = settings.get(f"electricity_meter_{meter_number}_name") or f"Meter {meter_number}"
    recorded_at_utc = payload.recorded_at.astimezone(timezone.utc)
    doc = {
        "meter_id": payload.meter_id,
        "meter_name": meter_name,
        "reading_value": payload.reading_value,
        "recorded_at": recorded_at_utc,
        "created_by": str(current_user.get("user_id", "")),
        "created_at": datetime.now(timezone.utc),
    }
    result = await electricity_meter_readings_collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    await log_audit(
        str(current_user.get("user_id", "")),
        "create",
        "electricity_meter_reading",
        str(result.inserted_id),
        details={
            "meter_id": payload.meter_id,
            "meter_name": meter_name,
            "reading_value": payload.reading_value,
            "recorded_at": recorded_at_utc.astimezone(SRI_LANKA_TIME).isoformat(),
        },
    )
    return _meter_reading_response(doc)


@router.put("/company-settings/electricity-meter-readings/{reading_id}")
async def update_electricity_meter_reading(
    reading_id: str,
    payload: ElectricityMeterReadingUpdate,
    current_user: dict = Depends(require_capability("employee:write")),
):
    if str(current_user.get("role", "")).upper() != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can correct electricity meter readings.",
        )

    oid = parse_object_id(reading_id, "meter reading")
    doc = await electricity_meter_readings_collection().find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Electricity meter reading not found.",
        )

    old_value = float(doc["reading_value"])
    if old_value == payload.reading_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The reading value has not changed.",
        )

    now = datetime.now(timezone.utc)
    new_correction = {
        "old_value": old_value,
        "new_value": payload.reading_value,
        "reason": payload.reason,
        "corrected_by": str(current_user.get("user_id", "")),
        "corrected_at": now,
    }
    await electricity_meter_readings_collection().update_one(
        {"_id": oid},
        {
            "$set": {
                "reading_value": payload.reading_value,
                "updated_at": now,
                "last_correction": new_correction,
            },
            "$push": {"corrections": new_correction},
        },
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "update",
        "electricity_meter_reading",
        str(oid),
        details={
            "meter_id": doc["meter_id"],
            "meter_name": doc["meter_name"],
            "old_value": old_value,
            "new_value": payload.reading_value,
            "reason": payload.reason,
        },
    )
    updated = await electricity_meter_readings_collection().find_one({"_id": oid})
    return _meter_reading_response(updated)
