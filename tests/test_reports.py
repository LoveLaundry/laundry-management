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

    result = await reports.daily_report(date="2026-10-10", current_user={"role": "ADMIN"})

    assert result["date"] == "2026-10-10"
    assert result["total_revenue"] == 150.0
    assert result["gross_profit"] == 110.0
    assert result["net_profit"] == 100.0


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
