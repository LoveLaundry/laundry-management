from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth_helper import require_capability
from ..database.main_db import customers_collection, items_collection, transactions_collection
from ..models import TransactionCreate, TransactionUpdate
from ..crypto_helper import get_search_token, encrypt_dict, decrypt_dict
from ..router_utils import serialize, log_audit
from ..error_responses import BadRequestError

router = APIRouter(tags=["Transactions"])

SENSITIVE_FIELDS = ["customer_name", "invoice_number", "items", "notes"]

STATUSES = ["RECEIVED", "IN_PROCESS", "COMPLETED", "DELIVERED", "PARTIAL", "CANCELLED", "PENDING"]


class BulkTransactionRequest(BaseModel):
    transactions: List[TransactionCreate]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _billed_qty(item: dict) -> float:
    washed = _num(item.get("quantity_washed"))
    if washed:
        return washed
    return _num(item.get("quantity_received"))


async def _build_item(item: dict, source: str) -> dict:
    """Resolve item name/category from item_id and compute line totals."""
    item_name = (item.get("item_name") or "").strip()
    category_name = (item.get("category_name") or "").strip() or None
    item_id = item.get("item_id")

    if item_id and ObjectId.is_valid(item_id):
        item_doc = await items_collection().find_one({"_id": ObjectId(item_id)})
        if item_doc:
            dec = decrypt_dict(item_doc, ["name"])
            if not item_name:
                item_name = dec.get("name") or item_name
            cid = item_doc.get("category_id")
            if cid and ObjectId.is_valid(cid):
                from ..database.main_db import categories_collection
                cat_doc = await categories_collection().find_one({"_id": ObjectId(cid)})
                if cat_doc:
                    category_name = decrypt_dict(cat_doc, ["name", "description"]).get("name") or category_name
            # Fall back to category from item doc if not set
            if not category_name:
                category_name = item_doc.get("category_name")

    qty = _billed_qty(item)
    rate = _num(item.get("rate"))
    cost = _num(item.get("cost"))
    line_total = round(qty * rate, 2)
    line_cost = round(qty * cost, 2)

    return {
        "item_id": item_id,
        "item_name": item_name,
        "category_name": category_name,
        "quantity_received": _num(item.get("quantity_received")),
        "quantity_washed": _num(item.get("quantity_washed")),
        "quantity_delivered": _num(item.get("quantity_delivered")),
        "quantity_rejected": _num(item.get("quantity_rejected")),
        "quantity_damaged": _num(item.get("quantity_damaged")),
        "quantity_missing": _num(item.get("quantity_missing")),
        "quantity_stored": _num(item.get("quantity_stored")),
        "rate": rate,
        "cost": cost,
        "line_total": line_total,
        "line_cost": line_cost,
        "line_profit": round(line_total - line_cost, 2),
        "status": item.get("status") or "RECEIVED",
        "notes": (item.get("notes") or "").strip() or None,
    }


async def _resolve_customer_name(customer_id: Optional[str], fallback: Optional[str]) -> str:
    if customer_id and ObjectId.is_valid(customer_id):
        doc = await customers_collection().find_one({"_id": ObjectId(customer_id)})
        if doc:
            dec = decrypt_dict(doc, ["name", "contact_person", "phone", "email", "address", "notes"])
            return dec.get("name") or (fallback or "").strip()
    return (fallback or "").strip()


async def _recompute_item_stats(item_id: str):
    total_qty = 0.0
    total_rev = 0.0
    total_cost = 0.0
    for doc in await transactions_collection().find({"item_ids": item_id}).to_list(length=None):
        try:
            txn = decrypt_dict(doc, SENSITIVE_FIELDS)
        except (ValueError, KeyError):
            continue
        for it in txn.get("items", []):
            if str(it.get("item_id")) == item_id:
                total_qty += _billed_qty(it)
                total_rev += _num(it.get("line_total"))
                total_cost += _num(it.get("line_cost"))
    await items_collection().update_one(
        {"_id": ObjectId(item_id)},
        {"$set": {
            "total_quantity": round(total_qty, 2),
            "total_revenue": round(total_rev, 2),
            "total_cost": round(total_cost, 2),
        }},
    )


def _serialize_txn(doc: dict) -> dict:
    try:
        txn = serialize(doc, SENSITIVE_FIELDS)
    except (ValueError, KeyError):
        txn = {k: v for k, v in doc.items() if k != "encryption_metadata" and not k.endswith("_search")}
        txn["id"] = str(doc.get("_id", ""))
    items = txn.get("items") or []
    for i, it in enumerate(items):
        it["id"] = str(it.get("item_id") or f"li-{i}")
    txn["items"] = items
    return txn


async def _store_transaction(txn: dict, current_user: dict) -> dict:
    """Encrypt and insert a single transaction document."""
    items = []
    for raw_item in txn.get("items", []):
        items.append(await _build_item(raw_item, txn.get("source") or "MANUAL"))

    customer_name = await _resolve_customer_name(txn.get("customer_id"), txn.get("customer_name"))
    if not customer_name:
        raise BadRequestError("A valid customer is required")

    total_qty = round(sum(_billed_qty(i) for i in items), 2)
    total_amount = round(sum(_num(i.get("line_total")) for i in items), 2)
    total_cost = round(sum(_num(i.get("line_cost")) for i in items), 2)
    total_profit = round(total_amount - total_cost, 2)

    now = datetime.now(timezone.utc)
    raw_date = txn.get("transaction_date") or now.date().isoformat()
    txn_date = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date)
    doc = {
        "transaction_date": txn_date,
        "customer_id": txn.get("customer_id"),
        "customer_name": customer_name,
        "invoice_number": (txn.get("invoice_number") or "").strip() or None,
        "status": (txn.get("status") or "COMPLETED").upper() if (txn.get("status") or "COMPLETED").upper() in STATUSES else "COMPLETED",
        "source": (txn.get("source") or "MANUAL").upper(),
        "import_batch_id": txn.get("import_batch_id"),
        "items": items,
        "item_ids": [it["item_id"] for it in items if it.get("item_id")],
        "total_quantity": total_qty,
        "total_amount": total_amount,
        "total_cost": total_cost,
        "total_profit": total_profit,
        "notes": (txn.get("notes") or "").strip() or None,
        "created_at": now,
        "updated_at": now,
        "created_by": str(current_user.get("user_name") or current_user.get("user_id") or ""),
    }
    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await transactions_collection().insert_one(encrypted)

    # Update item rollups
    for it in items:
        if it.get("item_id"):
            try:
                await _recompute_item_stats(it["item_id"])
            except Exception:
                pass

    encrypted["_id"] = result.inserted_id
    return _serialize_txn(encrypted)


@router.post("/transactions/bulk")
async def bulk_create_transactions(
    payload: BulkTransactionRequest,
    current_user: dict = Depends(require_capability("transaction:write")),
):
    if not payload.transactions:
        raise BadRequestError("No transactions provided")
    created = []
    for txn in payload.transactions:
        created.append(await _store_transaction(txn.model_dump(), current_user))
    await log_audit(
        str(current_user.get("user_id", "")),
        "bulk_create",
        "transaction",
        None,
        details={"count": len(created), "total_amount": round(sum(t["total_amount"] for t in created), 2)},
    )
    return created


@router.post("/transactions")
async def create_transaction(
    payload: TransactionCreate,
    current_user: dict = Depends(require_capability("transaction:write")),
):
    txn = await _store_transaction(payload.model_dump(), current_user)
    await log_audit(
        str(current_user.get("user_id", "")),
        "create",
        "transaction",
        txn["id"],
        details={"total_amount": txn["total_amount"], "items": len(txn.get("items") or [])},
    )
    return txn


@router.get("/transactions")
async def list_transactions(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(require_capability("transaction:read")),
):
    query: dict = {}
    if start_date:
        query["transaction_date"] = {"$gte": start_date}
    if end_date:
        query["transaction_date"] = {**query.get("transaction_date", {}), "$lte": end_date}
    if customer_id and ObjectId.is_valid(customer_id):
        query["customer_id"] = customer_id
    if search:
        query["invoice_search"] = get_search_token(search)
    if source:
        query["source"] = source.upper()

    cursor = transactions_collection().find(query).sort("transaction_date", -1).skip(offset).limit(limit)
    return [_serialize_txn(doc) async for doc in cursor]


@router.get("/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: str,
    current_user: dict = Depends(require_capability("transaction:read")),
):
    oid = parse_oid(transaction_id, "transaction")
    doc = await transactions_collection().find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _serialize_txn(doc)


@router.put("/transactions/{transaction_id}")
async def update_transaction(
    transaction_id: str,
    payload: TransactionUpdate,
    current_user: dict = Depends(require_capability("transaction:write")),
):
    oid = parse_oid(transaction_id, "transaction")
    existing = await transactions_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Transaction not found")

    old = _serialize_txn(await transactions_collection().find_one({"_id": oid}))
    data = payload.model_dump(exclude_none=True)
    updates = {k: v for k, v in data.items() if k != "items"}
    if updates.get("transaction_date") is not None:
        td = updates["transaction_date"]
        updates["transaction_date"] = td.isoformat() if hasattr(td, "isoformat") else str(td)
    if "items" in data and data["items"] is not None:
        updates["items"] = [await _build_item(i.model_dump(), "MANUAL") for i in data["items"]]
        updates["item_ids"] = [it["item_id"] for it in updates["items"] if it.get("item_id")]
        updates["total_quantity"] = round(sum(_billed_qty(i) for i in updates["items"]), 2)
        updates["total_amount"] = round(sum(_num(i.get("line_total")) for i in updates["items"]), 2)
        updates["total_cost"] = round(sum(_num(i.get("line_cost")) for i in updates["items"]), 2)
        updates["total_profit"] = round(updates["total_amount"] - updates["total_cost"], 2)
    if "customer_name" in updates and updates["customer_name"]:
        updates["customer_name"] = (updates["customer_name"] or "").strip()
    updates["updated_at"] = datetime.now(timezone.utc)

    await transactions_collection().update_one({"_id": oid}, {"$set": updates})

    # Refresh rollups for old + new items
    affected = set()
    for it in (old.get("items") or []):
        if it.get("item_id"):
            affected.add(it["item_id"])
    for it in (updates.get("items") or []):
        if it.get("item_id"):
            affected.add(it["item_id"])
    for iid in affected:
        try:
            await _recompute_item_stats(iid)
        except Exception:
            pass

    updated = await transactions_collection().find_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "update", "transaction", transaction_id, details={})
    return _serialize_txn(updated)


@router.delete("/transactions/{transaction_id}")
async def delete_transaction(
    transaction_id: str,
    current_user: dict = Depends(require_capability("transaction:write")),
):
    oid = parse_oid(transaction_id, "transaction")
    existing = await transactions_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Transaction not found")

    old = _serialize_txn(existing)
    await transactions_collection().delete_one({"_id": oid})

    affected = set()
    for it in (old.get("items") or []):
        if it.get("item_id"):
            affected.add(it["item_id"])
    for iid in affected:
        try:
            await _recompute_item_stats(iid)
        except Exception:
            pass

    await log_audit(str(current_user.get("user_id", "")), "delete", "transaction", transaction_id, details={})
    return {"success": True}


def parse_oid(value: str, label: str):
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value}")