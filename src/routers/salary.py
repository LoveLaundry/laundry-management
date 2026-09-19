import asyncio
import calendar
from datetime import date as date_cls, datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from ..auth_helper import require_capability
from ..database.main_db import (
    employees_collection,
    salary_slips_collection,
    salary_advances_collection,
    salary_packages_collection,
    attendance_collection,
    holidays_collection,
    extra_work_records_collection,
    extra_work_categories_collection,
    company_settings_collection,
    salaries_collection,
)
from ..crypto_helper import decrypt_dict
from ..models import (
    SalarySlipCreate, SalarySlipUpdate, AttendanceBulkDay,
    SalaryPackageUpsert, SalaryPackageUpdate,
)
from ..router_utils import serialize, log_audit
from ..error_responses import NotFoundError, BadRequestError, ConflictError

router = APIRouter(tags=["Salary Management"])

SENSITIVE_FIELDS = ["name", "phone", "nic", "notes"]
SALARY_SENSITIVE = ["notes"]
EMPLOYEE_SENSITIVE = ["name", "phone", "nic", "notes"]


def _parse_oid(value: str, label: str = "id") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise NotFoundError(label, value)
    return ObjectId(value)


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Helpers ────────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "company_name": "Love Laundry",
    "working_days_per_week": 6,
    "working_days_pattern": [0, 1, 2, 3, 4, 5],
    "default_overtime_rate": 0,
    "salary_basis_days": 30,
}


async def _get_company_settings() -> dict:
    doc = await company_settings_collection().find_one({"key": "main"})
    return {**DEFAULT_SETTINGS, **(doc or {})}


async def _get_holiday_dates(start: str, end: str) -> set:
    """Return ISO date strings in [start, end], expanding recurring (yearly) holidays."""
    start_date = date_cls.fromisoformat(start)
    end_date = date_cls.fromisoformat(end)
    result: set = set()

    cursor = holidays_collection().find({
        "$or": [
            {"date": {"$lte": end}},
            {"is_recurring": True, "date": {"$gt": end}},
        ]
    }).sort("date", 1)
    async for doc in cursor:
        h_date = doc.get("date")
        if not h_date:
            continue
        try:
            hd = date_cls.fromisoformat(h_date)
        except (ValueError, AttributeError):
            continue
        if start_date <= hd <= end_date:
            result.add(h_date)
        if doc.get("is_recurring"):
            year = start_date.year
            while year <= end_date.year:
                try:
                    candidate = hd.replace(year=year)
                    if start_date <= candidate <= end_date:
                        result.add(candidate.isoformat())
                except ValueError:
                    pass
                year += 1
    return result


async def _get_attendance(employee_id: str, start: str, end: str) -> list:
    query = {
        "employee_id": employee_id,
        "date": {"$gte": start, "$lte": end},
    }
    cursor = attendance_collection().find(query).sort("date", 1)
    return [doc async for doc in cursor]


async def _get_extra_work(employee_id: str, start: str, end: str) -> list:
    query = {
        "employee_id": employee_id,
        "date": {"$gte": start, "$lte": end},
    }
    cursor = extra_work_records_collection().find(query).sort("date", 1)
    return [doc async for doc in cursor]


async def _get_outstanding_advances(employee_id: str) -> list:
    cursor = salary_advances_collection().find({
        "employee_id": employee_id,
        "status": "OUTSTANDING",
    }).sort("date", 1)
    return [doc async for doc in cursor]


async def _collect(cursor) -> list:
    return [doc async for doc in cursor]


async def _prefetch_payroll_data(
    employee_ids: list,
    start_date: str,
    end_date: str,
) -> dict:
    """Load every payroll input for a batch of employees in a handful of queries
    instead of ~6 sequential round-trips per employee (N+1)."""
    ids = list(employee_ids)
    if not ids:
        return {
            "settings": await _get_company_settings(),
            "holiday_dates": set(),
            "packages": {},
            "attendance": {},
            "extra_work": {},
            "advances": {},
            "slips": {},
        }

    settings_fut = _get_company_settings()
    holidays_fut = _get_holiday_dates(start_date, end_date)
    packages_fut = _collect(salary_packages_collection().find({
        "employee_id": {"$in": ids},
        "month": start_date[:7],
    }))
    attendance_fut = _collect(attendance_collection().find({
        "employee_id": {"$in": ids},
        "date": {"$gte": start_date, "$lte": end_date},
    }))
    extra_work_fut = _collect(extra_work_records_collection().find({
        "employee_id": {"$in": ids},
        "date": {"$gte": start_date, "$lte": end_date},
    }))
    advances_fut = _collect(salary_advances_collection().find({
        "employee_id": {"$in": ids},
        "status": "OUTSTANDING",
    }).sort("date", 1))
    slips_fut = _collect(salary_slips_collection().find({
        "employee_id": {"$in": ids},
        "period_start": {"$lte": end_date},
        "period_end": {"$gte": start_date},
        "status": {"$nin": ["CANCELLED", "DELETED"]},
    }))

    settings, holiday_dates, packages, attendance, extra_work, advances, slips = await asyncio.gather(
        settings_fut, holidays_fut, packages_fut, attendance_fut,
        extra_work_fut, advances_fut, slips_fut,
    )

    by_package: dict = {}
    for doc in packages:
        by_package.setdefault(doc.get("employee_id"), doc)

    def _group(records: list) -> dict:
        grouped: dict = {}
        for rec in records:
            grouped.setdefault(rec.get("employee_id"), []).append(rec)
        for lst in grouped.values():
            lst.sort(key=lambda r: str(r.get("date") or ""))
        return {k: v for k, v in grouped.items() if k is not None}

    by_slip: dict = {}
    for doc in slips:
        by_slip.setdefault(doc.get("employee_id"), doc)

    return {
        "settings": settings,
        "holiday_dates": holiday_dates,
        "packages": by_package,
        "attendance": _group(attendance),
        "extra_work": _group(extra_work),
        "advances": _group(advances),
        "slips": by_slip,
    }


# ── Generic Period Salary Calculation ─────────────────────────────────────
async def _calculate_period_salary(
    emp: dict,
    emp_decrypted: dict,
    start_date: str,
    end_date: str,
    period_type: str = "MONTHLY",
    preloaded: Optional[dict] = None,
    full_attendance: bool = False,
) -> dict:
    """Calculate salary for an arbitrary date period (monthly / weekly / custom).

    When ``preloaded`` is provided (batch payroll), all reads come from the
    prefetched maps instead of issuing per-employee database queries.

    When ``full_attendance`` is True every working day counts as attended,
    regardless of what has been recorded so far — used to project what the
    month would pay out if all employees attend all remaining days.
    """
    employee_id = str(emp["_id"])

    if preloaded is not None:
        settings = preloaded["settings"]
        holiday_dates = preloaded["holiday_dates"]
        salary_pkg = preloaded["packages"].get(employee_id)
        attendance_records = preloaded["attendance"].get(employee_id, [])
        extra_work_records = preloaded["extra_work"].get(employee_id, [])
        outstanding_advances = preloaded["advances"].get(employee_id, [])
        existing_slip = preloaded["slips"].get(employee_id)
    else:
        settings = await _get_company_settings()

        salary_pkg = await salary_packages_collection().find_one({
            "employee_id": employee_id,
            "month": start_date[:7],
        })

        holiday_dates = await _get_holiday_dates(start_date, end_date)
        attendance_records = await _get_attendance(employee_id, start_date, end_date)
        extra_work_records = await _get_extra_work(employee_id, start_date, end_date)
        outstanding_advances = await _get_outstanding_advances(employee_id)

        existing_slip = await salary_slips_collection().find_one({
            "employee_id": employee_id,
            "period_start": {"$lte": end_date},
            "period_end": {"$gte": start_date},
            "status": {"$nin": ["CANCELLED", "DELETED"]},
        })

    def _arr(field, default):
        if salary_pkg is not None and field in salary_pkg and salary_pkg.get(field) is not None:
            return salary_pkg.get(field)
        return default

    salary_type = (_arr("salary_type", emp.get("salary_type", "MONTHLY")) or "MONTHLY").upper()
    attendance_required = bool(_arr("attendance_required", emp.get("attendance_required", True)))
    basic_salary = _num(_arr("basic_salary", emp.get("basic_salary")))
    daily_rate = _num(_arr("daily_rate", emp.get("daily_rate")))
    weekly_rate = _num(_arr("weekly_rate", emp.get("weekly_rate")))
    contract_amount = _num(_arr("contract_amount", emp.get("contract_amount")))
    employee_overtime_rate = _num(_arr("overtime_rate", emp.get("overtime_rate")))
    epf_rate = _num(_arr("epf_rate", emp.get("epf_rate")))
    etf_rate = _num(_arr("etf_rate", emp.get("etf_rate")))
    salary_components = _arr("salary_components", emp.get("salary_components")) or []

    working_days_pattern = settings.get("working_days_pattern", [0, 1, 2, 3, 4, 5])
    salary_basis_days = settings.get("salary_basis_days", 30)
    basis_days = max(_num(emp.get("salary_basis_days")), salary_basis_days, 1)

    if salary_type == "CONTRACT":
        calculation_method = "CONTRACT"
    elif salary_type == "DAILY":
        calculation_method = "DAILY_WORKED_DAYS"
    elif salary_type == "WEEKLY":
        calculation_method = "WEEKLY_FIXED" if not attendance_required else "WEEKLY_ATTENDANCE"
    else:
        calculation_method = "FIXED_MONTHLY" if not attendance_required else "MONTHLY_ATTENDANCE"

    sdt = date_cls.fromisoformat(start_date)
    edt = date_cls.fromisoformat(end_date)
    if edt < sdt:
        raise BadRequestError("Period end must be after period start")
    num_days = (edt - sdt).days + 1

    joined_date_str = emp.get("joined_date")
    leaving_date_str = emp.get("leaving_date")

    effective_start = sdt
    if joined_date_str:
        try:
            jd = date_cls.fromisoformat(joined_date_str)
            if jd > effective_start:
                effective_start = jd
        except (ValueError, AttributeError):
            pass

    effective_end = edt
    if leaving_date_str:
        try:
            ld = date_cls.fromisoformat(leaving_date_str)
            if ld < effective_end:
                effective_end = ld
        except (ValueError, AttributeError):
            pass

    if effective_end < effective_start:
        effective_end = effective_start

    total_working_days = 0
    worked_days = 0.0
    absent_days = 0.0
    leave_days = 0.0
    holiday_count = 0
    weekend_count = 0
    total_ot_hours = 0.0

    attendance_by_date = {}
    for rec in attendance_records:
        attendance_by_date[rec["date"]] = rec

    day = effective_start
    while day <= effective_end:
        date_str = day.isoformat()
        dow = day.weekday()

        if dow not in working_days_pattern:
            weekend_count += 1
            day = date_cls.fromordinal(day.toordinal() + 1)
            continue

        if date_str in holiday_dates:
            holiday_count += 1
            day = date_cls.fromordinal(day.toordinal() + 1)
            continue

        total_working_days += 1
        att = attendance_by_date.get(date_str)
        if full_attendance:
            worked_days += 1
            if att:
                total_ot_hours += _num(att.get("overtime_hours"))
            day = date_cls.fromordinal(day.toordinal() + 1)
            continue
        if att:
            status = (att.get("status") or "ABSENT").upper()
            if status in ("PRESENT",):
                worked_days += 1
            elif status in ("HALF_DAY",):
                worked_days += 0.5
                if attendance_required:
                    absent_days += 0.5
            elif status in ("ON_LEAVE", "PAID_LEAVE"):
                leave_days += 1
            elif status in ("UNPAID_LEAVE", "ABSENT"):
                if attendance_required:
                    absent_days += 1
            else:
                if attendance_required:
                    absent_days += 1
            total_ot_hours += _num(att.get("overtime_hours"))
        else:
            if attendance_required:
                absent_days += 1
        day = date_cls.fromordinal(day.toordinal() + 1)

    effective_working_days = worked_days + leave_days

    effective_num_days = (effective_end - effective_start).days + 1
    partial_employment = (effective_start != sdt) or (effective_end != edt)

    adjusted_base = 0.0
    base_salary_for_period = 0.0

    if salary_type == "MONTHLY":
        if attendance_required:
            adjusted_base = round(basic_salary * effective_num_days / basis_days, 2) if basic_salary > 0 else 0
            base_salary_for_period = round(adjusted_base * effective_working_days / effective_num_days, 2) if adjusted_base > 0 else 0
        else:
            # FIXED_MONTHLY: pay the full configured amount regardless of 28/30/31 day months.
            # Only prorate for partial employment (joined/left mid-period).
            adjusted_base = round(basic_salary, 2)
            base_salary_for_period = round(adjusted_base * effective_num_days / basis_days, 2) if partial_employment else adjusted_base
    elif salary_type == "DAILY":
        base_salary_for_period = round(daily_rate * worked_days, 2)
        adjusted_base = basic_salary or round(daily_rate * settings.get("working_days_per_week", 6), 2)
    elif salary_type == "WEEKLY":
        weekly_amt = weekly_rate or round(daily_rate * settings.get("working_days_per_week", 6), 2)
        if attendance_required:
            working_days_per_week = max(settings.get("working_days_per_week", 6), 1)
            base_salary_for_period = round(weekly_amt * worked_days / working_days_per_week, 2)
            adjusted_base = round(weekly_amt * effective_num_days / 7, 2) if weekly_amt > 0 else 0
        else:
            adjusted_base = round(weekly_amt * effective_num_days / 7, 2) if weekly_amt > 0 else 0
            base_salary_for_period = adjusted_base
    elif salary_type == "CONTRACT":
        adjusted_base = round(contract_amount or basic_salary, 2)
        base_salary_for_period = round(adjusted_base * effective_num_days / basis_days, 2) if partial_employment else adjusted_base

    overtime_pay = 0.0
    overtime_rate = employee_overtime_rate or settings.get("default_overtime_rate", 0)
    if overtime_rate > 0 and total_ot_hours > 0:
        overtime_pay = round(total_ot_hours * overtime_rate, 2)

    allowance_fixed = _num(_arr("allowance", emp.get("allowance")))
    allowance_type = (_arr("allowance_type", emp.get("allowance_type")) or "FIXED").upper()
    if allowance_type in ("DAYS", "ADJUSTED"):
        allowance_type = "ADJUSTED"
    allowance_for_period = 0.0
    if allowance_fixed > 0:
        if not attendance_required:
            allowance_for_period = round(allowance_fixed, 2)
        elif allowance_type == "ADJUSTED":
            if salary_type == "MONTHLY" and basic_salary > 0 and base_salary_for_period > 0:
                ratio = round(base_salary_for_period / basic_salary, 4)
            else:
                ratio = round(effective_working_days / max(effective_num_days, 1), 4)
            allowance_for_period = round(allowance_fixed * ratio, 2)
        elif allowance_type == "ATTENDANCE":
            if salary_type == "MONTHLY" and basic_salary > 0:
                ratio = round((adjusted_base * worked_days / max(effective_num_days, 1)) / max(basic_salary, 1), 4)
            else:
                ratio = round(worked_days / max(effective_num_days, 1), 4)
            allowance_for_period = round(allowance_fixed * ratio, 2)
        else:
            allowance_for_period = round(allowance_fixed, 2)

    extra_work_total = 0.0
    extra_work_details = []
    for ew in extra_work_records:
        extra_work_total += _num(ew.get("amount"))
        extra_work_details.append({
            "category_id": ew.get("category_id"),
            "category_name": ew.get("category_name"),
            "date": ew.get("date"),
            "units": ew.get("units"),
            "rate": ew.get("rate"),
            "amount": ew.get("amount"),
        })

    bonus_total = 0.0
    other_payments_total = 0.0
    other_deductions_total = 0.0
    components_snapshot = []
    if salary_components:
        for comp in salary_components:
            ctype = (comp.get("type") or "OTHER_PAYMENT").upper()
            cname = (comp.get("name") or "").strip()
            camount = _num(comp.get("amount"))
            if camount <= 0:
                continue
            components_snapshot.append({"type": ctype, "name": cname, "amount": round(camount, 2)})
            if ctype == "BONUS":
                bonus_total += camount
            elif ctype in ("DEDUCTION", "OTHER_DEDUCTION"):
                other_deductions_total += camount
            else:
                other_payments_total += camount

    epf_employee = 0.0
    epf_employer = 0.0
    etf_employer = 0.0
    epf_base = (_arr("epf_base", emp.get("epf_base")) or "ADJUSTED").upper()
    if epf_base == "FULL":
        epf_basis = round(basic_salary, 2)
    elif epf_base == "ATTENDANCE":
        epf_basis = round(adjusted_base * worked_days / max(effective_num_days, 1), 2) if attendance_required else round(base_salary_for_period, 2)
    else:
        epf_basis = round(base_salary_for_period, 2)
    etf_basis = epf_basis
    if epf_rate > 0:
        epf_employee = round(epf_basis * epf_rate / 100, 2)
        epf_employer = round(epf_basis * epf_rate / 100, 2)
    if etf_rate > 0:
        etf_employer = round(etf_basis * etf_rate / 100, 2)

    total_advance_deductions = 0.0
    advance_details = []
    for adv in outstanding_advances:
        amt = _num(adv.get("outstanding", 0))
        total_advance_deductions += amt
        advance_details.append({
            "advance_id": str(adv["_id"]),
            "date": adv.get("date"),
            "original_amount": adv.get("amount"),
            "amount_deducted": amt,
            "reason": adv.get("reason"),
        })

    gross_salary = round(base_salary_for_period + overtime_pay + extra_work_total + allowance_for_period + bonus_total + other_payments_total, 2)
    total_deductions = round(epf_employee + total_advance_deductions + other_deductions_total, 2)
    net_salary = round(gross_salary - total_deductions, 2)

    return {
        "employee_id": employee_id,
        "employee_name": emp_decrypted.get("name"),
        "salary_type": salary_type,
        "pay_frequency": salary_type,
        "attendance_required": attendance_required,
        "calculation_method": calculation_method,
        "period_type": period_type,
        "period_start": start_date,
        "period_end": end_date,
        "calendar_days": effective_num_days,
        "effective_start": effective_start.isoformat(),
        "effective_end": effective_end.isoformat(),
        "total_working_days": total_working_days,
        "worked_days": worked_days,
        "absent_days": absent_days,
        "leave_days": leave_days,
        "holiday_count": holiday_count,
        "weekend_count": weekend_count,
        "overtime_hours": total_ot_hours,
        "overtime_rate": overtime_rate,
        "overtime_pay": overtime_pay,
        "basic_salary": basic_salary,
        "daily_rate": daily_rate,
        "weekly_rate": weekly_rate,
        "contract_amount": contract_amount,
        "adjusted_base_salary": adjusted_base,
        "base_salary_for_period": base_salary_for_period,
        "allowance": allowance_fixed,
        "allowance_type": allowance_type,
        "allowance_for_period": allowance_for_period,
        "bonus": round(bonus_total, 2),
        "other_payments": round(other_payments_total, 2),
        "components": components_snapshot,
        "extra_work_total": extra_work_total,
        "extra_work_details": extra_work_details,
        "epf_rate": epf_rate,
        "etf_rate": etf_rate,
        "epf_base": epf_base,
        "epf_basis": epf_basis,
        "epf_employee": epf_employee,
        "epf_employer": epf_employer,
        "etf_employer": etf_employer,
        "gross_salary": gross_salary,
        "advance_deductions": total_advance_deductions,
        "advance_details": advance_details,
        "other_deductions": round(other_deductions_total, 2),
        "total_deductions": total_deductions,
        "net_salary": net_salary,
        "existing_slip_id": str(existing_slip["_id"]) if existing_slip else None,
        "existing_slip_status": existing_slip.get("status") if existing_slip else None,
        "existing_slip_period": (
            f"{existing_slip.get('period_start')} to {existing_slip.get('period_end')}"
            if existing_slip else None
        ),
    }


# ── Salary Calculation Endpoint (monthly) ─────────────────────────────────
@router.post("/salary/calculate")
@router.get("/salary/calculate")
async def calculate_salary(
    employee_id: str = Query(...),
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(require_capability("salary:read")),
):
    emp_oid = _parse_oid(employee_id, "employee")
    emp = await employees_collection().find_one({"_id": emp_oid})
    if not emp:
        raise NotFoundError("Employee", employee_id)

    emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
    num_days = calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{num_days:02d}"
    return await _calculate_period_salary(emp, emp_decrypted, start_date, end_date, period_type="MONTHLY")


# ── Period / Weekly Salary Calculation ────────────────────────────────────
@router.post("/salary/calculate-period")
@router.get("/salary/calculate-period")
async def calculate_period_salary(
    employee_id: str = Query(...),
    period_start: str = Query(...),
    period_end: str = Query(...),
    period_type: str = Query("MONTHLY"),
    current_user: dict = Depends(require_capability("salary:read")),
):
    emp_oid = _parse_oid(employee_id, "employee")
    emp = await employees_collection().find_one({"_id": emp_oid})
    if not emp:
        raise NotFoundError("Employee", employee_id)

    emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
    return await _calculate_period_salary(emp, emp_decrypted, period_start, period_end, period_type=period_type)


# ── Generate / Save Salary Slip ───────────────────────────────────────────
async def _find_overlapping_slip(employee_id: str, start_str: str, end_str: str) -> Optional[dict]:
    """Return the first non-cancelled/deleted slip whose date range overlaps
    [start_str, end_str] for the same employee. Prevents the same date from
    being paid more than once (e.g. weekly slip + custom range slip)."""
    return await salary_slips_collection().find_one({
        "employee_id": employee_id,
        "status": {"$nin": ["CANCELLED", "DELETED"]},
        "period_start": {"$lte": end_str},
        "period_end": {"$gte": start_str},
    })


async def _validate_advance_details(advance_details: list, period_start_str: str) -> tuple:
    """Validate slip advance deductions against the live advance balances.

    Guarantees each advance is listed once, exists, is not over-deducted, and
    returns (normalized details, total deduction) so the slip ledger matches
    the advance ledger exactly.
    """
    if not advance_details:
        return [], 0.0

    seen = set()
    total = 0.0
    normalized = []
    for d in advance_details:
        adv_id = d.get("advance_id")
        if not adv_id or not ObjectId.is_valid(adv_id):
            continue
        if adv_id in seen:
            raise ConflictError(f"Advance {adv_id} is listed more than once on this slip")
        seen.add(adv_id)

    if not seen:
        return [], 0.0

    adv_docs = await salary_advances_collection().find(
        {"_id": {"$in": [ObjectId(a) for a in seen]}}
    ).to_list(length=None)
    adv_map = {str(a["_id"]): a for a in adv_docs}

    for d in advance_details:
        adv_id = d.get("advance_id")
        if not adv_id or not ObjectId.is_valid(adv_id):
            continue
        adv = adv_map.get(adv_id)
        if not adv:
            raise NotFoundError("Advance", adv_id)
        amt = round(_num(d.get("amount_deducted")), 2)
        outstanding = round(_num(adv.get("outstanding")), 2)
        if amt < 0:
            raise BadRequestError(f"Advance {adv_id} deduction amount cannot be negative")
        if amt == 0:
            continue
        if amt > outstanding + 0.005:
            raise ConflictError(
                f"Deduction {amt:g} for advance {adv_id} exceeds its remaining balance {outstanding:g}"
            )
        total += amt
        normalized.append({
            "advance_id": adv_id,
            "date": d.get("date") or period_start_str,
            "original_amount": adv.get("amount"),
            "amount_deducted": amt,
            "reason": d.get("reason"),
            "requested_amount": d.get("requested_amount"),
            "status": adv.get("status", "OUTSTANDING"),
        })

    return normalized, round(total, 2)


@router.post("/salary/slip")
async def create_salary_slip(
    payload: SalarySlipCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    emp_oid = _parse_oid(payload.employee_id, "employee")
    emp = await employees_collection().find_one({"_id": emp_oid})
    if not emp:
        raise NotFoundError("Employee", payload.employee_id)

    if payload.period_end < payload.period_start:
        raise BadRequestError("Period end cannot be before period start")

    period_start_str = payload.period_start.isoformat() if hasattr(payload.period_start, "isoformat") else str(payload.period_start)
    period_end_str = payload.period_end.isoformat() if hasattr(payload.period_end, "isoformat") else str(payload.period_end)

    existing = await _find_overlapping_slip(payload.employee_id, period_start_str, period_end_str)
    if existing:
        raise ConflictError(
            f"Dates {period_start_str} to {period_end_str} are already covered by a previous salary slip "
            f"(ID: {str(existing['_id'])}, {existing.get('period_start')} to {existing.get('period_end')}, "
            f"status: {existing.get('status')}). A new slip may only cover dates not included in any previous slip."
        )

    advance_details, advance_deductions = await _validate_advance_details(
        payload.advance_details or [], period_start_str,
    )
    if advance_details:
        payload = payload.model_copy(update={"advance_deductions": advance_deductions})

    emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
    emp_name = emp_decrypted.get("name", "Unknown")
    salary_type = emp.get("salary_type", "MONTHLY")

    slip_count_for_emp = await salary_slips_collection().count_documents({
        "employee_id": payload.employee_id,
    })
    emp_code = emp.get("employee_code") or f"EMP{slip_count_for_emp + 1:03d}"
    slip_number = f"{emp_code}-{period_start_str[:7].replace('-', '')}-{slip_count_for_emp + 1:04d}"

    if _num(payload.base_salary_for_period) > 0:
        base_for_period = round(_num(payload.base_salary_for_period), 2)
    else:
        base_for_period = round(
            _num(payload.adjusted_base_salary) * _num(payload.worked_days) / max(_num(payload.calendar_days), 1),
            2,
        )

    total_earnings = round(
        base_for_period
        + _num(payload.overtime_pay)
        + _num(payload.extra_work_total)
        + _num(payload.allowances)
        + _num(payload.bonus)
        + _num(payload.other_payments),
        2,
    )

    total_deductions = round(
        _num(payload.epf_employee)
        + _num(payload.advance_deductions)
        + _num(payload.loan_deduction)
        + _num(payload.other_deductions),
        2,
    )

    net_salary = round(total_earnings - total_deductions, 2)

    doc = {
        "employee_id": payload.employee_id,
        "employee_name": emp_name,
        "salary_type": salary_type,
        "period_type": payload.period_type,
        "period_start": period_start_str,
        "period_end": period_end_str,
        "basic_salary": round(_num(payload.basic_salary), 2),
        "adjusted_base_salary": round(_num(payload.adjusted_base_salary), 2),
        "base_salary_for_period": base_for_period,
        "calendar_days": payload.calendar_days,
        "working_days": payload.working_days,
        "worked_days": round(_num(payload.worked_days), 2),
        "absent_days": round(_num(payload.absent_days), 2),
        "leave_days": round(_num(payload.leave_days), 2),
        "holiday_count": payload.holiday_count,
        "weekend_count": payload.weekend_count,
        "overtime_hours": round(_num(payload.overtime_hours), 2),
        "overtime_rate": round(_num(payload.overtime_rate), 2),
        "overtime_pay": round(_num(payload.overtime_pay), 2),
        "allowances": round(_num(payload.allowances), 2),
        "allowance_details": payload.allowance_details or [],
        "bonus": round(_num(payload.bonus), 2),
        "other_payments": round(_num(payload.other_payments), 2),
        "components": payload.components or [],
        "attendance_required": bool(payload.attendance_required),
        "calculation_method": payload.calculation_method or "MONTHLY_ATTENDANCE",
        "extra_work_total": round(_num(payload.extra_work_total), 2),
        "extra_work_details": payload.extra_work_details or [],
        "epf_employee": round(_num(payload.epf_employee), 2),
        "epf_employer": round(_num(payload.epf_employer), 2),
        "etf_employer": round(_num(payload.etf_employer), 2),
        "epf_base": (payload.epf_base or "ADJUSTED").upper(),
        "total_earnings": total_earnings,
        "advance_deductions": round(_num(payload.advance_deductions), 2),
        "advance_details": advance_details,
        "loan_deduction": round(_num(payload.loan_deduction), 2),
        "other_deductions": round(_num(payload.other_deductions), 2),
        "total_deductions": total_deductions,
        "gross_salary": total_earnings,
        "net_salary": net_salary,
        "amount_paid": round(_num(payload.amount_paid), 2),
        "paid": bool(payload.amount_paid and payload.amount_paid > 0),
        "paid_date": payload.paid_date.isoformat() if payload.paid_date else None,
        "status": payload.status,
        "slip_number": slip_number,
        "notes": (payload.notes or "").strip() or None,
        "created_by": str(current_user.get("user_id", "")),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    result = await salary_slips_collection().insert_one(doc)

    for adv_detail in advance_details:
        adv_id = adv_detail.get("advance_id")
        if adv_id and ObjectId.is_valid(adv_id):
            deducted_amt = _num(adv_detail.get("amount_deducted"))
            await salary_advances_collection().update_one(
                {"_id": ObjectId(adv_id)},
                {
                    "$inc": {"total_deducted": deducted_amt, "outstanding": -deducted_amt},
                    "$push": {
                        "deductions": {
                            "salary_slip_id": str(result.inserted_id),
                            "amount": deducted_amt,
                            "date": period_start_str,
                        }
                    },
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
            )
            adv = await salary_advances_collection().find_one({"_id": ObjectId(adv_id)})
            if adv and _num(adv.get("outstanding")) <= 0:
                await salary_advances_collection().update_one(
                    {"_id": ObjectId(adv_id)},
                    {"$set": {"status": "FULLY_DEDUCTED", "outstanding": 0, "updated_at": datetime.now(timezone.utc)}},
                )

    await log_audit(
        str(current_user.get("user_id", "")),
        "create", "salary_slip", str(result.inserted_id),
        details={"employee_id": payload.employee_id, "period": period_start_str, "net": net_salary, "slip_number": slip_number},
    )

    doc["_id"] = result.inserted_id
    return serialize(doc, SALARY_SENSITIVE)


# ── List salary slips ─────────────────────────────────────────────────────
@router.get("/salary/slips")
async def list_salary_slips(
    employee_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    deleted: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if employee_id:
        query["employee_id"] = employee_id
    if status:
        query["status"] = status
    elif deleted:
        query["status"] = "DELETED"
    else:
        query["status"] = {"$ne": "DELETED"}
    if year:
        query["period_start"] = {"$regex": f"^{year:04d}-"}
    if month and year:
        query["period_start"] = {"$regex": f"^{year:04d}-{month:02d}-"}

    total = await salary_slips_collection().count_documents(query)
    cursor = salary_slips_collection().find(query).sort("created_at", -1).skip(offset).limit(limit)
    items = [serialize(doc, SALARY_SENSITIVE) async for doc in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ── Get single salary slip ────────────────────────────────────────────────
@router.get("/salary/slips/{slip_id}")
async def get_salary_slip(
    slip_id: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = _parse_oid(slip_id, "slip")
    doc = await salary_slips_collection().find_one({"_id": oid})
    if not doc:
        raise NotFoundError("Salary slip", slip_id)
    return serialize(doc, SALARY_SENSITIVE)


# ── Update salary slip ────────────────────────────────────────────────────
@router.put("/salary/slips/{slip_id}")
async def update_salary_slip(
    slip_id: str,
    payload: SalarySlipUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(slip_id, "slip")
    existing = await salary_slips_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary slip", slip_id)
    if existing.get("status") == "CANCELLED":
        raise BadRequestError("Cannot edit a cancelled salary slip")
    if existing.get("status") == "DELETED":
        raise BadRequestError("Cannot edit a deleted salary slip")

    updates = payload.model_dump(exclude_none=True)
    for k in ["basic_salary", "adjusted_base_salary", "base_salary_for_period", "worked_days", "absent_days", "leave_days",
              "overtime_hours", "overtime_rate", "overtime_pay", "allowances", "extra_work_total",
              "bonus", "other_payments",
              "epf_employee", "epf_employer", "etf_employer", "advance_deductions", "loan_deduction",
              "other_deductions", "amount_paid"]:
        if k in updates:
            updates[k] = round(float(updates[k]), 2)

    if "paid_date" in updates and updates["paid_date"] is not None:
        updates["paid_date"] = updates["paid_date"].isoformat() if hasattr(updates["paid_date"], "isoformat") else updates["paid_date"]

    base = {**existing, **updates}
    base_for_period = _num(base.get("base_salary_for_period"))
    if base_for_period <= 0:
        base_for_period = round(
            _num(base.get("adjusted_base_salary")) * _num(base.get("worked_days")) / max(_num(base.get("calendar_days", 30)), 1),
            2,
        )
    total_earnings = round(
        base_for_period
        + _num(base.get("overtime_pay"))
        + _num(base.get("extra_work_total"))
        + _num(base.get("allowances"))
        + _num(base.get("bonus"))
        + _num(base.get("other_payments")),
        2,
    )
    total_deductions = round(
        _num(base.get("epf_employee"))
        + _num(base.get("advance_deductions"))
        + _num(base.get("loan_deduction"))
        + _num(base.get("other_deductions")),
        2,
    )
    net_salary = round(total_earnings - total_deductions, 2)

    updates["total_earnings"] = total_earnings
    updates["gross_salary"] = total_earnings
    updates["total_deductions"] = total_deductions
    updates["net_salary"] = net_salary
    updates["updated_at"] = datetime.now(timezone.utc)

    await salary_slips_collection().update_one({"_id": oid}, {"$set": updates})
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "salary_slip", slip_id, details={"net": net_salary},
    )
    updated = await salary_slips_collection().find_one({"_id": oid})
    return serialize(updated, SALARY_SENSITIVE)


# ── Finalize salary slip ──────────────────────────────────────────────────
@router.post("/salary/slips/{slip_id}/finalize")
async def finalize_salary_slip(
    slip_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(slip_id, "slip")
    existing = await salary_slips_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary slip", slip_id)
    if existing.get("status") == "FINALIZED":
        raise BadRequestError("Salary slip is already finalized")
    if existing.get("status") == "CANCELLED":
        raise BadRequestError("Cannot finalize a cancelled salary slip")
    if existing.get("status") == "DELETED":
        raise BadRequestError("Cannot finalize a deleted salary slip")

    await salary_slips_collection().update_one(
        {"_id": oid},
        {"$set": {"status": "FINALIZED", "updated_at": datetime.now(timezone.utc)}},
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "finalize", "salary_slip", slip_id, details={},
    )
    return {"success": True, "status": "FINALIZED"}


# ── Cancel salary slip ────────────────────────────────────────────────────
@router.post("/salary/slips/{slip_id}/cancel")
async def cancel_salary_slip(
    slip_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(slip_id, "slip")
    existing = await salary_slips_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary slip", slip_id)
    if existing.get("status") == "CANCELLED":
        raise BadRequestError("Salary slip is already cancelled")
    if existing.get("status") == "DELETED":
        raise BadRequestError("Cannot cancel a deleted salary slip")

    for adv_detail in (existing.get("advance_details") or []):
        adv_id = adv_detail.get("advance_id")
        if adv_id and ObjectId.is_valid(adv_id):
            deducted_amt = _num(adv_detail.get("amount_deducted"))
            await salary_advances_collection().update_one(
                {"_id": ObjectId(adv_id)},
                {
                    "$inc": {"total_deducted": -deducted_amt, "outstanding": deducted_amt},
                    "$pull": {"deductions": {"salary_slip_id": slip_id}},
                    "$set": {"status": "OUTSTANDING", "updated_at": datetime.now(timezone.utc)},
                },
            )

    await salary_slips_collection().update_one(
        {"_id": oid},
        {"$set": {"status": "CANCELLED", "updated_at": datetime.now(timezone.utc)}},
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "cancel", "salary_slip", slip_id, details={},
    )
    return {"success": True, "status": "CANCELLED"}


# ── Delete salary slip (soft delete — only cancelled slips) ────────────────
@router.delete("/salary/slips/{slip_id}")
async def delete_salary_slip(
    slip_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(slip_id, "slip")
    existing = await salary_slips_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary slip", slip_id)
    if existing.get("status") == "DELETED":
        raise BadRequestError("Salary slip is already deleted")
    if existing.get("status") != "CANCELLED":
        raise BadRequestError("Only a cancelled salary slip can be deleted")

    await salary_slips_collection().update_one(
        {"_id": oid},
        {"$set": {
            "status": "DELETED",
            "deleted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "delete", "salary_slip", slip_id, details={},
    )
    return {"success": True, "status": "DELETED"}


# ── Mark salary slip as paid ──────────────────────────────────────────────
@router.post("/salary/slips/{slip_id}/pay")
async def mark_salary_paid(
    slip_id: str,
    amount: float = Query(...),
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(slip_id, "slip")
    existing = await salary_slips_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary slip", slip_id)
    if existing.get("status") == "CANCELLED":
        raise BadRequestError("Cannot pay a cancelled salary slip")
    if existing.get("status") == "DELETED":
        raise BadRequestError("Cannot pay a deleted salary slip")
    if amount <= 0:
        raise BadRequestError("Payment amount must be greater than zero")

    await salary_slips_collection().update_one(
        {"_id": oid},
        {"$set": {
            "amount_paid": round(amount, 2),
            "paid": True,
            "paid_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    await log_audit(
        str(current_user.get("user_id", "")),
        "pay", "salary_slip", slip_id, details={"amount": amount},
    )
    return {"success": True}


# ── Salary history summary for an employee ────────────────────────────────
@router.get("/employees/{employee_id}/salary-history")
async def employee_salary_history(
    employee_id: str,
    current_user: dict = Depends(require_capability("salary:read")),
):
    _parse_oid(employee_id, "employee")
    cursor = salary_slips_collection().find(
        {"employee_id": employee_id, "status": {"$ne": "DELETED"}}
    ).sort("period_start", -1)
    return [serialize(doc, SALARY_SENSITIVE) async for doc in cursor]


# ── Attendance bulk set ───────────────────────────────────────────────────
@router.post("/attendance/bulk")
async def bulk_set_attendance(
    employee_id: str = Query(...),
    dates: list = Query(...),
    status: str = Query("PRESENT"),
    overtime_hours: float = Query(0),
    current_user: dict = Depends(require_capability("salary:write")),
):
    _parse_oid(employee_id, "employee")
    emp = await employees_collection().find_one({"_id": ObjectId(employee_id)}, {"attendance_required": 1})
    if emp and emp.get("attendance_required") is False:
        raise BadRequestError("Attendance is not required for this employee (fixed salary arrangement)")
    count = 0
    for d in dates:
        doc = {
            "employee_id": employee_id,
            "date": d,
            "status": status,
            "overtime_hours": overtime_hours,
            "check_in_time": None,
            "check_out_time": None,
            "notes": None,
            "created_at": datetime.now(timezone.utc),
        }
        await attendance_collection().update_one(
            {"employee_id": employee_id, "date": d},
            {"$set": doc},
            upsert=True,
        )
        count += 1
    return {"success": True, "count": count}


# ── Attendance bulk set for a single day (all employees in one table) ──────
@router.post("/attendance/bulk-day")
async def bulk_day_attendance(
    payload: AttendanceBulkDay,
    current_user: dict = Depends(require_capability("salary:write")),
):
    now = datetime.now(timezone.utc)
    date_str = payload.date.isoformat() if hasattr(payload.date, "isoformat") else payload.date
    count = 0
    for rec in payload.records:
        if not rec.employee_id:
            continue
        _parse_oid(rec.employee_id, "employee")
        emp = await employees_collection().find_one({"_id": ObjectId(rec.employee_id)}, {"attendance_required": 1})
        if emp and emp.get("attendance_required") is False:
            raise BadRequestError("Attendance is not required for one or more selected employees (fixed salary arrangement)")
        status = (rec.status or "PRESENT").upper()
        doc = {
            "employee_id": rec.employee_id,
            "date": date_str,
            "status": status,
            "overtime_hours": round(_num(rec.overtime_hours), 2),
            "check_in_time": None,
            "check_out_time": None,
            "notes": None,
            "updated_at": now,
        }
        await attendance_collection().update_one(
            {"employee_id": rec.employee_id, "date": date_str},
            {
                "$set": doc,
                "$setOnInsert": {
                    "employee_id": rec.employee_id,
                    "date": date_str,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        count += 1
    await log_audit(str(current_user.get("user_id", "")), "bulk-set", "attendance", None, details={"date": date_str, "count": count})
    return {"success": True, "count": count}


# ── General attendance listing ─────────────────────────────────────────────
@router.get("/attendance")
async def list_attendance_range(
    employee_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if employee_id:
        query["employee_id"] = employee_id
    if start_date or end_date:
        dateq: dict = {}
        if start_date:
            dateq["$gte"] = start_date
        if end_date:
            dateq["$lte"] = end_date
        query["date"] = dateq
    cursor = attendance_collection().find(query).sort("date", 1)
    return [serialize(doc, ["notes"]) async for doc in cursor]


# ── Attendance summary (mirrors the salary engine) ─────────────────────────
@router.get("/attendance/summary")
async def attendance_summary(
    employee_id: str = Query(...),
    start_date: str = Query(...),
    end_date: str = Query(...),
    current_user: dict = Depends(require_capability("salary:read")),
):
    _parse_oid(employee_id, "employee")
    settings = await _get_company_settings()
    working_days_pattern = settings.get("working_days_pattern", [0, 1, 2, 3, 4, 5])
    holiday_dates = await _get_holiday_dates(start_date, end_date)

    emp = await employees_collection().find_one({"_id": _parse_oid(employee_id, "employee")})
    start_dt = date_cls.fromisoformat(start_date)
    end_dt = date_cls.fromisoformat(end_date)
    if emp:
        if emp.get("joined_date"):
            try:
                jd = date_cls.fromisoformat(emp["joined_date"])
                if jd > start_dt:
                    start_dt = jd
            except (ValueError, AttributeError):
                pass
        if emp.get("leaving_date"):
            try:
                ld = date_cls.fromisoformat(emp["leaving_date"])
                if ld < end_dt:
                    end_dt = ld
            except (ValueError, AttributeError):
                pass

    records = await _get_attendance(employee_id, start_date, end_date)
    by_date = {rec["date"]: rec for rec in records}

    worked_days = 0.0
    half_days = 0.0
    paid_leave_days = 0.0
    unpaid_leave_days = 0.0
    absent_days = 0.0
    total_ot = 0.0
    holiday_count = 0
    weekend_count = 0
    working_day_count = 0
    daily = []

    day = start_dt
    while day <= end_dt:
        date_str = day.isoformat()
        dow = day.weekday()
        rec = by_date.get(date_str)

        if dow not in working_days_pattern:
            weekend_count += 1
            category = "WEEKEND"
        elif date_str in holiday_dates:
            holiday_count += 1
            category = "HOLIDAY"
        else:
            working_day_count += 1
            category = "WORKING"
            status = (rec or {}).get("status", "ABSENT")
            if status == "PRESENT":
                worked_days += 1
            elif status == "HALF_DAY":
                half_days += 0.5
                absent_days += 0.5
            elif status in ("ON_LEAVE", "PAID_LEAVE"):
                paid_leave_days += 1
            elif status == "UNPAID_LEAVE":
                unpaid_leave_days += 1
            else:
                absent_days += 1

        if rec:
            total_ot += _num(rec.get("overtime_hours"))
            daily.append({
                "date": date_str, "dow": dow, "category": category,
                "status": rec.get("status"),
                "overtime_hours": _num(rec.get("overtime_hours")),
                "check_in_time": rec.get("check_in_time"),
                "check_out_time": rec.get("check_out_time"),
            })
        else:
            daily.append({
                "date": date_str, "dow": dow, "category": category,
                "status": None, "overtime_hours": 0, "check_in_time": None, "check_out_time": None,
            })
        day = date_cls.fromordinal(day.toordinal() + 1)

    return {
        "employee_id": employee_id,
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
        "calendar_days": (end_dt - start_dt).days + 1,
        "working_day_count": working_day_count,
        "weekend_count": weekend_count,
        "holiday_count": holiday_count,
        "worked_days": worked_days,
        "half_days": half_days,
        "paid_leave_days": paid_leave_days,
        "unpaid_leave_days": unpaid_leave_days,
        "absent_days": absent_days,
        "overtime_hours": total_ot,
        "daily": daily,
    }


# ── Payroll preview (all active employees for a month) ─────────────────────
@router.get("/salary/payroll-preview")
async def payroll_preview(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(require_capability("salary:read")),
):
    num_days = calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{num_days:02d}"

    emps = [emp async for emp in employees_collection().find({"is_active": True})]

    results = []
    if emps:
        preloaded = await _prefetch_payroll_data(
            [str(e["_id"]) for e in emps], start_date, end_date,
        )
        sem = asyncio.Semaphore(8)

        async def _calc(emp: dict) -> dict:
            async with sem:
                emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
                calc = await _calculate_period_salary(
                    emp, emp_decrypted, start_date, end_date, "MONTHLY", preloaded=preloaded,
                )
                if not calc.get("attendance_required"):
                    calc["projected_worked_days"] = calc.get("worked_days")
                    calc["projected_gross_salary"] = _num(calc.get("gross_salary"))
                    calc["projected_net_salary"] = _num(calc.get("net_salary"))
                else:
                    projected = await _calculate_period_salary(
                        emp, emp_decrypted, start_date, end_date, "MONTHLY",
                        preloaded=preloaded, full_attendance=True,
                    )
                    calc["projected_worked_days"] = int(projected.get("worked_days") or 0)
                    calc["projected_gross_salary"] = round(_num(projected.get("gross_salary")), 2)
                    calc["projected_net_salary"] = round(_num(projected.get("net_salary")), 2)
                return calc

        outs = await asyncio.gather(*(_calc(e) for e in emps), return_exceptions=True)
        for out in outs:
            if isinstance(out, Exception):
                continue
            results.append(out)

    results.sort(key=lambda r: str(r.get("employee_name") or ""))
    total_net = round(sum(_num(r.get("net_salary")) for r in results), 2)
    projected_total_net = round(sum(_num(r.get("projected_net_salary")) for r in results), 2)
    return {
        "year": year,
        "month": month,
        "period_start": start_date,
        "period_end": end_date,
        "count": len(results),
        "total_gross": round(sum(_num(r.get("gross_salary")) for r in results), 2),
        "total_deductions": round(sum(_num(r.get("total_deductions")) for r in results), 2),
        "total_net": total_net,
        "projected_total_gross": round(sum(_num(r.get("projected_gross_salary")) for r in results), 2),
        "projected_total_deductions": round(sum(_num(r.get("total_deductions")) for r in results), 2),
        "projected_total_net": projected_total_net,
        "projected_total_net_variance": round(projected_total_net - total_net, 2),
        "employees": results,
    }


# ── Run payroll (create DRAFT slips for all employees missing one) ─────────
@router.post("/salary/payroll-run")
async def run_payroll(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(require_capability("salary:write")),
):
    num_days = calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{num_days:02d}"

    emps = [emp async for emp in employees_collection().find({"is_active": True})]

    skipped = []
    to_create = []
    preloaded = None
    if emps:
        preloaded = await _prefetch_payroll_data(
            [str(e["_id"]) for e in emps], start_date, end_date,
        )
        for emp in emps:
            employee_id = str(emp["_id"])
            existing = preloaded["slips"].get(employee_id)
            if existing:
                skipped.append({"employee_id": employee_id, "slip_id": str(existing["_id"])})
                continue
            to_create.append(emp)

    created = []
    failed = []

    if to_create:
        sem = asyncio.Semaphore(6)

        async def _create(emp: dict) -> tuple:
            employee_id = str(emp["_id"])
            try:
                async with sem:
                    emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
                    calc = await _calculate_period_salary(
                        emp, emp_decrypted, start_date, end_date, "MONTHLY", preloaded=preloaded,
                    )
                    payload = SalarySlipCreate(
                        employee_id=employee_id,
                        period_type="MONTHLY",
                        period_start=date_cls.fromisoformat(start_date),
                        period_end=date_cls.fromisoformat(end_date),
                        basic_salary=_num(calc.get("basic_salary")),
                        adjusted_base_salary=_num(calc.get("adjusted_base_salary")),
                        base_salary_for_period=_num(calc.get("base_salary_for_period")),
                        calendar_days=int(calc.get("calendar_days") or 30),
                        working_days=int(calc.get("total_working_days") or 0),
                        worked_days=_num(calc.get("worked_days")),
                        absent_days=_num(calc.get("absent_days")),
                        leave_days=_num(calc.get("leave_days")),
                        holiday_count=int(calc.get("holiday_count") or 0),
                        weekend_count=int(calc.get("weekend_count") or 0),
                        overtime_hours=_num(calc.get("overtime_hours")),
                        overtime_rate=_num(calc.get("overtime_rate")),
                        overtime_pay=_num(calc.get("overtime_pay")),
                        extra_work_total=_num(calc.get("extra_work_total")),
                        extra_work_details=calc.get("extra_work_details") or [],
                        allowances=_num(calc.get("allowance_for_period")),
                        bonus=_num(calc.get("bonus")),
                        other_payments=_num(calc.get("other_payments")),
                        other_deductions=_num(calc.get("other_deductions")),
                        components=calc.get("components") or [],
                        attendance_required=bool(calc.get("attendance_required", True)),
                        calculation_method=calc.get("calculation_method") or "MONTHLY_ATTENDANCE",
                        epf_employee=_num(calc.get("epf_employee")),
                        epf_employer=_num(calc.get("epf_employer")),
                        etf_employer=_num(calc.get("etf_employer")),
                        epf_base=calc.get("epf_base") or "ADJUSTED",
                        advance_deductions=_num(calc.get("advance_deductions")),
                        advance_details=calc.get("advance_details") or [],
                        status="DRAFT",
                    )
                    slip = await create_salary_slip(payload, current_user)
                    return ("ok", slip)
            except Exception as exc:
                return ("err", {"employee_id": employee_id, "error": str(exc)})

        outs = await asyncio.gather(*(_create(e) for e in to_create), return_exceptions=True)
        for out in outs:
            if isinstance(out, Exception):
                failed.append({"employee_id": "", "error": str(out)})
                continue
            kind, value = out
            if kind == "ok":
                created.append(value)
            else:
                failed.append(value)

    await log_audit(
        str(current_user.get("user_id", "")),
        "run_payroll", "salary_slip", None,
        details={"year": year, "month": month, "created": len(created), "skipped": len(skipped), "failed": len(failed)},
    )
    return {"created": created, "count": len(created), "skipped": skipped, "failed": failed}


# ── Salary package overrides (per-month arrangement) ─────────────────────
def _validate_month(value: str) -> str:
    parts = str(value or "").split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise BadRequestError("Month must be in YYYY-MM format")
    y, m = parts
    if not (y.isdigit() and m.isdigit() and (1 <= int(m) <= 12)):
        raise BadRequestError("Month must be a valid YYYY-MM value")
    return f"{int(y):04d}-{int(m):02d}"


async def _package_locked(employee_id: str, month: str) -> bool:
    """A finalized/paid slip makes the month's arrangement immutable."""
    year, mon = int(month[:4]), int(month[5:7])
    num_days = calendar.monthrange(year, mon)[1]
    existing = await salary_slips_collection().find_one({
        "employee_id": employee_id,
        "period_start": f"{month}-01",
        "period_end": f"{month}-{num_days:02d}",
        "status": {"$in": ["FINALIZED", "PAID"]},
    })
    return existing is not None


@router.get("/salary/packages")
async def list_salary_packages(
    employee_id: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if employee_id:
        _parse_oid(employee_id, "employee")
        query["employee_id"] = employee_id
    if month:
        query["month"] = _validate_month(month)
    cursor = salary_packages_collection().find(query).sort("month", -1)
    return [serialize(doc, ["notes"]) async for doc in cursor]


@router.post("/salary/packages")
async def upsert_salary_package(
    payload: SalaryPackageUpsert,
    current_user: dict = Depends(require_capability("salary:write")),
):
    _parse_oid(payload.employee_id, "employee")
    month = _validate_month(payload.month)
    if await _package_locked(payload.employee_id, month):
        raise ConflictError("Salary for this month is already finalized for this employee")

    doc = {
        "employee_id": payload.employee_id,
        "month": month,
        "salary_type": (payload.salary_type or "MONTHLY").upper(),
        "attendance_required": payload.attendance_required,
        "basic_salary": round(_num(payload.basic_salary), 2),
        "daily_rate": round(_num(payload.daily_rate), 2),
        "weekly_rate": round(_num(payload.weekly_rate), 2),
        "contract_amount": round(_num(payload.contract_amount), 2),
        "overtime_rate": round(_num(payload.overtime_rate), 2),
        "allowance": round(_num(payload.allowance), 2),
        "allowance_type": (payload.allowance_type or "FIXED").upper(),
        "epf_rate": round(_num(payload.epf_rate), 2),
        "etf_rate": round(_num(payload.etf_rate), 2),
        "epf_base": (payload.epf_base or "ADJUSTED").upper(),
        "salary_components": [c.model_dump() for c in payload.salary_components] if payload.salary_components else [],
        "notes": (payload.notes or "").strip() or None,
        "created_by": str(current_user.get("user_id", "")),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    filterq = {"employee_id": payload.employee_id, "month": month}
    existing = await salary_packages_collection().find_one(filterq)
    if existing:
        doc["created_at"] = existing.get("created_at", doc["created_at"])
        await salary_packages_collection().update_one(filterq, {"$set": doc})
        result_id = existing["_id"]
    else:
        result = await salary_packages_collection().insert_one(doc)
        result_id = result.inserted_id

    await log_audit(
        str(current_user.get("user_id", "")),
        "upsert", "salary_package", str(result_id),
        details={"employee_id": payload.employee_id, "month": month},
    )
    saved = await salary_packages_collection().find_one({"_id": result_id})
    return serialize(saved, ["notes"])


@router.put("/salary/packages/{package_id}")
async def update_salary_package(
    package_id: str,
    payload: SalaryPackageUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(package_id, "package")
    existing = await salary_packages_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary package", package_id)
    if await _package_locked(str(existing.get("employee_id", "")), str(existing.get("month", ""))):
        raise ConflictError("Salary for this month is already finalized for this employee")

    updates = payload.model_dump(exclude_none=True)
    numeric = ["basic_salary", "daily_rate", "weekly_rate", "contract_amount", "overtime_rate", "allowance", "epf_rate", "etf_rate"]
    for k in numeric:
        if k in updates:
            updates[k] = round(float(updates[k]), 2)
    for k in ("salary_type", "allowance_type", "epf_base"):
        if updates.get(k):
            updates[k] = updates[k].upper()
    if updates.get("salary_components") is not None:
        updates["salary_components"] = [c.model_dump() for c in updates["salary_components"]]
    updates["updated_at"] = datetime.now(timezone.utc)

    await salary_packages_collection().update_one({"_id": oid}, {"$set": updates})
    await log_audit(str(current_user.get("user_id", "")), "update", "salary_package", package_id, details={})
    updated = await salary_packages_collection().find_one({"_id": oid})
    return serialize(updated, ["notes"])


@router.delete("/salary/packages/{package_id}")
async def delete_salary_package(
    package_id: str,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = _parse_oid(package_id, "package")
    existing = await salary_packages_collection().find_one({"_id": oid})
    if not existing:
        raise NotFoundError("Salary package", package_id)
    if await _package_locked(str(existing.get("employee_id", "")), str(existing.get("month", ""))):
        raise ConflictError("Salary for this month is already finalized for this employee")
    await salary_packages_collection().delete_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "delete", "salary_package", package_id, details={})
    return {"success": True}


# ── Legacy salary compatibility ───────────────────────────────────────────
@router.get("/employees/{employee_id}/salaries")
async def list_salaries(
    employee_id: str,
    year: Optional[int] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = _parse_oid(employee_id, "employee")
    query: dict = {"employee_id": employee_id}
    if year:
        query["month"] = {"$regex": f"^{year:04d}-"}
    cursor = salaries_collection().find(query).sort("month", -1)
    return [serialize(doc, SALARY_SENSITIVE) async for doc in cursor]
