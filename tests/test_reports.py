import os
import sys
from datetime import date
from pathlib import Path
from bson import ObjectId
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MASTER_KEY", "test-master-key-1234567890")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unittest.mock import AsyncMock

import pytest

from src.routers import reports


class FakeSavedReports:
    def __init__(self):
        self.document = None

    async def insert_one(self, document):
        document["_id"] = ObjectId()
        self.document = dict(document)
        return type("InsertResult", (), {"inserted_id": document["_id"]})()

    async def find_one(self, query):
        if self.document and self.document["_id"] == query.get("_id"):
            return dict(self.document)
        return None

    async def update_one(self, query, update):
        if self.document and self.document["_id"] == query.get("_id"):
            self.document.update(update["$set"])

    async def delete_one(self, query):
        deleted = int(bool(self.document and self.document["_id"] == query.get("_id")))
        if deleted:
            self.document = None
        return type("DeleteResult", (), {"deleted_count": deleted})()


@pytest.mark.asyncio
async def test_daily_report_accepts_date_alias(monkeypatch):
    monkeypatch.setattr(
        reports,
        "_dec_transactions",
        AsyncMock(return_value=[
            {"total_amount": 100.0, "total_cost": 30.0, "total_quantity": 2.0},
            {"total_amount": 50.0, "total_cost": 10.0, "total_quantity": 1.0},
        ]),
    )
    monkeypatch.setattr(reports, "_dec_expenses", AsyncMock(return_value=[{"amount": 10.0}]))
    monkeypatch.setattr(
        reports,
        "_salary_expense_summary",
        AsyncMock(return_value={"salary_expense_total": 40.0, "employees": [], "period_details": []}),
    )

    result = await reports.daily_report(date="2026-10-10", current_user={"role": "ADMIN"})

    assert result["date"] == "2026-10-10"
    assert result["total_revenue"] == 150.0
    assert result["salary_expense_total"] == 40.0
    assert result["total_expenses"] == 50.0
    assert result["gross_profit"] == 110.0
    assert result["net_profit"] == 60.0


@pytest.mark.asyncio
async def test_range_report_includes_zero_activity_days(monkeypatch):
    async def fake_dec_transactions(query):
        if query["transaction_date"] == "2026-10-01":
            return [{"total_amount": 100.0, "total_cost": 30.0, "total_quantity": 2.0}]
        if query["transaction_date"] == "2026-10-03":
            return [{"total_amount": 80.0, "total_cost": 20.0, "total_quantity": 4.0}]
        return []

    async def fake_dec_expenses(query):
        if query.get("date") == "2026-10-02":
            return [{"amount": 12.0}]
        return []

    monkeypatch.setattr(reports, "_dec_transactions", AsyncMock(side_effect=fake_dec_transactions))
    monkeypatch.setattr(reports, "_dec_expenses", AsyncMock(side_effect=fake_dec_expenses))

    result = await reports.range_report("2026-10-01", "2026-10-03", current_user={"role": "ADMIN"})

    assert result["days"] == 3
    assert result["daily"][1]["date"] == "2026-10-02"
    assert result["daily"][1]["total_revenue"] == 0.0
    assert result["daily"][1]["total_expenses"] == 12.0
    assert result["total_revenue"] == 180.0
    assert result["net_profit"] == 118.0


@pytest.mark.asyncio
async def test_master_report_returns_management_expenses_and_payroll(monkeypatch):
    monkeypatch.setattr(reports, "_dec_expenses", AsyncMock(return_value=[{"amount": 15.0}]))
    monkeypatch.setattr(
        reports,
        "_salary_expense_summary",
        AsyncMock(return_value={
            "salary_expense_total": 90.0,
            "employees": [{"employee_name": "Laundry Attendant", "salary_expense": 90.0}],
            "period_details": [],
        }),
    )

    result = await reports.master_report(
        start_date="2026-10-10",
        end_date="2026-10-10",
        current_user={"role": "ADMIN"},
    )

    assert result["source_of_truth"] == "management_payroll_and_expenses"
    assert result["salary_expense_total"] == 90.0
    assert result["other_expense_total"] == 15.0
    assert result["total_expenses"] == 105.0
    assert result["salary_expenses"][0]["employee_name"] == "Laundry Attendant"
    assert "total_income" not in result


def test_fixed_monthly_salary_is_prorated_by_calendar_days():
    calculation = {
        "salary_type": "MONTHLY",
        "attendance_required": False,
        "basic_salary": 3100.0,
        "base_salary_for_period": 3100.0,
        "allowance": 310.0,
        "allowance_type": "FIXED",
        "allowance_for_period": 310.0,
        "components": [{"type": "BONUS", "name": "Monthly bonus", "amount": 310.0}],
        "bonus": 310.0,
        "other_payments": 0.0,
        "other_deductions": 0.0,
        "epf_base": "ADJUSTED",
        "epf_basis": 3100.0,
        "epf_rate": 8.0,
        "etf_rate": 3.0,
        "epf_employee": 248.0,
        "epf_employer": 248.0,
        "etf_employer": 93.0,
        "overtime_pay": 0.0,
        "extra_work_total": 0.0,
        "advance_deductions": 0.0,
    }

    result = reports._prorate_monthly_salary(calculation, month_days=31, covered_days=10)

    assert result["base_salary_for_period"] == 1000.0
    assert result["allowance_for_period"] == 100.0
    assert result["components"][0]["amount"] == 100.0
    assert result["epf_employer"] == 80.0
    assert result["etf_employer"] == 30.0
    assert result["salary_expense"] == 1310.0


@pytest.mark.asyncio
async def test_saved_report_can_be_created_edited_and_deleted(monkeypatch):
    collection = FakeSavedReports()
    monkeypatch.setattr(reports, "saved_reports_collection", lambda: collection)
    monkeypatch.setattr(reports, "log_audit", AsyncMock())
    current_user = {"user_id": "manager-1", "role": "MANAGER"}
    payload = reports.SavedReportCreate(
        title="October operations",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        selected_quotations={"Sunrise Hotel": "quote-1"},
        report_data={"total_income": 1250.0},
    )

    created = await reports.create_saved_report(payload, current_user)
    updated = await reports.update_saved_report(
        created["id"],
        reports.SavedReportUpdate(title="October receiving report"),
        current_user,
    )
    with pytest.raises(HTTPException) as error:
        await reports.update_saved_report(
            created["id"],
            reports.SavedReportUpdate(end_date=date(2026, 11, 1)),
            current_user,
        )
    deleted = await reports.delete_saved_report(created["id"], current_user)

    assert created["title"] == "October operations"
    assert updated["title"] == "October receiving report"
    assert error.value.status_code == 400
    assert deleted == {"deleted": True, "id": created["id"]}
