from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import categories_collection, items_collection, transactions_collection
from ..models import CategoryCreate, CategoryUpdate, ItemCreate, ItemUpdate
from ..crypto_helper import get_search_token, encrypt_dict, decrypt_dict
from ..router_utils import serialize, log_audit
from ..error_responses import BadRequestError

router = APIRouter(tags=["Items & Categories"])

CAT_SENSITIVE = ["name", "description"]
ITEM_SENSITIVE = ["name"]
TXN_ITEM_SENSITIVE = ["customer_name", "invoice_number", "items", "notes"]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- Categories ----------------
@router.get("/items/categories")
async def list_categories(
    current_user: dict = Depends(require_capability("item:read")),
):
    cursor = categories_collection().find({}).sort("created_at", -1)
    return [serialize(doc, CAT_SENSITIVE) async for doc in cursor]


@router.post("/items/categories")
async def create_category(
    payload: CategoryCreate,
    current_user: dict = Depends(require_capability("item:write")),
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
    result = await categories_collection().insert_one(encrypted)
    await log_audit(str(current_user.get("user_id", "")), "create", "category", str(result.inserted_id), details={"name": payload.name})
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, CAT_SENSITIVE)


@router.put("/items/categories/{category_id}")
async def update_category(
    category_id: str,
    payload: CategoryUpdate,
    current_user: dict = Depends(require_capability("item:write")),
):
    oid = ObjectId(category_id) if ObjectId.is_valid(category_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Category not found")
    existing = await categories_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Category not found")
    updates = payload.model_dump(exclude_none=True)
    updates["updated_at"] = datetime.now(timezone.utc)
    if len(updates) > 1:
        await categories_collection().update_one({"_id": oid}, {"$set": updates})
    updated = await categories_collection().find_one({"_id": oid})
    return serialize(updated, CAT_SENSITIVE)


@router.delete("/items/categories/{category_id}")
async def delete_category(
    category_id: str,
    current_user: dict = Depends(require_capability("item:write")),
):
    oid = ObjectId(category_id) if ObjectId.is_valid(category_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Category not found")
    existing = await categories_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Category not found")
    await categories_collection().update_one({"_id": oid}, {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}})
    await log_audit(str(current_user.get("user_id", "")), "delete", "category", category_id, details={})
    return {"success": True}


# ---------------- Items ----------------
async def _recompute_item_stats(item_id: str):
    """Recompute aggregate totals for an item from all transactions."""
    total_qty = 0.0
    total_rev = 0.0
    total_cost = 0.0
    for doc in await transactions_collection().find({"item_ids": item_id}).to_list(length=None):
        try:
            txn = decrypt_dict(doc, TXN_ITEM_SENSITIVE)
        except (ValueError, KeyError):
            continue
        for it in txn.get("items", []):
            if str(it.get("item_id")) == item_id:
                total_qty += _num(it.get("quantity_washed")) or _num(it.get("quantity_received"))
                total_rev += _num(it.get("line_total"))
                total_cost += _num(it.get("line_cost"))
    await items_collection().update_one(
        {"_id": ObjectId(item_id)},
        {"$set": {"total_quantity": round(total_qty, 2), "total_revenue": round(total_rev, 2), "total_cost": round(total_cost, 2)}},
    )


@router.get("/items")
async def list_items(
    category_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("item:read")),
):
    query: dict = {"is_active": True}
    if category_id and ObjectId.is_valid(category_id):
        query["category_id"] = category_id
    if search:
        query["name_search"] = get_search_token(search)

    category_cache: dict = {}
    cursor = items_collection().find(query).sort("created_at", -1)
    result = []
    async for doc in cursor:
        item = serialize(doc, ITEM_SENSITIVE)
        cid = item.get("category_id")
        if cid:
            if cid not in category_cache:
                cat_doc = await categories_collection().find_one({"_id": ObjectId(cid)})
                category_cache[cid] = serialize(cat_doc, CAT_SENSITIVE).get("name") if cat_doc else None
            item["category_name"] = category_cache[cid]
        else:
            item["category_name"] = None
        result.append(item)
    return result


@router.post("/items")
async def create_item(
    payload: ItemCreate,
    current_user: dict = Depends(require_capability("item:write")),
):
    if not payload.name.strip():
        raise BadRequestError("Item name is required")
    if payload.category_id and ObjectId.is_valid(payload.category_id):
        cat_doc = await categories_collection().find_one({"_id": ObjectId(payload.category_id)})
        if not cat_doc:
            raise HTTPException(status_code=404, detail="Category not found")

    doc = {
        "name": payload.name.strip(),
        "category_id": payload.category_id,
        "standard_cost": payload.standard_cost,
        "default_rate": payload.default_rate,
        "unit": payload.unit,
        "is_active": payload.is_active,
        "total_quantity": 0.0,
        "total_revenue": 0.0,
        "total_cost": 0.0,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    encrypted = encrypt_dict(doc, ITEM_SENSITIVE)
    result = await items_collection().insert_one(encrypted)
    await log_audit(str(current_user.get("user_id", "")), "create", "item", str(result.inserted_id), details={"name": payload.name})
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, ITEM_SENSITIVE)


@router.get("/items/{item_id}")
async def get_item(
    item_id: str,
    current_user: dict = Depends(require_capability("item:read")),
):
    oid = ObjectId(item_id) if ObjectId.is_valid(item_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Item not found")
    doc = await items_collection().find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Item not found")
    item = serialize(doc, ITEM_SENSITIVE)
    cid = item.get("category_id")
    if cid:
        cat_doc = await categories_collection().find_one({"_id": ObjectId(cid)})
        if cat_doc:
            item["category_name"] = serialize(cat_doc, CAT_SENSITIVE).get("name")
    return item


@router.put("/items/{item_id}")
async def update_item(
    item_id: str,
    payload: ItemUpdate,
    current_user: dict = Depends(require_capability("item:write")),
):
    oid = ObjectId(item_id) if ObjectId.is_valid(item_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Item not found")
    existing = await items_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Item not found")
    updates = payload.model_dump(exclude_none=True)
    updates["updated_at"] = datetime.now(timezone.utc)
    if len(updates) > 1:
        await items_collection().update_one({"_id": oid}, {"$set": updates})
    updated = await items_collection().find_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "update", "item", item_id, details={})
    return serialize(updated, ITEM_SENSITIVE)


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    current_user: dict = Depends(require_capability("item:write")),
):
    oid = ObjectId(item_id) if ObjectId.is_valid(item_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Item not found")
    existing = await items_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Item not found")
    await items_collection().update_one({"_id": oid}, {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}})
    await log_audit(str(current_user.get("user_id", "")), "delete", "item", item_id, details={})
    return {"success": True}