import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.database import main_db


class FakeCollection:
    def __init__(self):
        self.indexes = {"date_1": {"key": [("date", 1)], "unique": True}}
        self.operations = []

    async def create_index(self, keys, **options):
        normalized = [(keys, 1)] if isinstance(keys, str) else list(keys)
        name = "_".join(f"{key}_{direction}" for key, direction in normalized)
        self.operations.append(("create", name, options))
        self.indexes[name] = {"key": normalized, **options}
        return name

    async def index_information(self):
        return self.indexes

    async def drop_index(self, name):
        self.operations.append(("drop", name))
        del self.indexes[name]


@pytest.mark.asyncio
async def test_holiday_index_migrates_to_tenant_scope_and_keeps_date_lookup(monkeypatch):
    collection = FakeCollection()
    other_collections = (
        "customers_collection",
        "customer_rates_collection",
        "items_collection",
        "transactions_collection",
        "payments_collection",
        "employees_collection",
        "salaries_collection",
        "attendance_collection",
        "salary_advances_collection",
        "salary_slips_collection",
        "salary_packages_collection",
        "extra_work_categories_collection",
        "extra_work_records_collection",
        "company_settings_collection",
        "electricity_meter_readings_collection",
        "expense_categories_collection",
        "expenses_collection",
        "expense_templates_collection",
        "idempotency_keys_collection",
        "imports_collection",
        "audit_logs_collection",
        "saved_reports_collection",
    )
    for name in other_collections:
        monkeypatch.setattr(main_db, name, lambda: FakeCollection())
    monkeypatch.setattr(main_db, "holidays_collection", lambda: collection)

    await main_db.ensure_indexes()

    compound_name = "laundry_id_1_date_1"
    compound_create = next(
        i for i, operation in enumerate(collection.operations)
        if operation[:2] == ("create", compound_name)
    )
    legacy_drop = collection.operations.index(("drop", "date_1"))
    date_create = max(
        i for i, operation in enumerate(collection.operations)
        if operation[:2] == ("create", "date_1")
    )

    assert compound_create < legacy_drop < date_create
    assert collection.indexes[compound_name]["unique"] is True
    assert "unique" not in collection.indexes["date_1"]
