from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..database.main_db import salary_advances_collection, employees_collection
from ..models import AdvanceCreate, AdvanceUpdate, AdvanceDeduct
from ..router_utils import serialize, log_audit
from ..error_responses import NotFoundError, BadRequestError, ConflictError

router = APIRouter(tags=["Salary Advances"])

SENSITIVE_FIELDS = []


def _parse_oid(value: str, label: str = "id") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise NotFoundError(label, value)
    return ObjectId(value)


# ── List advances ──────────────────────────────────────────────────────────
@router.get("/advances")
async def list_advances(
    employee_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if employee_id:
        query["employee_id"] = employee_id
    if status:
        query["status"] = status
    if start_date or end_date:
        date_q: dict = {}
        if start_date:
            date_q["$gte"] = start_date
        if end_date:
            date_q["$lte"] = end_date
        query["date"] = date_q
    cursor = salary_advances_collection().find(query).sort("date", -1)
    return [serialize(doc, SENSITIVE_FIELDS) async for doc in cursor]


# ── Get single advance ────────────────────────────────────────────────────
@router.get("/advances/{advance_id}")
async def get_advance(
    advance_id: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = _parse_oid(advance_id, "advance")
    doc = await salary_advances_collection().find_one({"_id": oid})
    if not doc:
        raise NotFoundError("Advance", advance_id)
    return serialize(doc, SENSITIVE_FIELDS)


# ── Create advance ────────────────────────────────────────────────────────
@router.post("/advances")
async def create_advance(
    payload: AdvanceCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    emp_oid = _parse_oid(payload.employee_id, "employee")
    emp = await employees_collection().find_one({"_id": emp_oid})
    if not emp:
        raise NotFoundError("Employee", payload.employee_id)

    if payload.amount <= 0:
        raise BadRequestError("Advance amount must be greater than zero")

    doc = {
        "employee_id": payload.employee_id,
        "amount": round(payload.amount, 2),
        "date": payload.date.isoformat(),
        "reason": (payload.reason or "").strip() or None,
        "reference": (payload.reference or "").strip() or None,
        "total_deducted": 0.0,
        "outstanding": round(payload.amount, 2),
        "status": "OUTSTANDING",
        "deductions": [],
        "created_by": str(current_user.get("user_id", "")),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await salary_advances_collection().insert_one(doc)
    await log_audit(
        str(current_user.get("user_id", "")),
        "create", "advance", str(result.inserted_id),
        details={"employee_id": payload.employee_id, "amount": payload.amount},
    )
    doc["_id"] = result.inserted_id
    return serialize(doc, SENSITIVE_FIELDS)


# ── Update advance ────────────────────────────────────────────────────────
@router.put("/advances/{advance_id}")
async def update_advance(
    advance_id: str,
    payload: AdvanceUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(advance_id, "advance")
    existing = await salary_advances_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Advance", advance_id)

    updates = payload.model_dump(exclude_none=True)
    if "date" in updates and updates["date"] is not None:
        updates["date"] = updates["date"].isoformat() if hasattr(updates["date"], "isoformat") else updates["date"]
    if "amount" in updates:
        updates["amount"] = round(float(updates["amount"]), 2)
        original = existing.get("amount", 0)
        deducted = existing.get("total_deducted", 0)
        updates["outstanding"] = round(updates["amount"] - deducted, 2)
    updates["updated_at"] = datetime.now(timezone.utc)

    if len(updates) > 1:
        await salary_advances_collection().update_one({"_id": oid}, {"$set": updates})
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "advance", advance_id, details={},
    )
    updated = await salary_advances_collection().find_one({"_id": oid})
    return serialize(updated, SENSITIVE_FIELDS)


# ── Cancel advance ────────────────────────────────────────────────────────
@router.delete("/advances/{advance_id}")
async def cancel_advance(
    advance_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(advance_id, "advance")
    existing = await salary_advances_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Advance", advance_id)
    if existing.get("status") == "CANCELLED":
        raise ConflictError("Advance is already cancelled")

    await salary_advances_collection().update_one(
        {"_id": oid},
        {"$set": {"status": "CANCELLED", "updated_at": datetime.now(timezone.utc)}},
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "cancel", "advance", advance_id, details={},
    )
    return {"success": True}


# ── Get outstanding advances for an employee ──────────────────────────────
@router.get("/employees/{employee_id}/advances")
async def list_employee_advances(
    employee_id: str,
    status: Optional[str] = Query("OUTSTANDING"),
    current_user: dict = Depends(require_capability("salary:read")),
):
    _parse_oid(employee_id, "employee")
    query: dict = {"employee_id": employee_id}
    if status:
        query["status"] = status
    cursor = salary_advances_collection().find(query).sort("date", -1)
    return [serialize(doc, SENSITIVE_FIELDS) async for doc in cursor]


# ── Get total outstanding for an employee ─────────────────────────────────
@router.get("/employees/{employee_id}/advances/summary")
async def employee_advances_summary(
    employee_id: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    _parse_oid(employee_id, "employee")
    pipeline = [
        {"$match": {"employee_id": employee_id, "status": "OUTSTANDING"}},
        {"$group": {"_id": None, "total_outstanding": {"$sum": "$outstanding"}, "count": {"$sum": 1}}},
    ]
    result = await salary_advances_collection().aggregate(pipeline).to_list(1)
    if result:
        return {"total_outstanding": result[0].get("total_outstanding", 0), "count": result[0].get("count", 0)}
    return {"total_outstanding": 0, "count": 0}
