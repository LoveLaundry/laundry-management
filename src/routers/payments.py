from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import payments_collection, customers_collection
from ..models import PaymentCreate
from ..crypto_helper import encrypt_dict, decrypt_dict
from ..router_utils import serialize, log_audit
from ..error_responses import BadRequestError

router = APIRouter(tags=["Payments"])

SENSITIVE_FIELDS = ["customer_name", "reference", "notes"]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _resolve_customer_name(customer_id: Optional[str], fallback: Optional[str]) -> str:
    if customer_id and ObjectId.is_valid(customer_id):
        doc = await customers_collection().find_one({"_id": ObjectId(customer_id)})
        if doc:
            name = decrypt_dict(doc, ["name", "contact_person", "phone", "email", "address", "notes"]).get("name")
            if name:
                return name
    return (fallback or "").strip()


@router.get("/payments")
async def list_payments(
    customer_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(require_capability("payment:read")),
):
    query: dict = {}
    if customer_id and ObjectId.is_valid(customer_id):
        query["customer_id"] = customer_id
    if start_date:
        query["payment_date"] = {"$gte": start_date}
    if end_date:
        query["payment_date"] = {**query.get("payment_date", {}), "$lte": end_date}
    total = await payments_collection().count_documents(query)
    cursor = payments_collection().find(query).sort("payment_date", -1).skip(offset).limit(limit)
    items = [serialize(doc, SENSITIVE_FIELDS) async for doc in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/payments")
async def create_payment(
    payload: PaymentCreate,
    current_user: dict = Depends(require_capability("payment:write")),
):
    if payload.amount <= 0:
        raise BadRequestError("Amount must be greater than zero")
    customer_name = await _resolve_customer_name(payload.customer_id, payload.customer_name)
    if not customer_name:
        raise BadRequestError("A valid customer is required")

    doc = {
        "customer_id": payload.customer_id,
        "customer_name": customer_name,
        "amount": round(payload.amount, 2),
        "payment_date": payload.payment_date.isoformat(),
        "payment_method": payload.payment_method,
        "reference": (payload.reference or "").strip() or None,
        "notes": (payload.notes or "").strip() or None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await payments_collection().insert_one(encrypted)
    await log_audit(str(current_user.get("user_id", "")), "create", "payment", str(result.inserted_id), details={"amount": payload.amount, "customer": customer_name})
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, SENSITIVE_FIELDS)


@router.delete("/payments/{payment_id}")
async def delete_payment(
    payment_id: str,
    current_user: dict = Depends(require_capability("payment:write")),
):
    oid = ObjectId(payment_id) if ObjectId.is_valid(payment_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Payment not found")
    existing = await payments_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Payment not found")
    await payments_collection().delete_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "delete", "payment", payment_id, details={})
    return {"success": True}