import asyncio
from datetime import datetime, timezone
from datetime import timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends

from ..auth_helper import require_capability
from ..database.main_db import (
    customers_collection,
    items_collection,
    categories_collection,
    transactions_collection,
    payments_collection,
    expenses_collection,
    expense_categories_collection,
    employees_collection,
    attendance_collection,
    salary_advances_collection,
    salary_slips_collection,
)
from ..crypto_helper import decrypt_dict

router = APIRouter(tags=["Dashboard"])

TXN_SENSITIVE = ["customer_name", "invoice_number", "items", "notes"]
EXPENSE_SENSITIVE = ["description", "reference", "notes"]
CAT_SENSITIVE = ["name", "description"]
CUSTOMER_SENSITIVE = ["name", "contact_person", "phone", "email", "address", "notes"]
ITEM_SENSITIVE = ["name"]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _str_key(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime,)):
        return value.date().isoformat() if getattr(value, "date", None) else value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            return ""
    return str(value)


async def _txn_by_date() -> dict:
    """Map date -> {amount, qty} for all transactions (server-side aggregation)."""
    rows = await transactions_collection().aggregate([
        {"$group": {"_id": "$transaction_date", "amount": {"$sum": "$total_amount"}, "qty": {"$sum": "$total_quantity"}}}
    ]).to_list(length=None)
    out = {}
    for r in rows:
        key = _str_key(r.get("_id"))
        out[key] = {"amount": r.get("amount") or 0, "qty": r.get("qty") or 0}
    return out


async def _exp_by_date() -> dict:
    """Map date -> amount for all expenses (server-side aggregation)."""
    rows = await expenses_collection().aggregate([
        {"$group": {"_id": "$date", "amount": {"$sum": "$amount"}}}
    ]).to_list(length=None)
    out = {}
    for r in rows:
        out[_str_key(r.get("_id"))] = r.get("amount") or 0
    return out


async def _billed_by_customer() -> dict:
    rows = await transactions_collection().aggregate([
        {"$group": {"_id": {"$toString": {"$ifNull": ["$customer_id", ""]}}, "amount": {"$sum": "$total_amount"}}}
    ]).to_list(length=None)
    return {str(r.get("_id") or ""): r.get("amount") or 0 for r in rows}


async def _paid_by_customer() -> dict:
    rows = await payments_collection().aggregate([
        {"$group": {"_id": {"$toString": {"$ifNull": ["$customer_id", ""]}}, "amount": {"$sum": "$amount"}}}
    ]).to_list(length=None)
    return {str(r.get("_id") or ""): r.get("amount") or 0 for r in rows}


async def _customer_revenue_top6() -> list:
    """Top 6 customers by aggregate revenue (server-side)."""
    return await transactions_collection().aggregate([
        {"$group": {"_id": {"$toString": {"$ifNull": ["$customer_id", ""]}}, "revenue": {"$sum": "$total_amount"}}},
        {"$sort": {"revenue": -1}},
        {"$limit": 6},
    ]).to_list(length=None)


async def _expense_breakdown_rows() -> list:
    """Top 6 expense categories by aggregate amount (server-side)."""
    return await expenses_collection().aggregate([
        {"$group": {"_id": {"$ifNull": ["$category_id", "Uncategorized"]}, "amount": {"$sum": "$amount"}}},
        {"$sort": {"amount": -1}},
        {"$limit": 6},
    ]).to_list(length=None)


async def _top_items() -> dict:
    """Top items by revenue. `items` is encrypted per-document, so it must be
    projected and decrypted in Python — only the needed column is transferred."""
    by_item: dict = {}
    cursor = transactions_collection().find({}, {"items": 1})
    async for doc in cursor:
        if "items" not in doc:
            continue
        try:
            dec = decrypt_dict(doc, ["items"])
        except (ValueError, KeyError):
            continue
        for it in dec.get("items") or []:
            iid = str(it.get("item_id") or "")
            if not iid:
                continue
            rec = by_item.setdefault(iid, {"name": it.get("item_name") or "Unknown", "revenue": 0.0})
            rec["revenue"] += _num(it.get("line_total"))
            rec["name"] = it.get("item_name") or rec["name"]
    return by_item


async def _outstanding_advances_sum() -> float:
    """Sum of positive outstanding balances across OUTSTANDING advances."""
    total = 0.0
    async for a in salary_advances_collection().find({"status": "OUTSTANDING"}, {"outstanding": 1}):
        amt = _num(a.get("outstanding"))
        if amt > 0:
            total += amt
    return total


@router.get("/dashboard")
async def dashboard(
    current_user: dict = Depends(require_capability("dashboard:read")),
):
    now = datetime.now(timezone.utc)
    today = _today()
    month_prefix = now.strftime("%Y-%m")
    six_months_ago = (now - timedelta(days=183)).date().isoformat()

    # All level-1 reads are independent — issue them concurrently instead of
    # ~13 sequential round-trips (result values are unchanged).
    (txn_by_date, exp_by_date, billed_map, paid_map, customer_rows, exp_rows,
     by_item, att_today, active_employees, total_customers,
     draft_slips_month, unpaid_slips_month, outstanding_advances) = await asyncio.gather(
        _txn_by_date(),
        _exp_by_date(),
        _billed_by_customer(),
        _paid_by_customer(),
        _customer_revenue_top6(),
        _expense_breakdown_rows(),
        _top_items(),
        attendance_collection().find({"date": today}).to_list(length=None),
        employees_collection().count_documents({"is_active": True}),
        customers_collection().count_documents({}),
        salary_slips_collection().count_documents({
            "period_start": {"$regex": f"^{month_prefix}"},
            "status": "DRAFT",
        }),
        salary_slips_collection().count_documents({
            "period_start": {"$regex": f"^{month_prefix}"},
            "status": {"$in": ["FINALIZED", "PAID"]},
            "paid": {"$ne": True},
        }),
        _outstanding_advances_sum(),
    )

    today_revenue = round(_num(txn_by_date.get(today, {}).get("amount")), 2)
    month_revenue = round(sum(v["amount"] for k, v in txn_by_date.items() if k.startswith(month_prefix)), 2)
    six_month_revenue = round(sum(v["amount"] for k, v in txn_by_date.items() if k >= six_months_ago), 2)
    total_pieces = round(sum(v["qty"] for v in txn_by_date.values()), 2)
    total_expenses = round(sum(exp_by_date.values()), 2)
    month_expenses = round(sum(a for k, a in exp_by_date.items() if k.startswith(month_prefix)), 2)

    net_profit = round(month_revenue - month_expenses, 2)

    # Monthly revenue & expenses (last 6 months)
    monthly_revenue = []
    monthly_profit = []
    for i in range(5, -1, -1):
        ref = now.replace(day=1) - timedelta(days=30 * i)
        prefix = ref.strftime("%Y-%m")
        label = ref.strftime("%b")
        rev = round(sum(v["amount"] for k, v in txn_by_date.items() if k.startswith(prefix)), 2)
        exp = round(sum(a for k, a in exp_by_date.items() if k.startswith(prefix)), 2)
        monthly_revenue.append({"month": label, "revenue": rev, "expenses": exp})
        monthly_profit.append({"month": label, "profit": round(rev - exp, 2)})

    # Outstanding payments (billed - paid per customer)
    outstanding = sum(max(billed_map.get(cid, 0.0) - paid_map.get(cid, 0.0), 0.0) for cid in set(billed_map) | set(paid_map))

    # Top customers by revenue
    name_cache: dict = {}
    top_customers = []
    for r in customer_rows:
        cid = str(r.get("_id") or "")
        if cid and cid not in name_cache:
            name_cache[cid] = None
            if ObjectId.is_valid(cid):
                cust = await customers_collection().find_one({"_id": ObjectId(cid)})
                if cust:
                    try:
                        name_cache[cid] = decrypt_dict(cust, CUSTOMER_SENSITIVE).get("name")
                    except (ValueError, KeyError):
                        name_cache[cid] = None
        name = (name_cache.get(cid) or "Unknown").strip() or "Unknown"
        top_customers.append({"name": name, "revenue": round(r.get("revenue") or 0, 2)})

    # Top items by revenue
    top_items = [
        {"name": rec["name"], "revenue": round(rec["revenue"], 2)}
        for rec in sorted(by_item.values(), key=lambda x: x["revenue"], reverse=True)[:5]
    ]

    # Expense breakdown (aggregated by category, names decrypted on demand)
    cat_name_cache: dict = {}
    expense_breakdown = []
    for r in exp_rows:
        cid = r.get("_id") or "Uncategorized"
        name = cid
        if isinstance(cid, (ObjectId,)) or (isinstance(cid, str) and ObjectId.is_valid(cid)):
            key = str(cid)
            if key not in cat_name_cache:
                cat = await expense_categories_collection().find_one({"_id": ObjectId(cid)})
                cat_name_cache[key] = decrypt_dict(cat, CAT_SENSITIVE).get("name") if cat else None
            name = cat_name_cache.get(key) or str(cid)
        expense_breakdown.append({"category": name, "amount": round(r.get("amount") or 0, 2)})

    # Workforce stats
    present_today = sum(1 for a in att_today if (a.get("status") or "").upper() in ("PRESENT", "HALF_DAY"))
    on_leave_today = sum(1 for a in att_today if (a.get("status") or "").upper() in ("PAID_LEAVE", "ON_LEAVE", "UNPAID_LEAVE"))

    return {
        "today_revenue": today_revenue,
        "month_revenue": month_revenue,
        "six_month_revenue": six_month_revenue,
        "net_profit": net_profit,
        "total_pieces": total_pieces,
        "total_customers": total_customers,
        "total_expenses": total_expenses,
        "outstanding_payments": round(outstanding, 2),
        "active_employees": active_employees,
        "present_today": present_today,
        "on_leave_today": on_leave_today,
        "draft_slips_month": draft_slips_month,
        "unpaid_slips_month": unpaid_slips_month,
        "outstanding_advances": round(outstanding_advances, 2),
        "monthly_revenue": monthly_revenue,
        "monthly_profit": monthly_profit,
        "top_customers": top_customers,
        "top_items": top_items,
        "expense_breakdown": expense_breakdown,
    }