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
    attendance_collection,
    holidays_collection,
    extra_work_records_collection,
    extra_work_categories_collection,
    company_settings_collection,
    salaries_collection,
)
from ..crypto_helper import decrypt_dict
from ..models import SalarySlipCreate, SalarySlipUpdate
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


# ── Generic Period Salary Calculation ─────────────────────────────────────
async def _calculate_period_salary(
    emp: dict,
    emp_decrypted: dict,
    start_date: str,
    end_date: str,
    period_type: str = "MONTHLY",
) -> dict:
    """Calculate salary for an arbitrary date period (monthly / weekly / custom)."""
    employee_id = str(emp["_id"])
    salary_type = emp.get("salary_type", "MONTHLY")
    basic_salary = _num(emp.get("basic_salary"))
    daily_rate = _num(emp.get("daily_rate"))
    epf_rate = _num(emp.get("epf_rate"))
    etf_rate = _num(emp.get("etf_rate"))

    settings = await _get_company_settings()
    working_days_pattern = settings.get("working_days_pattern", [0, 1, 2, 3, 4, 5])
    salary_basis_days = settings.get("salary_basis_days", 30)
    basis_days = max(_num(emp.get("salary_basis_days")), salary_basis_days, 1)

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

    holiday_dates = await _get_holiday_dates(start_date, end_date)
    attendance_records = await _get_attendance(employee_id, start_date, end_date)

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
        if att:
            status = (att.get("status") or "ABSENT").upper()
            if status in ("PRESENT",):
                worked_days += 1
            elif status in ("HALF_DAY",):
                worked_days += 0.5
                absent_days += 0.5
            elif status in ("ON_LEAVE", "PAID_LEAVE"):
                leave_days += 1
            elif status in ("UNPAID_LEAVE", "ABSENT"):
                absent_days += 1
            else:
                absent_days += 1
            total_ot_hours += _num(att.get("overtime_hours"))
        else:
            absent_days += 1
        day = date_cls.fromordinal(day.toordinal() + 1)

    effective_working_days = worked_days + leave_days

    effective_num_days = (effective_end - effective_start).days + 1

    adjusted_base = round(basic_salary * effective_num_days / basis_days, 2) if salary_type == "MONTHLY" else 0
    base_salary_for_period = round(adjusted_base * effective_working_days / effective_num_days, 2) if salary_type == "MONTHLY" else 0

    if salary_type == "DAILY":
        base_salary_for_period = round(daily_rate * worked_days, 2)
        adjusted_base = basic_salary or daily_rate * settings.get("working_days_per_week", 6)
    elif salary_type == "WEEKLY":
        base_salary_for_period = round(daily_rate * worked_days, 2)
        adjusted_base = base_salary_for_period if worked_days > 0 else round(daily_rate * settings.get("working_days_per_week", 6), 2)

    overtime_pay = 0.0
    overtime_rate = _num(emp.get("overtime_rate")) or settings.get("default_overtime_rate", 0)
    if overtime_rate > 0 and total_ot_hours > 0:
        overtime_pay = round(total_ot_hours * overtime_rate, 2)

    allowance_fixed = _num(emp.get("allowance"))
    allowance_type = (emp.get("allowance_type") or "FIXED").upper()
    if allowance_fixed > 0:
        if allowance_type == "DAYS":
            if salary_type == "MONTHLY" and basic_salary > 0:
                ratio = round(base_salary_for_period / basic_salary, 4)
            else:
                ratio = round(effective_working_days / max(effective_num_days, 1), 4)
            allowance_for_period = round(allowance_fixed * ratio, 2)
        else:
            allowance_for_period = round(allowance_fixed, 2)
    else:
        allowance_for_period = 0.0

    extra_work_records = await _get_extra_work(employee_id, start_date, end_date)
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

    epf_employee = 0.0
    epf_employer = 0.0
    etf_employer = 0.0
    epf_base = (emp.get("epf_base") or "ADJUSTED").upper()
    if epf_base == "FULL":
        epf_basis = round(basic_salary, 2)
    elif epf_base == "ATTENDANCE":
        epf_basis = round(adjusted_base * worked_days / max(effective_num_days, 1), 2)
    else:
        epf_basis = round(base_salary_for_period, 2)
    etf_basis = epf_basis
    if epf_rate > 0:
        epf_employee = round(epf_basis * epf_rate / 100, 2)
        epf_employer = round(epf_basis * epf_rate / 100, 2)
    if etf_rate > 0:
        etf_employer = round(etf_basis * etf_rate / 100, 2)

    outstanding_advances = await _get_outstanding_advances(employee_id)
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

    gross_salary = round(base_salary_for_period + overtime_pay + extra_work_total + allowance_for_period, 2)
    total_deductions = round(epf_employee + total_advance_deductions, 2)
    net_salary = round(gross_salary - total_deductions, 2)

    existing_slip = await salary_slips_collection().find_one({
        "employee_id": employee_id,
        "period_start": start_date,
        "period_end": end_date,
        "status": {"$ne": "CANCELLED"},
    })

    return {
        "employee_id": employee_id,
        "employee_name": emp_decrypted.get("name"),
        "salary_type": salary_type,
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
        "adjusted_base_salary": adjusted_base,
        "base_salary_for_period": base_salary_for_period,
        "allowance": allowance_fixed,
        "allowance_type": allowance_type,
        "allowance_for_period": allowance_for_period,
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
        "total_deductions": total_deductions,
        "net_salary": net_salary,
        "existing_slip_id": str(existing_slip["_id"]) if existing_slip else None,
        "existing_slip_status": existing_slip.get("status") if existing_slip else None,
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
@router.post("/salary/slip")
async def create_salary_slip(
    payload: SalarySlipCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    emp_oid = _parse_oid(payload.employee_id, "employee")
    emp = await employees_collection().find_one({"_id": emp_oid})
    if not emp:
        raise NotFoundError("Employee", payload.employee_id)

    existing = await salary_slips_collection().find_one({
        "employee_id": payload.employee_id,
        "period_start": payload.period_start.isoformat() if hasattr(payload.period_start, "isoformat") else str(payload.period_start),
        "period_end": payload.period_end.isoformat() if hasattr(payload.period_end, "isoformat") else str(payload.period_end),
        "status": {"$ne": "CANCELLED"},
    })
    if existing:
        raise ConflictError(f"A salary slip already exists for this period (ID: {str(existing['_id'])})")

    emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
    emp_name = emp_decrypted.get("name", "Unknown")
    salary_type = emp.get("salary_type", "MONTHLY")

    period_start_str = payload.period_start.isoformat() if hasattr(payload.period_start, "isoformat") else str(payload.period_start)
    period_end_str = payload.period_end.isoformat() if hasattr(payload.period_end, "isoformat") else str(payload.period_end)

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
        + _num(payload.allowances),
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
        "extra_work_total": round(_num(payload.extra_work_total), 2),
        "extra_work_details": payload.extra_work_details or [],
        "epf_employee": round(_num(payload.epf_employee), 2),
        "epf_employer": round(_num(payload.epf_employer), 2),
        "etf_employer": round(_num(payload.etf_employer), 2),
        "epf_base": (payload.epf_base or "ADJUSTED").upper(),
        "total_earnings": total_earnings,
        "advance_deductions": round(_num(payload.advance_deductions), 2),
        "advance_details": payload.advance_details or [],
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

    for adv_detail in (payload.advance_details or []):
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
    current_user: dict = Depends(require_capability("salary:read")),
):
    query: dict = {}
    if employee_id:
        query["employee_id"] = employee_id
    if status:
        query["status"] = status
    if year:
        query["period_start"] = {"$regex": f"^{year:04d}-"}
    if month and year:
        query["period_start"] = {"$regex": f"^{year:04d}-{month:02d}-"}

    cursor = salary_slips_collection().find(query).sort("created_at", -1)
    return [serialize(doc, SALARY_SENSITIVE) async for doc in cursor]


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

    updates = payload.model_dump(exclude_none=True)
    for k in ["basic_salary", "adjusted_base_salary", "base_salary_for_period", "worked_days", "absent_days", "leave_days",
              "overtime_hours", "overtime_rate", "overtime_pay", "allowances", "extra_work_total",
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
        + _num(base.get("allowances")),
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
        {"employee_id": employee_id}
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

    results = []
    cursor = employees_collection().find({"is_active": True})
    async for emp in cursor:
        try:
            emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
            calc = await _calculate_period_salary(emp, emp_decrypted, start_date, end_date, "MONTHLY")
        except Exception:
            continue
        results.append(calc)

    results.sort(key=lambda r: str(r.get("employee_name") or ""))
    return {
        "year": year,
        "month": month,
        "period_start": start_date,
        "period_end": end_date,
        "count": len(results),
        "total_gross": round(sum(_num(r.get("gross_salary")) for r in results), 2),
        "total_deductions": round(sum(_num(r.get("total_deductions")) for r in results), 2),
        "total_net": round(sum(_num(r.get("net_salary")) for r in results), 2),
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

    created = []
    skipped = []
    failed = []
    cursor = employees_collection().find({"is_active": True})
    async for emp in cursor:
        employee_id = str(emp["_id"])
        existing = await salary_slips_collection().find_one({
            "employee_id": employee_id,
            "period_start": start_date,
            "period_end": end_date,
            "status": {"$ne": "CANCELLED"},
        })
        if existing:
            skipped.append({"employee_id": employee_id, "slip_id": str(existing["_id"])})
            continue
        try:
            emp_decrypted = decrypt_dict(emp, EMPLOYEE_SENSITIVE)
            calc = await _calculate_period_salary(emp, emp_decrypted, start_date, end_date, "MONTHLY")
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
                epf_employee=_num(calc.get("epf_employee")),
                epf_employer=_num(calc.get("epf_employer")),
                etf_employer=_num(calc.get("etf_employer")),
                epf_base=calc.get("epf_base") or "ADJUSTED",
                advance_deductions=_num(calc.get("advance_deductions")),
                advance_details=calc.get("advance_details") or [],
                status="DRAFT",
            )
            slip = await create_salary_slip(payload, current_user)
            created.append(slip)
        except Exception as exc:
            failed.append({"employee_id": employee_id, "error": str(exc)})

    await log_audit(
        str(current_user.get("user_id", "")),
        "run_payroll", "salary_slip", None,
        details={"year": year, "month": month, "created": len(created), "skipped": len(skipped), "failed": len(failed)},
    )
    return {"created": created, "count": len(created), "skipped": skipped, "failed": failed}


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
