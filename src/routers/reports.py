from datetime import date as date_type
from datetime import datetime, timezone
from datetime import timedelta
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..database.main_db import (
    customers_collection,
    items_collection,
    categories_collection,
    transactions_collection,
    payments_collection,
    expenses_collection,
    expense_categories_collection,
    salaries_collection,
)
from ..crypto_helper import decrypt_dict
from ..router_utils import serialize

router = APIRouter(tags=["Reports"])

TXN_SENSITIVE = ["customer_name", "invoice_number", "items", "notes"]
PAY_SENSITIVE = ["customer_name", "reference", "notes"]
PAY_CUSTOMER_SENSITIVE = ["name", "contact_person", "phone", "email", "address", "notes"]
ITEM_SENSITIVE = ["name"]
CAT_SENSITIVE = ["name", "description"]
EXPENSE_SENSITIVE = ["description", "reference", "notes"]
SALARY_SENSITIVE = ["notes"]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _dec_transactions(query: dict) -> list:
    result = []
    for doc in await transactions_collection().find(query).to_list(length=None):
        try:
            result.append(serialize(doc, TXN_SENSITIVE))
        except (ValueError, KeyError):
            result.append({k: v for k, v in doc.items() if k != "encryption_metadata" and not k.endswith("_search")})
    return result


async def _dec_expenses(query: dict) -> list:
    result = []
    for doc in await expenses_collection().find(query).to_list(length=None):
        try:
            result.append(serialize(doc, EXPENSE_SENSITIVE))
        except (ValueError, KeyError):
            pass
    return result


@router.get("/reports/profit-loss")
async def profit_loss_report(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("report:read")),
):
    query: dict = {}
    if start_date:
        query["transaction_date"] = {"$gte": start_date}
    if end_date:
        query["transaction_date"] = {**query.get("transaction_date", {}), "$lte": end_date}
    if customer_id and ObjectId.is_valid(customer_id):
        query["customer_id"] = customer_id

    txns = await _dec_transactions(query)
    exp_query: dict = {}
    if start_date:
        exp_query["date"] = {"$gte": start_date}
    if end_date:
        exp_query["date"] = {**exp_query.get("date", {}), "$lte": end_date}
    expenses = await _dec_expenses(exp_query)

    total_revenue = round(sum(_num(t.get("total_amount")) for t in txns), 2)
    total_cogs = round(sum(_num(t.get("total_cost")) for t in txns), 2)
    gross_profit = round(total_revenue - total_cogs, 2)
    total_expenses = round(sum(_num(e.get("amount")) for e in expenses), 2)
    net_profit = round(gross_profit - total_expenses, 2)

    # Revenue by customer
    by_customer: dict = {}
    for t in txns:
        cid = str(t.get("customer_id") or "")
        name = t.get("customer_name") or "Unknown"
        rec = by_customer.setdefault(cid, {"name": name, "value": 0.0})
        rec["value"] += _num(t.get("total_amount"))
        rec["name"] = t.get("customer_name") or rec["name"]
    revenue_by_customer = [
        {"name": rec["name"], "value": round(rec["value"], 2)}
        for rec in sorted(by_customer.values(), key=lambda x: x["value"], reverse=True)
    ]

    # Revenue by category (from decrypted line items)
    cat_cache: dict = {}
    by_category: dict = {}
    for t in txns:
        for it in t.get("items") or []:
            cid = it.get("category_id") or it.get("item_id") or "uncategorized"
            # Resolve category name
            key = it.get("item_id")
            if key and ObjectId.is_valid(key) and cid == key:
                pass
            name = cid
            if cid != "uncategorized" and ObjectId.is_valid(cid):
                if cid not in cat_cache:
                    cat_doc = await categories_collection().find_one({"_id": ObjectId(cid)})
                    cat_cache[cid] = decrypt_dict(cat_doc, CAT_SENSITIVE).get("name") if cat_doc else cid
                name = cat_cache[cid]
            elif key and ObjectId.is_valid(key):
                if key not in cat_cache:
                    item_doc = await items_collection().find_one({"_id": ObjectId(key)})
                    if item_doc and item_doc.get("category_id"):
                        cat_doc = await categories_collection().find_one({"_id": ObjectId(item_doc["category_id"])})
                        cat_cache[key] = decrypt_dict(cat_doc, CAT_SENSITIVE).get("name") if cat_doc else "Uncategorized"
                    else:
                        cat_cache[key] = it.get("category_name") or "Uncategorized"
                name = cat_cache[key]
            elif it.get("category_name"):
                name = it["category_name"]
            rec = by_category.setdefault(name, 0.0)
            rec += _num(it.get("line_total"))
    revenue_by_category = [
        {"name": k, "value": round(v, 2)}
        for k, v in sorted(by_category.items(), key=lambda x: x[1], reverse=True)
    ]

    # Expense breakdown
    by_exp_cat: dict = {}
    for e in expenses:
        cid = e.get("category_id") or "Uncategorized"
        name = cid
        if cid != "Uncategorized" and ObjectId.is_valid(cid):
            cat_doc = await expense_categories_collection().find_one({"_id": ObjectId(cid)})
            name = decrypt_dict(cat_doc, CAT_SENSITIVE).get("name") if cat_doc else "Uncategorized"
        by_exp_cat[name] = by_exp_cat.get(name, 0.0) + _num(e.get("amount"))
    expenses_by_category = [
        {"name": k, "value": round(v, 2)}
        for k, v in sorted(by_exp_cat.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "total_revenue": total_revenue,
        "total_cogs": total_cogs,
        "gross_profit": gross_profit,
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "revenue_by_customer": revenue_by_customer,
        "revenue_by_category": revenue_by_category,
        "expenses_by_category": expenses_by_category,
    }


@router.get("/reports/daily")
async def daily_report(
    report_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("report:read")),
):
    day = report_date or _today()
    txns = await _dec_transactions({"transaction_date": day})
    total_qty = round(sum(_num(t.get("total_quantity")) for t in txns), 2)
    revenue = round(sum(_num(t.get("total_amount")) for t in txns), 2)
    cogs = round(sum(_num(t.get("total_cost")) for t in txns), 2)
    return {
        "date": day,
        "transactions": len(txns),
        "total_quantity": total_qty,
        "total_revenue": revenue,
        "gross_profit": round(revenue - cogs, 2),
    }


@router.get("/reports/monthly")
async def monthly_report(
    year: int = Query(datetime.now(timezone.utc).year),
    month: int = Query(datetime.now(timezone.utc).month),
    current_user: dict = Depends(require_capability("report:read")),
):
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"

    query = {"transaction_date": {"$gte": start, "$lt": end}}
    txns = await _dec_transactions(query)
    total_qty = round(sum(_num(t.get("total_quantity")) for t in txns), 2)
    revenue = round(sum(_num(t.get("total_amount")) for t in txns), 2)
    cogs = round(sum(_num(t.get("total_cost")) for t in txns), 2)

    by_customer: dict = {}
    for t in txns:
        cid = str(t.get("customer_id") or "")
        rec = by_customer.setdefault(cid, {"name": t.get("customer_name") or "Unknown", "value": 0.0})
        rec["value"] += _num(t.get("total_amount"))
        rec["name"] = t.get("customer_name") or rec["name"]
    revenue_by_customer = [
        {"name": rec["name"], "value": round(rec["value"], 2)}
        for rec in sorted(by_customer.values(), key=lambda x: x["value"], reverse=True)
    ]

    return {
        "year": year,
        "month": month,
        "transactions": len(txns),
        "total_quantity": total_qty,
        "total_revenue": revenue,
        "gross_profit": round(revenue - cogs, 2),
        "revenue_by_customer": revenue_by_customer,
    }


@router.get("/reports/outstanding")
async def outstanding_report(
    current_user: dict = Depends(require_capability("report:read")),
):
    billed_map: dict = {}
    # Only customer_id / customer_name / total_amount are consumed — project them
    # (plus encryption_metadata so customer_name can be decrypted) instead of
    # transferring full documents with their (large) encrypted items arrays.
    txn_cursor = transactions_collection().find({}, {
        "customer_id": 1, "customer_name": 1, "total_amount": 1, "encryption_metadata": 1,
    })

    def _snap(doc: dict) -> dict:
        try:
            return serialize(doc, TXN_SENSITIVE)
        except (ValueError, KeyError):
            return {k: v for k, v in doc.items() if k != "encryption_metadata" and not k.endswith("_search")}

    for t in [_snap(doc) async for doc in txn_cursor]:
        cid = str(t.get("customer_id") or "")
        billed_map.setdefault(cid, {"name": t.get("customer_name") or "Unknown", "billed": 0.0})
        billed_map[cid]["billed"] += _num(t.get("total_amount"))
        billed_map[cid]["name"] = t.get("customer_name") or billed_map[cid]["name"]

    paid_rows = await payments_collection().aggregate([
        {"$group": {"_id": {"$toString": {"$ifNull": ["$customer_id", ""]}}, "amount": {"$sum": "$amount"}}},
    ]).to_list(length=None)
    paid_map: dict = {str(r.get("_id") or ""): r.get("amount") or 0.0 for r in paid_rows}

    result = []
    for cid in sorted(set(billed_map) | set(paid_map)):
        name = billed_map.get(cid, {}).get("name") or "Unknown"
        billed = billed_map.get(cid, {}).get("billed", 0.0)
        paid = paid_map.get(cid, 0.0)
        outstanding = max(billed - paid, 0.0)
        if outstanding > 0 or billed > 0:
            result.append({
                "customer_id": cid,
                "customer_name": name,
                "total_billed": round(billed, 2),
                "total_paid": round(paid, 2),
                "outstanding": round(outstanding, 2),
            })
    result.sort(key=lambda x: x["outstanding"], reverse=True)
    return result