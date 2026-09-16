from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import expenses_collection, expense_categories_collection
from ..models import ExpenseCreate, ExpenseUpdate, ExpenseCategoryCreate, ExpenseCategoryUpdate
from ..crypto_helper import encrypt_dict, decrypt_dict
from ..router_utils import serialize, log_audit
from ..error_responses import BadRequestError

router = APIRouter(tags=["Expenses"])

SENSITIVE_FIELDS = ["description", "reference", "notes"]
CAT_SENSITIVE = ["name", "description"]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _resolve_category_name(category_id: Optional[str], fallback: Optional[str]) -> Optional[str]:
    if category_id and ObjectId.is_valid(category_id):
        doc = await expense_categories_collection().find_one({"_id": ObjectId(category_id)})
        if doc:
            return decrypt_dict(doc, CAT_SENSITIVE).get("name") or (fallback or "").strip()
    return (fallback or "").strip() or None


# ---------------- Expense Categories ----------------
@router.get("/expenses/categories")
async def list_expense_categories(
    current_user: dict = Depends(require_capability("expense:read")),
):
    cursor = expense_categories_collection().find({}).sort("created_at", -1)
    return [serialize(doc, CAT_SENSITIVE) async for doc in cursor]


@router.post("/expenses/categories")
async def create_expense_category(
    payload: ExpenseCategoryCreate,
    current_user: dict = Depends(require_capability("expense:write")),
):
    if not payload.name.strip():
        raise BadRequestError("Category name is required")
    doc = {
        "name": payload.name.strip(),
        "description": (payload.description or "").strip() or None,
        "is_active": payload.is_active,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    encrypted = encrypt_dict(doc, CAT_SENSITIVE)
    result = await expense_categories_collection().insert_one(encrypted)
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, CAT_SENSITIVE)


@router.delete("/expenses/categories/{category_id}")
async def delete_expense_category(
    category_id: str,
    current_user: dict = Depends(require_capability("expense:write")),
):
    oid = ObjectId(category_id) if ObjectId.is_valid(category_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Category not found")
    existing = await expense_categories_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Category not found")
    await expense_categories_collection().update_one({"_id": oid}, {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}})
    return {"success": True}


# ---------------- Expenses ----------------
@router.get("/expenses/summary")
async def expenses_summary(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("expense:read")),
):
    """Total expensed grouped by category."""
    query: dict = {}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        query["date"] = {**query.get("date", {}), "$lte": end_date}

    totals: dict = {}
    async for doc in expenses_collection().find(query):
        cid = doc.get("category_id") or "uncategorized"
        entry = totals.setdefault(cid, {"total": 0.0, "count": 0})
        entry["total"] += _num(doc.get("amount"))
        entry["count"] += 1

    result = []
    for cid, entry in totals.items():
        name = "Uncategorized"
        if cid != "uncategorized" and ObjectId.is_valid(cid):
            cat_doc = await expense_categories_collection().find_one({"_id": ObjectId(cid)})
            if cat_doc:
                name = decrypt_dict(cat_doc, CAT_SENSITIVE).get("name") or name
        result.append({"category": name, "total": round(entry["total"], 2), "count": entry["count"]})
    result.sort(key=lambda x: x["total"], reverse=True)
    return result


@router.get("/expenses")
async def list_expenses(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(require_capability("expense:read")),
):
    query: dict = {}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        query["date"] = {**query.get("date", {}), "$lte": end_date}
    if category_id and ObjectId.is_valid(category_id):
        query["category_id"] = category_id

    total = await expenses_collection().count_documents(query)
    cursor = expenses_collection().find(query).sort("date", -1).skip(offset).limit(limit)
    items = [serialize(doc, SENSITIVE_FIELDS) async for doc in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/expenses")
async def create_expense(
    payload: ExpenseCreate,
    current_user: dict = Depends(require_capability("expense:write")),
):
    if payload.amount <= 0:
        raise BadRequestError("Amount must be greater than zero")
    category_name = await _resolve_category_name(payload.category_id, payload.category_name)
    doc = {
        "date": payload.date.isoformat(),
        "category_id": payload.category_id,
        "category_name": category_name,
        "amount": round(payload.amount, 2),
        "description": (payload.description or "").strip() or None,
        "reference": (payload.reference or "").strip() or None,
        "payment_method": payload.payment_method,
        "is_recurring": payload.is_recurring,
        "notes": (payload.notes or "").strip() or None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await expenses_collection().insert_one(encrypted)
    await log_audit(str(current_user.get("user_id", "")), "create", "expense", str(result.inserted_id), details={"amount": payload.amount, "category": category_name})
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, SENSITIVE_FIELDS)


@router.put("/expenses/{expense_id}")
async def update_expense(
    expense_id: str,
    payload: ExpenseUpdate,
    current_user: dict = Depends(require_capability("expense:write")),
):
    oid = ObjectId(expense_id) if ObjectId.is_valid(expense_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Expense not found")
    existing = await expenses_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Expense not found")

    data = payload.model_dump(exclude_none=True)
    updates = {}
    for key, val in data.items():
        if key == "date" and val is not None:
            updates["date"] = val.isoformat() if hasattr(val, "isoformat") else val
        elif key == "category_name":
            continue
        elif key == "amount" and val is not None:
            updates["amount"] = round(float(val), 2)
        else:
            updates[key] = val
    if payload.category_name and payload.category_id:
        updates["category_name"] = await _resolve_category_name(payload.category_id, payload.category_name)
    updates["updated_at"] = datetime.now(timezone.utc)

    if len(updates) > 1:
        await expenses_collection().update_one({"_id": oid}, {"$set": updates})
    updated = await expenses_collection().find_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "update", "expense", expense_id, details={})
    return serialize(updated, SENSITIVE_FIELDS)


@router.delete("/expenses/{expense_id}")
async def delete_expense(
    expense_id: str,
    current_user: dict = Depends(require_capability("expense:write")),
):
    oid = ObjectId(expense_id) if ObjectId.is_valid(expense_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Expense not found")
    existing = await expenses_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Expense not found")
    await expenses_collection().delete_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "delete", "expense", expense_id, details={})
    return {"success": True}