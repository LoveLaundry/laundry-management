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
)
from ..crypto_helper import decrypt_dict
from ..router_utils import serialize

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


async def _dec_transactions(query: dict) -> list:
    result = []
    for doc in await transactions_collection().find(query).to_list(length=None):
        try:
            result.append(serialize(doc, TXN_SENSITIVE))
        except (ValueError, KeyError):
            pass
    return result


async def _dec_expenses(query: dict) -> list:
    result = []
    for doc in await expenses_collection().find(query).to_list(length=None):
        try:
            result.append(serialize(doc, EXPENSE_SENSITIVE))
        except (ValueError, KeyError):
            pass
    return result


def _income(month_label: str, txns: list) -> float:
    return sum(_num(t.get("total_amount")) for t in txns)


@router.get("/dashboard")
async def dashboard(
    current_user: dict = Depends(require_capability("dashboard:read")),
):
    now = datetime.now(timezone.utc)
    today = _today()
    month_prefix = now.strftime("%Y-%m")

    # Period queries
    txns = await _dec_transactions({})
    expenses_all = await _dec_expenses({})

    today_revenue = round(sum(_num(t.get("total_amount")) for t in txns if t.get("transaction_date") == today), 2)
    month_revenue = round(sum(_num(t.get("total_amount")) for t in txns if str(t.get("transaction_date") or "").startswith(month_prefix)), 2)

    six_months_ago = (now - timedelta(days=183)).date().isoformat()
    six_month_txns = [t for t in txns if str(t.get("transaction_date") or "") >= six_months_ago]
    six_month_revenue = round(sum(_num(t.get("total_amount")) for t in six_month_txns), 2)

    total_pieces = round(sum(_num(t.get("total_quantity")) for t in txns), 2)
    total_customers = await customers_collection().count_documents({})

    total_expenses = round(sum(_num(e.get("amount")) for e in expenses_all), 2)
    month_expenses = round(sum(_num(e.get("amount")) for e in expenses_all if str(e.get("date") or "").startswith(month_prefix)), 2)

    # Outstanding payments
    billed_map: dict = {}
    for t in txns:
        cid = str(t.get("customer_id") or "")
        billed_map[cid] = billed_map.get(cid, 0.0) + _num(t.get("total_amount"))
    paid_map: dict = {}
    for doc in await payments_collection().find().to_list(length=None):
        try:
            pay = serialize(doc, ["customer_name", "reference", "notes"])
        except (ValueError, KeyError):
            continue
        cid = str(pay.get("customer_id") or "")
        paid_map[cid] = paid_map.get(cid, 0.0) + _num(pay.get("amount"))
    outstanding = sum(max(billed_map.get(cid, 0.0) - paid_map.get(cid, 0.0), 0.0) for cid in set(billed_map) | set(paid_map))

    net_profit = round(month_revenue - month_expenses, 2)

    # Monthly revenue & expenses (last 6 months)
    monthly_revenue = []
    monthly_profit = []
    for i in range(5, -1, -1):
        ref = now.replace(day=1) - timedelta(days=30 * i)
        prefix = ref.strftime("%Y-%m")
        label = ref.strftime("%b")
        rev = round(sum(_num(t.get("total_amount")) for t in txns if str(t.get("transaction_date") or "").startswith(prefix)), 2)
        exp = round(sum(_num(e.get("amount")) for e in expenses_all if str(e.get("date") or "").startswith(prefix)), 2)
        monthly_revenue.append({"month": label, "revenue": rev, "expenses": exp})
        monthly_profit.append({"month": label, "profit": round(rev - exp, 2)})

    # Top customers by revenue (all time)
    by_customer: dict = {}
    for t in txns:
        cid = str(t.get("customer_id") or "")
        rec = by_customer.setdefault(cid, {"name": t.get("customer_name") or "Unknown", "revenue": 0.0})
        rec["revenue"] += _num(t.get("total_amount"))
        rec["name"] = t.get("customer_name") or rec["name"]
    top_customers = [
        {"name": rec["name"], "revenue": round(rec["revenue"], 2)}
        for rec in sorted(by_customer.values(), key=lambda x: x["revenue"], reverse=True)[:5]
    ]

    # Top items by revenue (from decrypted line items)
    by_item: dict = {}
    cat_cache: dict = {}
    for t in txns:
        for it in t.get("items") or []:
            iid = str(it.get("item_id") or "")
            if not iid:
                continue
            rec = by_item.setdefault(iid, {"name": it.get("item_name") or "Unknown", "revenue": 0.0})
            rec["revenue"] += _num(it.get("line_total"))
            rec["name"] = it.get("item_name") or rec["name"]
    top_items = [
        {"name": rec["name"], "revenue": round(rec["revenue"], 2)}
        for rec in sorted(by_item.values(), key=lambda x: x["revenue"], reverse=True)[:5]
    ]

    # Expense breakdown (all time)
    by_exp_cat: dict = {}
    for e in expenses_all:
        cid = e.get("category_id") or "Uncategorized"
        name = cid
        if cid != "Uncategorized" and ObjectId.is_valid(cid):
            cat_doc = await expense_categories_collection().find_one({"_id": ObjectId(cid)})
            if cat_doc:
                name = decrypt_dict(cat_doc, CAT_SENSITIVE).get("name") or name
        by_exp_cat[name] = by_exp_cat.get(name, 0.0) + _num(e.get("amount"))
    expense_breakdown = [
        {"category": k, "amount": round(v, 2)}
        for k, v in sorted(by_exp_cat.items(), key=lambda x: x[1], reverse=True)[:6]
    ]

    return {
        "today_revenue": today_revenue,
        "month_revenue": month_revenue,
        "six_month_revenue": six_month_revenue,
        "net_profit": net_profit,
        "total_pieces": total_pieces,
        "total_customers": total_customers,
        "total_expenses": total_expenses,
        "outstanding_payments": round(outstanding, 2),
        "monthly_revenue": monthly_revenue,
        "monthly_profit": monthly_profit,
        "top_customers": top_customers,
        "top_items": top_items,
        "expense_breakdown": expense_breakdown,
    }