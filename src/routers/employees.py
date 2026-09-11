from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth_helper import require_capability
from ..database.main_db import employees_collection, salaries_collection, attendance_collection
from ..models import EmployeeCreate, EmployeeUpdate, SalaryCreate, SalaryUpdate, AttendanceCreate
from ..crypto_helper import encrypt_dict, decrypt_dict, get_search_token
from ..router_utils import serialize, log_audit
from ..error_responses import BadRequestError, ConflictError

router = APIRouter(tags=["Employees & Salaries"])

SENSITIVE_FIELDS = ["name", "phone", "nic", "notes"]
SALARY_SENSITIVE = ["notes"]


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- Employees ----------------
@router.get("/employees")
async def list_employees(
    search: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("employee:read")),
):
    query: dict = {}
    if search:
        query["name_search"] = get_search_token(search)
    cursor = employees_collection().find(query).sort("created_at", -1)
    return [serialize(doc, SENSITIVE_FIELDS) async for doc in cursor]


@router.get("/employees/{employee_id}")
async def get_employee(
    employee_id: str,
    current_user: dict = Depends(require_capability("employee:read")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    doc = await employees_collection().find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Employee not found")
    return serialize(doc, SENSITIVE_FIELDS)


@router.post("/employees")
async def create_employee(
    payload: EmployeeCreate,
    current_user: dict = Depends(require_capability("employee:write")),
):
    if not payload.name.strip():
        raise BadRequestError("Employee name is required")
    doc = {
        "name": payload.name.strip(),
        "position": (payload.position or "").strip() or None,
        "department": payload.department,
        "phone": (payload.phone or "").strip() or None,
        "nic": (payload.nic or "").strip() or None,
        "salary_type": payload.salary_type,
        "basic_salary": round(payload.basic_salary, 2),
        "daily_rate": round(payload.daily_rate, 2),
        "epf_rate": round(payload.epf_rate, 2),
        "etf_rate": round(payload.etf_rate, 2),
        "joined_date": payload.joined_date.isoformat() if payload.joined_date else None,
        "status": payload.status,
        "is_active": True,
        "notes": (payload.notes or "").strip() or None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    encrypted = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await employees_collection().insert_one(encrypted)
    await log_audit(str(current_user.get("user_id", "")), "create", "employee", str(result.inserted_id), details={"name": payload.name})
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, SENSITIVE_FIELDS)


@router.put("/employees/{employee_id}")
async def update_employee(
    employee_id: str,
    payload: EmployeeUpdate,
    current_user: dict = Depends(require_capability("employee:write")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    existing = await employees_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Employee not found")
    data = payload.model_dump(exclude_none=True)
    updates = {}
    for key, val in data.items():
        if key == "joined_date" and val is not None:
            updates["joined_date"] = val.isoformat() if hasattr(val, "isoformat") else val
        elif key in ("basic_salary", "daily_rate", "epf_rate", "etf_rate") and val is not None:
            updates[key] = round(float(val), 2)
        elif key == "salary_type":
            updates[key] = val
        else:
            updates[key] = val
    updates["updated_at"] = datetime.now(timezone.utc)
    if len(updates) > 1:
        await employees_collection().update_one({"_id": oid}, {"$set": updates})
    updated = await employees_collection().find_one({"_id": oid})
    await log_audit(str(current_user.get("user_id", "")), "update", "employee", employee_id, details={})
    return serialize(updated, SENSITIVE_FIELDS)


@router.delete("/employees/{employee_id}")
async def delete_employee(
    employee_id: str,
    current_user: dict = Depends(require_capability("employee:write")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    existing = await employees_collection().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Employee not found")
    await employees_collection().update_one({"_id": oid}, {"$set": {"is_active": False, "status": "INACTIVE", "updated_at": datetime.now(timezone.utc)}})
    await log_audit(str(current_user.get("user_id", "")), "delete", "employee", employee_id, details={})
    return {"success": True}


# ---------------- Salaries ----------------
def _payroll_month(month: str, year: Optional[int]) -> str:
    m = int(month)
    if m < 1 or m > 12:
        m = 1
    y = year or datetime.now(timezone.utc).year
    return f"{y:04d}-{m:02d}"


@router.get("/employees/{employee_id}/salaries")
async def list_salaries(
    employee_id: str,
    year: Optional[int] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    query: dict = {"employee_id": employee_id}
    if year:
        query["month"] = {"$regex": f"^{year:04d}-"}
    cursor = salaries_collection().find(query).sort("month", -1)
    return [serialize(doc, SALARY_SENSITIVE) async for doc in cursor]


@router.post("/employees/{employee_id}/salaries")
async def create_salary(
    employee_id: str,
    payload: SalaryCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    emp = await employees_collection().find_one({"_id": oid})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_str = _payroll_month(payload.month, payload.year)
    existing = await salaries_collection().find_one({"employee_id": employee_id, "month": month_str})
    if existing:
        raise ConflictError(f"Salary already recorded for {month_str}")

    overtime_pay = round(_num(payload.overtime_hours) * _num(payload.overtime_rate), 2)
    gross = round(_num(payload.basic_salary) + _num(payload.allowances) + overtime_pay, 2)
    total_deductions = round(
        _num(payload.epf_deduction) + _num(payload.etf_deduction) + _num(payload.loan_deduction) + _num(payload.advance_deduction),
        2,
    )
    net = round(gross - total_deductions, 2)
    amount_paid = payload.amount_paid if payload.amount_paid else net

    doc = {
        "employee_id": employee_id,
        "employee_name": decrypt_dict(emp, SENSITIVE_FIELDS).get("name"),
        "month": month_str,
        "basic_salary": round(_num(payload.basic_salary), 2),
        "overtime_hours": round(_num(payload.overtime_hours), 2),
        "overtime_rate": round(_num(payload.overtime_rate), 2),
        "overtime_pay": overtime_pay,
        "allowances": round(_num(payload.allowances), 2),
        "gross_salary": gross,
        "epf_deduction": round(_num(payload.epf_deduction), 2),
        "etf_deduction": round(_num(payload.etf_deduction), 2),
        "loan_deduction": round(_num(payload.loan_deduction), 2),
        "advance_deduction": round(_num(payload.advance_deduction), 2),
        "total_deductions": total_deductions,
        "net_salary": net,
        "amount_paid": round(_num(amount_paid), 2),
        "paid": bool(payload.amount_paid and payload.amount_paid > 0),
        "paid_date": payload.paid_date.isoformat() if payload.paid_date else None,
        "notes": (payload.notes or "").strip() or None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    encrypted = encrypt_dict(doc, SALARY_SENSITIVE)
    result = await salaries_collection().insert_one(encrypted)
    await log_audit(str(current_user.get("user_id", "")), "create", "salary", str(result.inserted_id), details={"employee": employee_id, "month": month_str, "net": net})
    encrypted["_id"] = result.inserted_id
    return serialize(encrypted, SALARY_SENSITIVE)


@router.put("/employees/{employee_id}/salaries/{salary_id}")
async def update_salary(
    employee_id: str,
    salary_id: str,
    payload: SalaryUpdate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    soid = ObjectId(salary_id) if ObjectId.is_valid(salary_id) else None
    if not soid:
        raise HTTPException(status_code=404, detail="Salary not found")
    existing = await salaries_collection().find_one({"_id": soid})
    if not existing:
        raise HTTPException(status_code=404, detail="Salary not found")

    updates = payload.model_dump(exclude_none=True)
    updates = {k: v for k, v in updates.items() if not isinstance(v, float) or v is not None}
    if "paid_date" in updates and updates["paid_date"] is not None:
        updates["paid_date"] = updates["paid_date"].isoformat() if hasattr(updates["paid_date"], "isoformat") else updates["paid_date"]
    # Recompute net if earnings touched
    if any(k in updates for k in ["basic_salary", "allowances", "overtime_hours", "overtime_rate", "epf_deduction", "etf_deduction", "loan_deduction", "advance_deduction", "amount_paid"]):
        base = existing
        overtime_pay = round(_num(updates.get("overtime_hours", base.get("overtime_hours"))) * _num(updates.get("overtime_rate", base.get("overtime_rate"))), 2)
        gross = round(_num(updates.get("basic_salary", base.get("basic_salary"))) + _num(updates.get("allowances", base.get("allowances"))) + overtime_pay, 2)
        total_deductions = round(
            _num(updates.get("epf_deduction", base.get("epf_deduction")))
            + _num(updates.get("etf_deduction", base.get("etf_deduction")))
            + _num(updates.get("loan_deduction", base.get("loan_deduction")))
            + _num(updates.get("advance_deduction", base.get("advance_deduction"))),
            2,
        )
        net = round(gross - total_deductions, 2)
        updates["overtime_pay"] = overtime_pay
        updates["gross_salary"] = gross
        updates["total_deductions"] = total_deductions
        if "net_salary" not in updates:
            updates["net_salary"] = net
        updates["num_placeholder"] = None
        updates.pop("num_placeholder")
    updates["updated_at"] = datetime.now(timezone.utc)

    if len(updates) > 1:
        await salaries_collection().update_one({"_id": soid}, {"$set": updates})
    updated = await salaries_collection().find_one({"_id": soid})
    await log_audit(str(current_user.get("user_id", "")), "update", "salary", salary_id, details={})
    return serialize(updated, SALARY_SENSITIVE)


# ---------------- Attendance ----------------
@router.get("/employees/{employee_id}/attendance")
async def list_attendance(
    employee_id: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(require_capability("salary:read")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    query: dict = {"employee_id": employee_id}
    if start_date:
        query["date"] = {"$gte": start_date}
    if end_date:
        query["date"] = {**query.get("date", {}), "$lte": end_date}
    cursor = attendance_collection().find(query).sort("date", -1)
    return [serialize(doc, ["notes"]) async for doc in cursor]


@router.post("/employees/{employee_id}/attendance")
async def create_attendance(
    employee_id: str,
    payload: AttendanceCreate,
    current_user: dict = Depends(require_capability("salary:write")),
):
    oid = ObjectId(employee_id) if ObjectId.is_valid(employee_id) else None
    if not oid:
        raise HTTPException(status_code=404, detail="Employee not found")
    doc = {
        "employee_id": employee_id,
        "date": payload.date.isoformat(),
        "status": payload.status,
        "overtime_hours": round(payload.overtime_hours, 2),
        "notes": (payload.notes or "").strip() or None,
        "created_at": datetime.now(timezone.utc),
    }
    result = await attendance_collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    return serialize(doc, ["notes"])