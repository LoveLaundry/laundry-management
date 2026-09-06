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
    await customers_collection().create_index("name_search")
    await customers_collection().create_index([("created_at", -1)])

    await customer_rates_collection().create_index("customer_id")
    await customer_rates_collection().create_index("item_id")

    await items_collection().create_index("name_search")
    await items_collection().create_index("category_id")

    await transactions_collection().create_index([("transaction_date", -1)])
    await transactions_collection().create_index("customer_id")
    await transactions_collection().create_index("invoice_search")
    await transactions_collection().create_index("created_at")
    await transactions_collection().create_index("import_batch_id")

    await payments_collection().create_index("customer_id")
    await payments_collection().create_index("payment_date")

    await employees_collection().create_index("name_search")
    await employees_collection().create_index("status")

    await salaries_collection().create_index("employee_id")
    await salaries_collection().create_index("month")

    await attendance_collection().create_index("employee_id")
    await attendance_collection().create_index("date")

    await expense_categories_collection().create_index("name_search")

    await expenses_collection().create_index("expense_date")
    await expenses_collection().create_index("category_id")

    await imports_collection().create_index("status")
    await imports_collection().create_index("created_at")

    await audit_logs_collection().create_index("created_at")
    await audit_logs_collection().create_index([("entity_type", 1), ("entity_id", 1)])