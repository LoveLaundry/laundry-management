from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..database.main_db import (
    extra_work_categories_collection,
    extra_work_records_collection,
    employees_collection,
)
from ..models import ExtraWorkCategoryCreate, ExtraWorkCategoryUpdate, ExtraWorkRecordCreate, ExtraWorkRecordUpdate
from ..router_utils import serialize, log_audit
from ..error_responses import NotFoundError, BadRequestError

router = APIRouter(tags=["Extra Work"])


def _parse_oid(value: str, label: str = "id") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise NotFoundError(label, value)
    return ObjectId(value)


# ── Categories ────────────────────────────────────────────────────────────
@router.get("/extra-work/categories")
async def list_categories(
    is_active: Optional[bool] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if is_active is not None:
        query["is_active"] = is_active
    cursor = extra_work_categories_collection().find(query).sort("name", 1)
    return [serialize(doc, []) async for doc in cursor]


@router.get("/extra-work/categories/{category_id}")
async def get_category(
    category_id: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = _parse_oid(category_id, "category")
    doc = await extra_work_categories_collection().find_one({"_id": oid})
    if not doc:
        raise NotFoundError("Extra work category", category_id)
    return serialize(doc, [])


@router.post("/extra-work/categories")
async def create_category(
    payload: ExtraWorkCategoryCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    doc = {
        "name": payload.name.strip(),
        "description": (payload.description or "").strip() or None,
        "rate": round(payload.rate, 2),
        "calculation_method": payload.calculation_method,
        "unit": payload.unit,
        "is_active": payload.is_active,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await extra_work_categories_collection().insert_one(doc)
    await log_audit(
        str(current_user.get("user_id", "")),
        "create", "extra_work_category", str(result.inserted_id),
        details={"name": payload.name},
    )
    doc["_id"] = result.inserted_id
    return serialize(doc, [])


@router.put("/extra-work/categories/{category_id}")
async def update_category(
    category_id: str,
    payload: ExtraWorkCategoryUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(category_id, "category")
    existing = await extra_work_categories_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Extra work category", category_id)

    updates = payload.model_dump(exclude_none=True)
    if "rate" in updates:
        updates["rate"] = round(float(updates["rate"]), 2)
    updates["updated_at"] = datetime.now(timezone.utc)

    if len(updates) > 1:
        await extra_work_categories_collection().update_one({"_id": oid}, {"$set": updates})
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "extra_work_category", category_id, details={},
    )
    updated = await extra_work_categories_collection().find_one({"_id": oid})
    return serialize(updated, [])


@router.delete("/extra-work/categories/{category_id}")
async def delete_category(
    category_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(category_id, "category")
    existing = await extra_work_categories_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Extra work category", category_id)
    await extra_work_categories_collection().update_one(
        {"_id": oid}, {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}}
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "deactivate", "extra_work_category", category_id, details={},
    )
    return {"success": True}


# ── Records ───────────────────────────────────────────────────────────────
@router.get("/extra-work/records")
async def list_records(
    employee_id: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if employee_id:
        query["employee_id"] = employee_id
    if category_id:
        query["category_id"] = category_id
    if start_date or end_date:
        date_q: dict = {}
        if start_date:
            date_q["$gte"] = start_date
        if end_date:
            date_q["$lte"] = end_date
        query["date"] = date_q
    cursor = extra_work_records_collection().find(query).sort("date", -1)
    return [serialize(doc, []) async for doc in cursor]


@router.post("/extra-work/records")
async def create_record(
    payload: ExtraWorkRecordCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    emp_oid = _parse_oid(payload.employee_id, "employee")
    emp = await employees_collection().find_one({"_id": emp_oid})
    if not emp:
        raise NotFoundError("Employee", payload.employee_id)

    cat_oid = _parse_oid(payload.category_id, "category")
    cat = await extra_work_categories_collection().find_one({"_id": cat_oid})
    if not cat:
        raise NotFoundError("Extra work category", payload.category_id)
    if not cat.get("is_active"):
        raise BadRequestError("Extra work category is inactive")

    amount = payload.amount
    if amount == 0 and payload.units > 0:
        amount = round(payload.units * cat.get("rate", 0), 2)

    doc = {
        "employee_id": payload.employee_id,
        "category_id": payload.category_id,
        "category_name": cat.get("name"),
        "date": payload.date.isoformat(),
        "units": round(payload.units, 2),
        "rate": cat.get("rate", 0),
        "amount": round(amount, 2),
        "notes": (payload.notes or "").strip() or None,
        "created_by": str(current_user.get("user_id", "")),
        "created_at": datetime.now(timezone.utc),
    }
    result = await extra_work_records_collection().insert_one(doc)
    await log_audit(
        str(current_user.get("user_id", "")),
        "create", "extra_work_record", str(result.inserted_id),
        details={"employee_id": payload.employee_id, "category": cat.get("name"), "amount": amount},
    )
    doc["_id"] = result.inserted_id
    return serialize(doc, [])


@router.put("/extra-work/records/{record_id}")
async def update_record(
    record_id: str,
    payload: ExtraWorkRecordUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(record_id, "record")
    existing = await extra_work_records_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Extra work record", record_id)

    updates = payload.model_dump(exclude_none=True)
    if "date" in updates and updates["date"] is not None:
        updates["date"] = updates["date"].isoformat() if hasattr(updates["date"], "isoformat") else updates["date"]
    if "amount" in updates:
        updates["amount"] = round(float(updates["amount"]), 2)
    if "units" in updates:
        updates["units"] = round(float(updates["units"]), 2)

    if len(updates) > 0:
        await extra_work_records_collection().update_one({"_id": oid}, {"$set": updates})

    updated = await extra_work_records_collection().find_one({"_id": oid})
    return serialize(updated, [])


@router.delete("/extra-work/records/{record_id}")
async def delete_record(
    record_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(record_id, "record")
    existing = await extra_work_records_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Extra work record", record_id)
    await extra_work_records_collection().delete_one({"_id": oid})
    await log_audit(
        str(current_user.get("user_id", "")),
        "delete", "extra_work_record", record_id, details={},
    )
    return {"success": True}
