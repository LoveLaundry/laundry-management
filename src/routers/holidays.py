from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..database.main_db import holidays_collection
from ..models import HolidayCreate, HolidayUpdate
from ..router_utils import serialize, log_audit
from ..error_responses import NotFoundError, ConflictError

router = APIRouter(tags=["Holidays"])


def _parse_oid(value: str, label: str = "id") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise NotFoundError(label, value)
    return ObjectId(value)


@router.get("/holidays")
async def list_holidays(
    year: Optional[int] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if year:
        query["date"] = {"$regex": f"^{year:04d}-"}
    cursor = holidays_collection().find(query).sort("date", 1)
    return [serialize(doc, []) async for doc in cursor]


@router.get("/holidays/{holiday_id}")
async def get_holiday(
    holiday_id: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = _parse_oid(holiday_id, "holiday")
    doc = await holidays_collection().find_one({"_id": oid})
    if not doc:
        raise NotFoundError("Holiday", holiday_id)
    return serialize(doc, [])


@router.post("/holidays")
async def create_holiday(
    payload: HolidayCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    date_str = payload.date.isoformat()
    existing = await holidays_collection().find_one({"date": date_str})
    if existing:
        raise ConflictError(f"A holiday already exists on {date_str}")

    doc = {
        "name": payload.name.strip(),
        "date": date_str,
        "description": (payload.description or "").strip() or None,
        "is_recurring": payload.is_recurring,
        "created_at": datetime.now(timezone.utc),
    }
    result = await holidays_collection().insert_one(doc)
    await log_audit(
        str(current_user.get("user_id", "")),
        "create", "holiday", str(result.inserted_id),
        details={"name": payload.name, "date": date_str},
    )
    doc["_id"] = result.inserted_id
    return serialize(doc, [])


@router.put("/holidays/{holiday_id}")
async def update_holiday(
    holiday_id: str,
    payload: HolidayUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(holiday_id, "holiday")
    existing = await holidays_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Holiday", holiday_id)

    updates = payload.model_dump(exclude_none=True)
    if "date" in updates and updates["date"] is not None:
        updates["date"] = updates["date"].isoformat() if hasattr(updates["date"], "isoformat") else updates["date"]
        dup = await holidays_collection().find_one({"date": updates["date"], "_id": {"$ne": oid}})
        if dup:
            raise ConflictError(f"A holiday already exists on {updates['date']}")

    if len(updates) > 0:
        await holidays_collection().update_one({"_id": oid}, {"$set": updates})
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "holiday", holiday_id, details={},
    )
    updated = await holidays_collection().find_one({"_id": oid})
    return serialize(updated, [])


@router.delete("/holidays/{holiday_id}")
async def delete_holiday(
    holiday_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(holiday_id, "holiday")
    existing = await holidays_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Holiday", holiday_id)
    await holidays_collection().delete_one({"_id": oid})
    await log_audit(
        str(current_user.get("user_id", "")),
        "delete", "holiday", holiday_id, details={},
    )
    return {"success": True}


@router.get("/holidays/check/{date}")
async def check_holiday(
    date: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    doc = await holidays_collection().find_one({"date": date})
    return {"is_holiday": doc is not None, "holiday": serialize(doc, []) if doc else None}
