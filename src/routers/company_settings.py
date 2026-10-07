from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import require_capability
from ..database.main_db import (
    company_settings_collection,
    electricity_meter_readings_collection,
)
from ..models import CompanySettingsUpdate, ElectricityMeterReadingCreate
from ..router_utils import serialize, log_audit

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
}


def _meter_reading_response(doc: dict) -> dict:
    recorded_at = doc["recorded_at"]
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    return {
        "id": str(doc["_id"]),
        "meter_id": doc["meter_id"],
        "meter_name": doc["meter_name"],
        "reading_value": doc["reading_value"],
        "recorded_at": recorded_at.astimezone(SRI_LANKA_TIME),
        "created_by": doc.get("created_by"),
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
    if (
        {"electricity_meter_1_name", "electricity_meter_2_name"} & fields_set
        and str(current_user.get("role", "")).upper() != "ADMIN"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can configure electricity meters.",
        )

    existing = await company_settings_collection().find_one({"key": "main"})
    updates = payload.model_dump(exclude_none=True)
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
    await company_settings_collection().update_one({"key": "main"}, {"$set": updates})
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "company_settings", "main", details=updates,
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
