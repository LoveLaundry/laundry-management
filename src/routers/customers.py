from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import (
    customers_collection,
    customer_rates_collection,
    items_collection,
    transactions_collection,
    payments_collection,
)
from ..models import CustomerCreate, CustomerUpdate, CustomerRateCreate
from ..crypto_helper import get_search_token, encrypt_dict
from ..router_utils import parse_object_id, serialize, log_audit

router = APIRouter(tags=["Customers"])

SENSITIVE_FIELDS = ["name", "contact_person", "phone", "email", "address", "notes"]
RATE_SENSITIVE_FIELDS: list = []


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@router.post("/customers")
async def create_customer(
    payload: CustomerCreate,
    current_user: dict = Depends(require_capability("customer:write")),
):
    now = datetime.now(timezone.utc)
    doc = {
        "name": payload.name.strip(),
        "customer_type": payload.customer_type,
        "contact_person": (payload.contact_person or "").strip() or None,
        "phone": (payload.phone or "").strip() or None,
        "email": (payload.email or "").strip() or None,
        "address": (payload.address or "").strip() or None,
        "billing_method": payload.billing_method,
        "payment_terms": payload.payment_terms,
        "is_active": payload.is_active,
        "notes": (payload.notes or "").strip() or None,
        "created_at": now,
        "updated_at": now,
    }
    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await customers_collection().insert_one(encrypted)
    await log_audit(
        str(current_user.get("user_id", "")),
        "create",
        "customer",
        str(result.inserted_id),
        details={"name": payload.name},
    )
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, SENSITIVE_FIELDS)


@router.get("/customers")
async def list_customers(
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(require_capability("customer:read")),
):
    query: dict = {}
    if search:
        query["name_search"] = get_search_token(search)
    total = await customers_collection().count_documents(query)
    cursor = customers_collection().find(query).sort("created_at", -1).skip(offset).limit(limit)
    items = [serialize(doc, SENSITIVE_FIELDS) async for doc in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/customers/summary")
async def customer_summary(
    current_user: dict = Depends(require_capability("customer:read")),
):
    """Per-customer totals for the customers grid."""
    txn_sens = ["customer_name", "invoice_number", "items", "notes"]
    pay_sens = ["customer_name", "reference", "notes"]

    # The only consumed fields (customer_id, total_amount, total_quantity) are
    # not encrypted, so aggregate server-side instead of transferring + decrypting
    # the full collections.
    txn_rows = await transactions_collection().aggregate([
        {"$group": {
            "_id": {"$toString": {"$ifNull": ["$customer_id", ""]}},
            "revenue": {"$sum": "$total_amount"},
            "qty": {"$sum": "$total_quantity"},
            "count": {"$sum": 1},
        }},
    ]).to_list(length=None)
    revenue_map: dict = {}
    qty_map: dict = {}
    txn_count: dict = {}
    for r in txn_rows:
        cid = str(r.get("_id") or "")
        if not cid:
            continue
        revenue_map[cid] = r.get("revenue") or 0
        qty_map[cid] = r.get("qty") or 0
        txn_count[cid] = r.get("count") or 0

    pay_rows = await payments_collection().aggregate([
        {"$group": {"_id": {"$toString": {"$ifNull": ["$customer_id", ""]}}, "amount": {"$sum": "$amount"}}},
    ]).to_list(length=None)
    paid_map: dict = {str(r.get("_id") or ""): r.get("amount") or 0 for r in pay_rows}

    cursor = customers_collection().find({}).sort("created_at", -1)
    result = []
    async for doc in cursor:
        rec = serialize(doc, SENSITIVE_FIELDS)
        cid = str(doc["_id"])
        billed = revenue_map.get(cid, 0)
        paid = paid_map.get(cid, 0)
        result.append({
            **rec,
            "total_transactions": txn_count.get(cid, 0),
            "total_revenue": round(billed, 2),
            "total_items": round(qty_map.get(cid, 0), 2),
            "total_paid": round(paid, 2),
            "outstanding_payments": round(max(billed - paid, 0), 2),
        })
    return result


@router.get("/customers/{customer_id}")
async def get_customer(
    customer_id: str,
    current_user: dict = Depends(require_capability("customer:read")),
):
    oid = parse_object_id(customer_id, "customer")
    doc = await customers_collection().find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Customer not found")
    return serialize(doc, SENSITIVE_FIELDS)


@router.put("/customers/{customer_id}")
async def update_customer(
    customer_id: str,
    payload: CustomerUpdate,
    current_user: dict = Depends(require_capability("customer:write")),
):
    oid = parse_object_id(customer_id, "customer")
    existing = await customers_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Customer not found")

    updates = payload.model_dump(exclude_none=True)
    updates = {k: v for k, v in updates.items() if k in SENSITIVE_FIELDS + ["customer_type", "billing_method", "payment_terms", "is_active"]}
    updates["updated_at"] = datetime.now(timezone.utc)

    if len(updates) > 1:
        await customers_collection().update_one({"_id": oid}, {"$set": updates})
    updated = await customers_collection().find_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "update", "customer", customer_id, details={"fields": list(payload.model_dump(exclude_none=True).keys())})
    return serialize(updated, SENSITIVE_FIELDS)


@router.delete("/customers/{customer_id}")
async def delete_customer(
    customer_id: str,
    current_user: dict = Depends(require_capability("customer:write")),
):
    oid = parse_object_id(customer_id, "customer")
    existing = await customers_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Customer not found")
    await customers_collection().update_one(
        {"_id": oid}, {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}}
    )
    await log_audit(str(current_user.get("user_id", "")), "delete", "customer", customer_id, details={})
    return {"success": True}


# ---------------- Customer Rates ----------------
@router.get("/customers/{customer_id}/rates")
async def list_customer_rates(
    customer_id: str,
    current_user: dict = Depends(require_capability("customer:read")),
):
    oid = parse_object_id(customer_id, "customer")
    cursor = customer_rates_collection().find({"customer_id": str(oid)})
    items = []
    async for doc in cursor:
        item = serialize(doc, RATE_SENSITIVE_FIELDS)
        iid = item.get("item_id")
        if iid:
            item_doc = await items_collection().find_one({"_id": ObjectId(iid)})
            if item_doc:
                item["item_name"] = serialize(item_doc, ["name"]).get("name")
        items.append(item)
    return items


@router.post("/customers/{customer_id}/rates")
async def add_customer_rate(
    customer_id: str,
    payload: CustomerRateCreate,
    current_user: dict = Depends(require_capability("customer:write")),
):
    oid = parse_object_id(customer_id, "customer")
    existing = await customers_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Customer not found")
    item_oid = parse_object_id(payload.item_id, "item")
    item_doc = await items_collection().find_one({"_id": item_oid})
    if not item_doc:
        raise HTTPException(status_code=404, detail="Item not found")

    doc = {
        "customer_id": str(oid),
        "item_id": payload.item_id,
        "rate": payload.rate,
        "cost": payload.cost,
        "is_active": payload.is_active,
        "created_at": datetime.now(timezone.utc),
    }
    result = await customer_rates_collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    return serialize(doc, RATE_SENSITIVE_FIELDS)


@router.delete("/customers/{customer_id}/rates/{rate_id}")
async def delete_customer_rate(
    customer_id: str,
    rate_id: str,
    current_user: dict = Depends(require_capability("customer:write")),
):
    rate_oid = parse_object_id(rate_id, "rate")
    result = await customer_rates_collection().delete_one({"_id": rate_oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Rate not found")
    return {"success": True}