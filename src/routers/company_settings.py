from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import require_capability
from ..database.main_db import (
    company_settings_collection,
    electricity_meter_readings_collection,
)
from ..models import CompanySettingsUpdate, ElectricityMeterReadingCreate, ElectricityMeterReadingUpdate
from ..router_utils import serialize, log_audit, parse_object_id
from ..services.electricity_formula import (
    DEFAULT_ELECTRICITY_COST_FORMULA,
    ElectricityFormulaError,
    electricity_formula_variables,
    evaluate_electricity_formula,
    validate_electricity_formula,
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
    "electricity_cost_formula": DEFAULT_ELECTRICITY_COST_FORMULA,
    "electricity_unit_rate_lkr": None,
    "electricity_fixed_charge_lkr": 0.0,
    "electricity_tax_rate": 0.0,
}

ADMIN_ELECTRICITY_SETTINGS = {
    "electricity_meter_1_name",
    "electricity_meter_2_name",
    "electricity_cost_formula",
    "electricity_unit_rate_lkr",
    "electricity_fixed_charge_lkr",
    "electricity_tax_rate",
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


def _lkt_month(doc: dict) -> str:
    return _recorded_at_utc(doc).astimezone(SRI_LANKA_TIME).strftime("%Y-%m")


def _new_month_summary(month: str) -> dict:
    return {
        "month": month,
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
            detail="Only an admin can configure electricity meters and their cost formula.",
        )

    existing = await company_settings_collection().find_one({"key": "main"})
    updates = payload.model_dump(exclude_none=True)
    unset_fields = {}
    if "electricity_unit_rate_lkr" in fields_set and payload.electricity_unit_rate_lkr is None:
        unset_fields["electricity_unit_rate_lkr"] = ""
    if "electricity_cost_formula" in updates:
        try:
            updates["electricity_cost_formula"] = validate_electricity_formula(
                updates["electricity_cost_formula"]
            )
        except ElectricityFormulaError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

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
    formula = settings.get(
        "electricity_cost_formula", DEFAULT_ELECTRICITY_COST_FORMULA
    )
    configured_unit_rate = settings.get("electricity_unit_rate_lkr")
    unit_rate = (
        float(configured_unit_rate) if configured_unit_rate is not None else None
    )
    fixed_charge = float(settings.get("electricity_fixed_charge_lkr", 0) or 0)
    tax_rate = float(settings.get("electricity_tax_rate", 0) or 0)
    configuration_error = None
    try:
        formula_variables = electricity_formula_variables(formula)
    except ElectricityFormulaError as exc:
        formula_variables = frozenset()
        configuration_error = str(exc)
    if "unit_rate_lkr" in formula_variables and unit_rate is None:
        configuration_error = "Set the electricity unit rate before calculating LKR amounts."

    cursor = electricity_meter_readings_collection().find({}).sort(
        [("meter_id", 1), ("recorded_at", 1)]
    )
    readings = [doc async for doc in cursor]
    grouped: dict[str, dict] = {}
    readings_by_meter = {"meter_1": [], "meter_2": []}
    seen_months_by_meter = {"meter_1": set(), "meter_2": set()}

    for doc in readings:
        readings_by_meter[doc["meter_id"]].append(doc)
        month = _lkt_month(doc)
        grouped.setdefault(month, _new_month_summary(month))

    for meter_id, docs in readings_by_meter.items():
        current_key = f"{meter_id}_current"
        previous_key = f"{meter_id}_previous"
        units_key = f"{meter_id}_units"
        previous_doc = None

        for doc in docs:
            month = _lkt_month(doc)
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
            if any(_lkt_month(doc) == month for doc in docs):
                continue
            earlier_docs = [doc for doc in docs if _lkt_month(doc) < month]
            if earlier_docs:
                latest = float(earlier_docs[-1]["reading_value"])
                summary[f"{meter_id}_current"] = latest
                summary[f"{meter_id}_previous"] = latest

        if configuration_error:
            summary["errors"].append(configuration_error)
        elif summary["has_interval"] and not summary["errors"]:
            variables = {
                "meter_1_current": summary["meter_1_current"],
                "meter_1_previous": summary["meter_1_previous"],
                "meter_1_units": summary["meter_1_units"],
                "meter_2_current": summary["meter_2_current"],
                "meter_2_previous": summary["meter_2_previous"],
                "meter_2_units": summary["meter_2_units"],
                "unit_rate_lkr": unit_rate or 0.0,
                "fixed_charge_lkr": fixed_charge,
                "tax_rate": tax_rate,
            }
            try:
                summary["amount_lkr"] = round(
                    evaluate_electricity_formula(formula, variables), 2
                )
            except ElectricityFormulaError as exc:
                summary["errors"].append(str(exc))

        if not configuration_error:
            for meter_id in ("meter_1", "meter_2"):
                if not summary[f"{meter_id}_has_interval"]:
                    continue
                meter_variables = {
                    "meter_1_current": summary["meter_1_current"] if meter_id == "meter_1" else 0.0,
                    "meter_1_previous": summary["meter_1_previous"] if meter_id == "meter_1" else 0.0,
                    "meter_1_units": summary["meter_1_units"] if meter_id == "meter_1" else 0.0,
                    "meter_2_current": summary["meter_2_current"] if meter_id == "meter_2" else 0.0,
                    "meter_2_previous": summary["meter_2_previous"] if meter_id == "meter_2" else 0.0,
                    "meter_2_units": summary["meter_2_units"] if meter_id == "meter_2" else 0.0,
                    "unit_rate_lkr": unit_rate or 0.0,
                    "fixed_charge_lkr": 0.0,
                    "tax_rate": 0.0,
                }
                try:
                    summary[f"{meter_id}_amount_lkr"] = round(
                        evaluate_electricity_formula(formula, meter_variables), 2
                    )
                except ElectricityFormulaError as exc:
                    summary["errors"].append(
                        f"{settings.get(f'electricity_{meter_id}_name', meter_id)}: {exc}"
                    )

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
        "formula": formula,
        "unit_rate_lkr": unit_rate,
        "fixed_charge_lkr": fixed_charge,
        "tax_rate": tax_rate,
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
