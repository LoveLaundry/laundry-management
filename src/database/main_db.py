"""
Central MongoDB collection access for the Laundry Management service.

Uses the same three-database architecture as bill_service. All business data
lives in the MAIN database; SECONDARY/LOCAL are reserved for replication.
"""
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from .connection_manager import get_database

CLIENTS = "clients"
CUSTOMERS = "customers"
CUSTOMER_RATES = "customer_rates"
CATEGORIES = "categories"
ITEMS = "items"
TRANSACTIONS = "transactions"
PAYMENTS = "payments"
EMPLOYEES = "employees"
SALARIES = "salaries"
ATTENDANCE = "attendance"
SALARY_ADVANCES = "salary_advances"
SALARY_SLIPS = "salary_slips"
HOLIDAYS = "holidays"
EXTRA_WORK_CATEGORIES = "extra_work_categories"
EXTRA_WORK_RECORDS = "extra_work_records"
COMPANY_SETTINGS = "company_settings"
EXPENSE_CATEGORIES = "expense_categories"
EXPENSES = "expenses"
IMPORTS = "imports"
AUDIT_LOGS = "audit_logs"


def _db() -> AsyncIOMotorDatabase:
    return get_database("main")


def clients_collection() -> AsyncIOMotorCollection:
    return _db()[CLIENTS]


def customers_collection() -> AsyncIOMotorCollection:
    return _db()[CUSTOMERS]


def customer_rates_collection() -> AsyncIOMotorCollection:
    return _db()[CUSTOMER_RATES]


def categories_collection() -> AsyncIOMotorCollection:
    return _db()[CATEGORIES]


def items_collection() -> AsyncIOMotorCollection:
    return _db()[ITEMS]


def transactions_collection() -> AsyncIOMotorCollection:
    return _db()[TRANSACTIONS]


def payments_collection() -> AsyncIOMotorCollection:
    return _db()[PAYMENTS]


def employees_collection() -> AsyncIOMotorCollection:
    return _db()[EMPLOYEES]


def salaries_collection() -> AsyncIOMotorCollection:
    return _db()[SALARIES]


def attendance_collection() -> AsyncIOMotorCollection:
    return _db()[ATTENDANCE]


def salary_advances_collection() -> AsyncIOMotorCollection:
    return _db()[SALARY_ADVANCES]


def salary_slips_collection() -> AsyncIOMotorCollection:
    return _db()[SALARY_SLIPS]


def holidays_collection() -> AsyncIOMotorCollection:
    return _db()[HOLIDAYS]


def extra_work_categories_collection() -> AsyncIOMotorCollection:
    return _db()[EXTRA_WORK_CATEGORIES]


def extra_work_records_collection() -> AsyncIOMotorCollection:
    return _db()[EXTRA_WORK_RECORDS]


def company_settings_collection() -> AsyncIOMotorCollection:
    return _db()[COMPANY_SETTINGS]


def expense_categories_collection() -> AsyncIOMotorCollection:
    return _db()[EXPENSE_CATEGORIES]


def expenses_collection() -> AsyncIOMotorCollection:
    return _db()[EXPENSES]


def imports_collection() -> AsyncIOMotorCollection:
    return _db()[IMPORTS]


def audit_logs_collection() -> AsyncIOMotorCollection:
    return _db()[AUDIT_LOGS]


async def ensure_indexes() -> None:
    """Create all required indexes at startup."""
    await customers_collection().create_index("name_search", background=True)
    await customers_collection().create_index([("created_at", -1)], background=True)
    await customers_collection().create_index("name", background=True)
    await customers_collection().create_index("phone", background=True)

    await customer_rates_collection().create_index("customer_id", background=True)
    await customer_rates_collection().create_index("item_id", background=True)

    await items_collection().create_index("name_search", background=True)
    await items_collection().create_index("category_id", background=True)
    await items_collection().create_index("name", background=True)

    await transactions_collection().create_index([("transaction_date", -1)], background=True)
    await transactions_collection().create_index("customer_id", background=True)
    await transactions_collection().create_index("invoice_search", background=True)
    await transactions_collection().create_index("created_at", background=True)
    await transactions_collection().create_index("import_batch_id", background=True)
    await transactions_collection().create_index("employee_id", background=True)
    await transactions_collection().create_index("source", background=True)

    await payments_collection().create_index("customer_id", background=True)
    await payments_collection().create_index("payment_date", background=True)
    await payments_collection().create_index([("payment_date", -1), ("type", 1)], background=True)

    await employees_collection().create_index("name_search", background=True)
    await employees_collection().create_index("status", background=True)

    await salaries_collection().create_index("employee_id", background=True)
    await salaries_collection().create_index("month", background=True)

    await attendance_collection().create_index("employee_id", background=True)
    await attendance_collection().create_index("date", background=True)
    await attendance_collection().create_index([("employee_id", 1), ("date", 1)], unique=True, background=True)

    await salary_advances_collection().create_index("employee_id", background=True)
    await salary_advances_collection().create_index("status", background=True)
    await salary_advances_collection().create_index("date", background=True)
    await salary_advances_collection().create_index([("employee_id", 1), ("date", -1)], background=True)

    await salary_slips_collection().create_index("employee_id", background=True)
    await salary_slips_collection().create_index([("employee_id", 1), ("period_start", 1)], background=True)
    await salary_slips_collection().create_index("slip_number", background=True)
    await salary_slips_collection().create_index("status", background=True)
    await salary_slips_collection().create_index("period_start", background=True)

    await holidays_collection().create_index("date", background=True)
    await holidays_collection().create_index([("date", 1)], unique=True, background=True)

    await extra_work_categories_collection().create_index("name", background=True)
    await extra_work_categories_collection().create_index("is_active", background=True)

    await extra_work_records_collection().create_index("employee_id", background=True)
    await extra_work_records_collection().create_index("category_id", background=True)
    await extra_work_records_collection().create_index("date", background=True)
    await extra_work_records_collection().create_index([("employee_id", 1), ("date", 1)], background=True)

    await company_settings_collection().create_index("key", background=True)

    await expense_categories_collection().create_index("name_search", background=True)

    await expenses_collection().create_index("expense_date", background=True)
    await expenses_collection().create_index("category_id", background=True)
    await expenses_collection().create_index("date", background=True)

    await imports_collection().create_index("status", background=True)
    await imports_collection().create_index("created_at", background=True)

    await audit_logs_collection().create_index("created_at", background=True)
    await audit_logs_collection().create_index([("entity_type", 1), ("entity_id", 1)], background=True)