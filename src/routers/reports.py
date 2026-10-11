import calendar
from datetime import date as date_type
from datetime import datetime, timezone
from datetime import timedelta
import sys
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..database.main_db import (
    employees_collection,
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
from ..app_time import this_month_number, this_year, today_str
from ..router_utils import serialize

router = APIRouter(tags=["Reports"])

TXN_SENSITIVE = ["customer_name", "invoice_number", "items", "notes"]
PAY_SENSITIVE = ["customer_name", "reference", "notes"]
PAY_CUSTOMER_SENSITIVE = ["name", "contact_person", "phone", "email", "address", "notes"]
ITEM_SENSITIVE = ["name"]
CAT_SENSITIVE = ["name", "description"]
EXPENSE_SENSITIVE = ["description", "reference", "notes"]
SALARY_SENSITIVE = ["notes"]

MASTER_REVENUE_SOURCE = "gate_pass_quotation_prices"

_BILL_SERVICE_SRC = Path(__file__).resolve().parents[3] / "bill-service" / "src"
if str(_BILL_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_BILL_SERVICE_SRC))


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _today() -> str:
    """Today on the Sri Lankan clock — the day the staff actually work."""
    return today_str()


def _normalize_gate_pass_date(value) -> Optional[date_type]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        text = value.strip()[:10]
        if not text:
            return None
        try:
            return date_type.fromisoformat(text)
        except ValueError:
            return None
    return None


async def _list_gate_passes_for_range(start_date: str, end_date: str) -> list:
    """List gate passes in a date window using the bill-service source of truth.

    The reporting engine must recognize revenue from gate passes and quotation
    prices, not from billed totals or invoice records. If the cross-service data
    is unavailable in this process, the requester gets an empty-income result
    rather than a false billing-driven number.
    """
    try:
        from importlib import import_module

        gatepasses_collection = import_module("bill_service.database.main_db").gatepasses_collection
    except Exception:
        return []

    try:
        start = date_type.fromisoformat(start_date)
        end = date_type.fromisoformat(end_date)
    except ValueError:
        return []

    docs = []
    try:
        async for doc in gatepasses_collection.find({}):
            pass_date = _normalize_gate_pass_date(doc.get("receiving_date"))
            if pass_date is None:
                continue
            if start <= pass_date <= end:
                docs.append(doc)
    except Exception:
        return []
    return docs


async def _resolve_quotation_prices(quotation_id: Optional[str]) -> dict:
    """Return item -> {price, category} from the quotation source of truth."""
    if not quotation_id:
        return {}

    try:
        from importlib import import_module

        get_client = import_module("bill_service.database.connection_manager").get_client
    except Exception:
        return {}

    try:
        q_oid = ObjectId(quotation_id)
    except Exception:
        return {}

    motor_client = get_client("MAIN")
    for db_name in ("quotations_db", "quotations", "laundry_db"):
        try:
            coll = motor_client[db_name]["quotations"]
            quotation_doc = await coll.find_one({"_id": q_oid})
        except Exception:
            continue
        if not quotation_doc:
            continue
        try:
            decrypted = decrypt_dict(quotation_doc, ["client_name", "quotation_title", "line_items"])
        except Exception:
            decrypted = quotation_doc

        prices = {}
        for item in decrypted.get("line_items", []) or []:
            if not isinstance(item, dict):
                continue
            item_name = (item.get("item_name") or "").strip()
            if not item_name:
                continue
            prices[item_name] = {
                "price": _num(item.get("unit_price") or item.get("price") or 0.0),
                "category": item.get("category"),
            }
        if prices:
            return prices
    return {}


def _gate_pass_summary(gate_passes: list, quotation_map: dict) -> tuple:
    hotel_summary: dict = {}
    item_summary: dict = {}
    total_income = 0.0
    total_quantity = 0.0
    unpriced_items = 0

    for gp in gate_passes:
        try:
            decoded = decrypt_dict(gp, ["client_name", "items", "notes"])
        except Exception:
            decoded = gp

        client_name = (decoded.get("client_name") or "Unknown").strip() or "Unknown"
        quote_id = decoded.get("quotation_id")
        prices = quotation_map.get(quote_id) if quote_id else {}
        if quote_id and not prices:
            prices = {}

        hotel_bucket = hotel_summary.setdefault(
            client_name,
            {"client_name": client_name, "income": 0.0, "quantity": 0.0, "gate_pass_count": 0},
        )
        hotel_bucket["gate_pass_count"] += 1

        for item in decoded.get("items", []) or []:
            item_name = (item.get("item_name") or "").strip()
            qty = _num(item.get("received_qty"))
            if not item_name:
                continue
            total_quantity += qty
            price_info = prices.get(item_name) or {}
            unit_price = _num(price_info.get("price") or price_info.get("unit_price"))
            line_income = qty * unit_price
            if unit_price <= 0:
                unpriced_items += 1
            total_income += line_income
            hotel_bucket["income"] += line_income
            hotel_bucket["quantity"] += qty

            item_bucket = item_summary.setdefault(
                item_name,
                {"item_name": item_name, "income": 0.0, "quantity": 0.0, "hotel_count": set()},
            )
            item_bucket["income"] += line_income
            item_bucket["quantity"] += qty
            item_bucket["hotel_count"].add(client_name)

    hotel_rows = [{
        "client_name": v["client_name"],
        "income": round(v["income"], 2),
        "quantity": round(v["quantity"], 2),
        "gate_pass_count": v["gate_pass_count"],
    } for v in hotel_summary.values()]
    hotel_rows.sort(key=lambda x: (-x["income"], x["client_name"]))

    item_rows = [{
        "item_name": v["item_name"],
        "income": round(v["income"], 2),
        "quantity": round(v["quantity"], 2),
        "hotel_count": len(v["hotel_count"]),
    } for v in item_summary.values()]
    item_rows.sort(key=lambda x: (-x["income"], x["item_name"]))

    return {
        "total_income": round(total_income, 2),
        "total_quantity": round(total_quantity, 2),
        "unpriced_items": unpriced_items,
        "hotel_summary": hotel_rows,
        "item_summary": item_rows,
    }, item_rows


def _prorate_monthly_salary(calculation: dict, month_days: int, covered_days: int) -> dict:
    """Accrue monthly salary components by calendar days in the selected period."""
    if month_days <= 0 or covered_days <= 0:
        return calculation

    ratio = covered_days / month_days
    if calculation.get("salary_type") == "MONTHLY":
        if not calculation.get("attendance_required"):
            calculation["base_salary_for_period"] = round(
                _num(calculation.get("basic_salary")) * ratio, 2
            )
        if not calculation.get("attendance_required") or calculation.get("allowance_type") == "FIXED":
            calculation["allowance_for_period"] = round(
                _num(calculation.get("allowance")) * ratio, 2
            )

        for component in calculation.get("components", []):
            component["amount"] = round(_num(component.get("amount")) * ratio, 2)
        calculation["bonus"] = round(_num(calculation.get("bonus")) * ratio, 2)
        calculation["other_payments"] = round(_num(calculation.get("other_payments")) * ratio, 2)
        calculation["other_deductions"] = round(_num(calculation.get("other_deductions")) * ratio, 2)

        epf_basis = _num(calculation.get("epf_basis"))
        if calculation.get("epf_base") == "FULL":
            epf_basis = _num(calculation.get("basic_salary")) * ratio
        elif not calculation.get("attendance_required"):
            epf_basis = _num(calculation.get("base_salary_for_period"))
        calculation["epf_basis"] = round(epf_basis, 2)
        calculation["epf_employee"] = round(epf_basis * _num(calculation.get("epf_rate")) / 100, 2)
        calculation["epf_employer"] = round(epf_basis * _num(calculation.get("epf_rate")) / 100, 2)
        calculation["etf_employer"] = round(epf_basis * _num(calculation.get("etf_rate")) / 100, 2)

    calculation["gross_salary"] = round(
        _num(calculation.get("base_salary_for_period"))
        + _num(calculation.get("overtime_pay"))
        + _num(calculation.get("extra_work_total"))
        + _num(calculation.get("allowance_for_period"))
        + _num(calculation.get("bonus"))
        + _num(calculation.get("other_payments")),
        2,
    )
    calculation["total_deductions"] = round(
        _num(calculation.get("epf_employee"))
        + _num(calculation.get("advance_deductions"))
        + _num(calculation.get("other_deductions")),
        2,
    )
    calculation["net_salary"] = round(
        calculation["gross_salary"] - calculation["total_deductions"], 2
    )
    calculation["salary_expense"] = round(
        calculation["gross_salary"]
        + _num(calculation.get("epf_employer"))
        + _num(calculation.get("etf_employer")),
        2,
    )
    return calculation


async def _salary_expense_summary(start: date_type, end: date_type) -> dict:
    """Calculate employee payroll accruals using the payroll engine."""
    from .salary import _calculate_period_salary, _prefetch_payroll_data

    employees = []
    cursor = employees_collection().find({"is_active": {"$ne": False}, "status": {"$ne": "INACTIVE"}})
    async for raw_employee in cursor:
        try:
            employee = decrypt_dict(raw_employee, ["name", "phone", "nic", "notes"])
        except Exception:
            employee = raw_employee
        if raw_employee.get("_id"):
            employees.append((raw_employee, employee))

    totals_by_employee: dict = {}
    period_details = []
    month_start = start.replace(day=1)
    while month_start <= end:
        month_days = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = month_start.replace(day=month_days)
        period_start = max(start, month_start)
        period_end = min(end, month_end)
        period_start_str = period_start.isoformat()
        period_end_str = period_end.isoformat()
        employee_ids = [str(raw["_id"]) for raw, _ in employees]
        preloaded = await _prefetch_payroll_data(employee_ids, period_start_str, period_end_str)

        for raw_employee, employee in employees:
            joined = _normalize_gate_pass_date(employee.get("joined_date"))
            leaving = _normalize_gate_pass_date(employee.get("leaving_date"))
            if (joined and joined > period_end) or (leaving and leaving < period_start):
                continue

            calculation = await _calculate_period_salary(
                raw_employee,
                employee,
                period_start_str,
                period_end_str,
                period_type="REPORT",
                preloaded=preloaded,
            )
            effective_start = _normalize_gate_pass_date(calculation.get("effective_start")) or period_start
            effective_end = _normalize_gate_pass_date(calculation.get("effective_end")) or period_end
            covered_start = max(period_start, effective_start)
            covered_end = min(period_end, effective_end)
            covered_days = max((covered_end - covered_start).days + 1, 0)
            calculation = _prorate_monthly_salary(calculation, month_days, covered_days)
            calculation["period_start"] = period_start_str
            calculation["period_end"] = period_end_str
            period_details.append(calculation)

            employee_id = calculation["employee_id"]
            summary = totals_by_employee.setdefault(employee_id, {
                "employee_id": employee_id,
                "employee_name": calculation.get("employee_name") or "Unknown",
                "salary_type": calculation.get("salary_type"),
                "attendance_required": calculation.get("attendance_required"),
                "worked_days": 0.0,
                "absent_days": 0.0,
                "base_salary": 0.0,
                "overtime_pay": 0.0,
                "allowances": 0.0,
                "bonus": 0.0,
                "other_payments": 0.0,
                "extra_work_total": 0.0,
                "epf_employer": 0.0,
                "etf_employer": 0.0,
                "gross_salary": 0.0,
                "salary_expense": 0.0,
                "periods": [],
            })
            summary["worked_days"] += _num(calculation.get("worked_days"))
            summary["absent_days"] += _num(calculation.get("absent_days"))
            summary["base_salary"] += _num(calculation.get("base_salary_for_period"))
            summary["overtime_pay"] += _num(calculation.get("overtime_pay"))
            summary["allowances"] += _num(calculation.get("allowance_for_period"))
            summary["bonus"] += _num(calculation.get("bonus"))
            summary["other_payments"] += _num(calculation.get("other_payments"))
            summary["extra_work_total"] += _num(calculation.get("extra_work_total"))
            summary["epf_employer"] += _num(calculation.get("epf_employer"))
            summary["etf_employer"] += _num(calculation.get("etf_employer"))
            summary["gross_salary"] += _num(calculation.get("gross_salary"))
            summary["salary_expense"] += _num(calculation.get("salary_expense"))
            summary["periods"].append(calculation)

        if month_end >= end:
            break
        month_start = month_end + timedelta(days=1)

    employee_rows = list(totals_by_employee.values())
    for employee in employee_rows:
        for key in (
            "worked_days", "absent_days", "base_salary", "overtime_pay", "allowances",
            "bonus", "other_payments", "extra_work_total", "epf_employer", "etf_employer",
            "gross_salary", "salary_expense",
        ):
            employee[key] = round(employee[key], 2)
    employee_rows.sort(key=lambda row: row["employee_name"].casefold())
    return {
        "salary_expense_total": round(sum(row["salary_expense"] for row in employee_rows), 2),
        "employees": employee_rows,
        "period_details": period_details,
    }


async def _master_report_data(start_date: str, end_date: str) -> dict:
    gate_passes = await _list_gate_passes_for_range(start_date, end_date)
    quotation_cache = {}
    for gp in gate_passes:
        quote_id = (gp.get("quotation_id") or "").strip()
        if quote_id and quote_id not in quotation_cache:
            quotation_cache[quote_id] = await _resolve_quotation_prices(quote_id)

    summary, _ = _gate_pass_summary(gate_passes, quotation_cache)
    expenses = await _dec_expenses({"date": {"$gte": start_date, "$lte": end_date}})
    other_expenses = round(sum(_num(e.get("amount")) for e in expenses), 2)
    payroll = await _salary_expense_summary(
        date_type.fromisoformat(start_date),
        date_type.fromisoformat(end_date),
    )
    total_expenses = round(other_expenses + payroll["salary_expense_total"], 2)
    summary["source_of_truth"] = MASTER_REVENUE_SOURCE
    summary["other_expense_total"] = other_expenses
    summary["salary_expense_total"] = payroll["salary_expense_total"]
    summary["expense_total"] = total_expenses
    summary["total_expenses"] = total_expenses
    summary["net_profit"] = round(summary["total_income"] - total_expenses, 2)
    summary["salary_expenses"] = payroll["employees"]
    summary["salary_period_details"] = payroll["period_details"]
    summary["other_expense_details"] = expenses
    summary["start_date"] = start_date
    summary["end_date"] = end_date
    return summary


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
    date: Optional[str] = Query(None, alias="date"),
    report_date: Optional[str] = Query(None, alias="report_date"),
    current_user: dict = Depends(require_capability("report:read")),
):
    selected_date = date or report_date or _today()
    try:
        date_type.fromisoformat(selected_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.") from exc

    txns = await _dec_transactions({"transaction_date": selected_date})
    total_qty = round(sum(_num(t.get("total_quantity")) for t in txns), 2)
    revenue = round(sum(_num(t.get("total_amount")) for t in txns), 2)
    cogs = round(sum(_num(t.get("total_cost")) for t in txns), 2)
    expenses = await _dec_expenses({"date": selected_date})
    other_expenses = round(sum(_num(e.get("amount")) for e in expenses), 2)
    payroll = await _salary_expense_summary(
        date_type.fromisoformat(selected_date),
        date_type.fromisoformat(selected_date),
    )
    total_expenses = round(other_expenses + payroll["salary_expense_total"], 2)
    net_profit = round(revenue - cogs - total_expenses, 2)
    return {
        "date": selected_date,
        "transactions": len(txns),
        "total_quantity": total_qty,
        "total_revenue": revenue,
        "other_expense_total": other_expenses,
        "salary_expense_total": payroll["salary_expense_total"],
        "salary_expenses": payroll["employees"],
        "total_expenses": total_expenses,
        "gross_profit": round(revenue - cogs, 2),
        "net_profit": net_profit,
    }


@router.get("/reports/range")
async def range_report(
    start_date: Optional[str] = Query(None, alias="start_date"),
    end_date: Optional[str] = Query(None, alias="end_date"),
    current_user: dict = Depends(require_capability("report:read")),
):
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required.")

    try:
        start = date_type.fromisoformat(start_date)
        end = date_type.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.") from exc

    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")

    daily_rows = []
    total_revenue = 0.0
    total_cogs = 0.0
    total_expenses = 0.0
    total_quantity = 0.0

    current = start
    while current <= end:
        current_str = current.isoformat()
        txns = await _dec_transactions({"transaction_date": current_str})
        expenses = await _dec_expenses({"date": current_str})

        daily_revenue = round(sum(_num(t.get("total_amount")) for t in txns), 2)
        daily_cogs = round(sum(_num(t.get("total_cost")) for t in txns), 2)
        daily_expenses = round(sum(_num(e.get("amount")) for e in expenses), 2)
        daily_quantity = round(sum(_num(t.get("total_quantity")) for t in txns), 2)

        total_revenue += daily_revenue
        total_cogs += daily_cogs
        total_expenses += daily_expenses
        total_quantity += daily_quantity

        daily_rows.append(
            {
                "date": current_str,
                "transactions": len(txns),
                "total_quantity": daily_quantity,
                "total_revenue": daily_revenue,
                "total_expenses": daily_expenses,
                "gross_profit": round(daily_revenue - daily_cogs, 2),
                "net_profit": round(daily_revenue - daily_cogs - daily_expenses, 2),
            }
        )
        current += timedelta(days=1)

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": len(daily_rows),
        "transactions": sum(row["transactions"] for row in daily_rows),
        "total_quantity": round(total_quantity, 2),
        "total_revenue": round(total_revenue, 2),
        "total_expenses": round(total_expenses, 2),
        "gross_profit": round(total_revenue - total_cogs, 2),
        "net_profit": round(total_revenue - total_cogs - total_expenses, 2),
        "daily": daily_rows,
    }


@router.get("/reports/monthly")
async def monthly_report(
    year: int = Query(this_year()),
    month: int = Query(this_month_number()),
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
    expenses = await _dec_expenses({"date": {"$gte": start, "$lt": end}})
    other_expenses = round(sum(_num(e.get("amount")) for e in expenses), 2)
    payroll = await _salary_expense_summary(
        date_type.fromisoformat(start),
        date_type.fromisoformat(end) - timedelta(days=1),
    )
    total_expenses = round(other_expenses + payroll["salary_expense_total"], 2)

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
        "other_expense_total": other_expenses,
        "salary_expense_total": payroll["salary_expense_total"],
        "salary_expenses": payroll["employees"],
        "total_expenses": total_expenses,
        "gross_profit": round(revenue - cogs, 2),
        "net_profit": round(revenue - cogs - total_expenses, 2),
        "revenue_by_customer": revenue_by_customer,
    }


@router.get("/reports/master")
async def master_report(
    start_date: Optional[str] = Query(None, alias="start_date"),
    end_date: Optional[str] = Query(None, alias="end_date"),
    current_user: dict = Depends(require_capability("report:read")),
):
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required.")

    try:
        start = date_type.fromisoformat(start_date)
        end = date_type.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.") from exc

    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")

    return await _master_report_data(start.isoformat(), end.isoformat())


@router.get("/reports/hotel-wise")
async def hotel_wise_report(
    start_date: Optional[str] = Query(None, alias="start_date"),
    end_date: Optional[str] = Query(None, alias="end_date"),
    current_user: dict = Depends(require_capability("report:read")),
):
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required.")

    payload = await _master_report_data(start_date, end_date)
    return payload["hotel_summary"]


@router.get("/reports/item-wise")
async def item_wise_report(
    start_date: Optional[str] = Query(None, alias="start_date"),
    end_date: Optional[str] = Query(None, alias="end_date"),
    current_user: dict = Depends(require_capability("report:read")),
):
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required.")

    payload = await _master_report_data(start_date, end_date)
    return payload["item_summary"]


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


@router.get("/reports/health")
async def reports_health(
    current_user: dict = Depends(require_capability("report:read")),
):
    return {
        "status": "ok",
        "service": "laundry-management-reports",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "transactions": "available",
            "expenses": "available",
        },
    }


@router.get("/reports/data-quality")
async def data_quality_report(
    start_date: Optional[str] = Query(None, alias="start_date"),
    end_date: Optional[str] = Query(None, alias="end_date"),
    current_user: dict = Depends(require_capability("report:read")),
):
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required.")

    try:
        start = date_type.fromisoformat(start_date)
        end = date_type.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.") from exc

    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")

    missing_days = []
    current = start
    while current <= end:
        current_str = current.isoformat()
        txns = await _dec_transactions({"transaction_date": current_str})
        expenses = await _dec_expenses({"date": current_str})
        if not txns and not expenses:
            missing_days.append(current_str)
        current += timedelta(days=1)

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "missing_data_days": missing_days,
        "missing_data_count": len(missing_days),
        "status": "complete" if not missing_days else "incomplete_data",
    }