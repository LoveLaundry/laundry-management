import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MASTER_KEY", "test-master-key-1234567890")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unittest.mock import AsyncMock

import pytest

from src.routers import reports


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
async def test_master_report_uses_gate_pass_quotation_pricing(monkeypatch):
    gate_passes = [
        {
            "client_name": "Sunrise Hotel",
            "receiving_date": "2026-10-10",
            "quotation_id": "q1",
            "items": [
                {"item_name": "Shirt", "received_qty": 10.0},
                {"item_name": "Towel", "received_qty": 3.0},
            ],
        },
        {
            "client_name": "Sunrise Hotel",
            "receiving_date": "2026-10-10",
            "quotation_id": "q1",
            "items": [
                {"item_name": "Shirt", "received_qty": 5.0},
            ],
        },
    ]

    monkeypatch.setattr(reports, "_list_gate_passes_for_range", AsyncMock(return_value=gate_passes))
    monkeypatch.setattr(
        reports,
        "_resolve_quotation_prices",
        AsyncMock(return_value={"Shirt": {"price": 25.0}, "Towel": {"price": 40.0}}),
    )
    monkeypatch.setattr(reports, "_dec_expenses", AsyncMock(return_value=[]))
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

    assert result["source_of_truth"] == "gate_pass_quotation_prices"
    assert result["total_income"] == 495.0
    assert result["salary_expense_total"] == 90.0
    assert result["total_expenses"] == 90.0
    assert result["salary_expenses"][0]["employee_name"] == "Laundry Attendant"
    assert result["net_profit"] == 405.0
    assert result["hotel_summary"][0]["client_name"] == "Sunrise Hotel"
    assert result["hotel_summary"][0]["income"] == 495.0


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
