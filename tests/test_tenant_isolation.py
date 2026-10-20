"""Tenant isolation for laundry-management money paths.

Same contract as bill-service's test_tenant_isolation: one session acts
inside its own laundry, a second laundry proves denial (404 oracle / scoped
lists), and pre-tenancy documents without laundry_id read as LoveLaundry's.
"""
import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MASTER_KEY", "test-master-key-1234567890")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from mongomock_motor import AsyncMongoMockClient

from src import tenancy
from src.tenancy import (
    LOVE_LAUNDRY_ID,
    assert_same_laundry,
    laundry_match,
    resolve_laundry_id,
    scoped_query,
)
from src.routers import payments as pay_router
from src.routers import salary as salary_router
from src.routers import transactions as txn_router

LAUNDRY_A = "acme"
LAUNDRY_B = "other"


def tenant_user(laundry_id=LAUNDRY_A, role="ADMIN", status="ACTIVE"):
    return {
        "auth_id": "tester",
        "user_id": "tester",
        "user_name": "Tester",
        "role": role,
        "laundry_id": laundry_id,
        "laundries": [{"id": laundry_id, "role": role, "status": status}],
    }


# ── tenancy helpers (pure) ────────────────────────────────────────────────

def test_resolve_laundry_fails_closed():
    with pytest.raises(HTTPException) as err:
        resolve_laundry_id(None)
    assert err.value.status_code == 401
    with pytest.raises(HTTPException) as err:
        resolve_laundry_id({"role": "ADMIN", "laundries": []})
    assert err.value.status_code == 400
    with pytest.raises(HTTPException) as err:
        resolve_laundry_id({
            "role": "ADMIN", "laundry_id": LAUNDRY_A,
            "laundries": [{"id": LAUNDRY_B, "status": "ACTIVE"}],
        })
    assert err.value.status_code == 403
    with pytest.raises(HTTPException) as err:
        resolve_laundry_id({
            "role": "ADMIN", "laundry_id": LAUNDRY_A,
            "laundries": [{"id": LAUNDRY_A, "status": "INACTIVE"}],
        })
    assert err.value.status_code == 403
    assert resolve_laundry_id(tenant_user()) == LAUNDRY_A
    # Platform/support context bypasses membership but still needs a laundry.
    platform = {"role": "PLATFORM_ADMIN", "laundry_id": LAUNDRY_B, "laundries": [],
                "platform_admin": True}
    assert resolve_laundry_id(platform) == LAUNDRY_B


def test_love_laundry_sees_legacy_unstamped_documents():
    scoped = scoped_query({"kind": "txn"}, LOVE_LAUNDRY_ID)
    tenant_clause = scoped["$and"][1]["$or"]
    assert {"laundry_id": LOVE_LAUNDRY_ID} in tenant_clause
    assert {"laundry_id": {"$exists": False}} in tenant_clause
    # A foreign tenant never matches an unstamped (pre-tenancy) document.
    foreign = scoped_query({"kind": "txn"}, LAUNDRY_B)
    assert foreign == {"kind": "txn", "laundry_id": LAUNDRY_B}


def test_assert_same_laundry_404s_across_tenants():
    with pytest.raises(HTTPException) as err:
        assert_same_laundry({"laundry_id": LAUNDRY_B}, LAUNDRY_A, "Transaction")
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        assert_same_laundry(None, LAUNDRY_A, "Transaction")
    assert err.value.status_code == 404
    # Legacy document belongs to LoveLaundry only.
    assert_same_laundry({}, LOVE_LAUNDRY_ID, "Transaction")
    assert_same_laundry({"laundry_id": LAUNDRY_A}, LAUNDRY_A, "Transaction")


# ── mocked collections ────────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    client = AsyncMongoMockClient()

    def bind(module, *names):
        for name in names:
            monkeypatch.setattr(module, name, (lambda coll=name: client["main"][coll]))

    bind(txn_router, "transactions_collection", "customers_collection", "items_collection")
    bind(pay_router, "payments_collection", "customers_collection")
    bind(salary_router, "salary_slips_collection")
    monkeypatch.setattr(txn_router, "log_audit", AsyncMock())
    monkeypatch.setattr(pay_router, "log_audit", AsyncMock())
    monkeypatch.setattr(salary_router, "log_audit", AsyncMock())
    return client["main"]


async def _seed(collection, laundry_id, n=1, **extra):
    ids = []
    for i in range(n):
        doc = {"laundry_id": laundry_id, "total_amount": 10.0 + i,
               "customer_name": f"c-{laundry_id}-{i}", "status": "COMPLETED",
               "items": [], **extra}
        res = await collection().insert_one(doc)
        ids.append(res.inserted_id)
    return ids


# ── transactions: the revenue ledger ──────────────────────────────────────

@pytest.mark.asyncio
async def test_transaction_list_is_scoped_to_session_laundry(db):
    await _seed(txn_router.transactions_collection, LAUNDRY_A, 2)
    await _seed(txn_router.transactions_collection, LAUNDRY_B, 3)
    legacy = {"customer_name": "pre-tenancy", "total_amount": 99.0,
              "items": [], "status": "COMPLETED"}
    await txn_router.transactions_collection().insert_one(dict(legacy))

    own = await txn_router.list_transactions(
        None, None, None, None, None, 200, 0, tenant_user(LAUNDRY_A)
    )
    assert own["total"] == 2

    # Legacy (unstamped) history belongs to LoveLaundry, the sole pre-tenancy
    # tenant — readable only by its session, never by a foreign laundry.
    love = await txn_router.list_transactions(
        None, None, None, None, None, 200, 0, tenant_user(LOVE_LAUNDRY_ID)
    )
    assert love["total"] == 1  # only the unstamped legacy row

    foreign = await txn_router.list_transactions(
        None, None, None, None, None, 200, 0, tenant_user(LAUNDRY_B)
    )
    assert foreign["total"] == 3
    assert all(row["customer_name"] != "pre-tenancy" for row in foreign["items"])


@pytest.mark.asyncio
async def test_transaction_cross_tenant_is_a_404_oracle(db):
    (foreign_id,) = await _seed(txn_router.transactions_collection, LAUNDRY_B)
    user_a = tenant_user(LAUNDRY_A)

    with pytest.raises(HTTPException) as err:
        await txn_router.get_transaction(str(foreign_id), user_a)
    assert err.value.status_code == 404

    from src.routers.transactions import TransactionUpdate
    with pytest.raises(HTTPException) as err:
        await txn_router.update_transaction(
            str(foreign_id), TransactionUpdate(notes="tampered"), user_a
        )
    assert err.value.status_code == 404

    with pytest.raises(HTTPException) as err:
        await txn_router.delete_transaction(str(foreign_id), user_a)
    assert err.value.status_code == 404

    # The foreign document is untouched.
    survivor = await txn_router.transactions_collection().find_one({"_id": foreign_id})
    assert survivor and "tampered" not in (survivor.get("notes") or "")


@pytest.mark.asyncio
async def test_create_transaction_stamps_the_session_laundry(db):
    created = await txn_router._store_transaction(
        {"customer_name": "Walk-in", "items": []},
        tenant_user(LAUNDRY_A),
        LAUNDRY_A,
    )
    raw = await txn_router.transactions_collection().find_one(
        {"_id": ObjectId(created["id"])}
    )
    assert raw["laundry_id"] == LAUNDRY_A


# ── payments: cash in ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payment_list_scoped_and_delete_is_404_across_tenants(db):
    await pay_router.payments_collection().insert_one(
        {"laundry_id": LAUNDRY_A, "amount": 100.0, "payment_date": "2026-10-01",
         "customer_name": "a"}
    )
    (foreign_id,) = await _seed(pay_router.payments_collection, LAUNDRY_B)

    listing = await pay_router.list_payments(
        None, None, None, 50, 0, tenant_user(LAUNDRY_A)
    )
    assert listing["total"] == 1

    with pytest.raises(HTTPException) as err:
        await pay_router.delete_payment(str(foreign_id), tenant_user(LAUNDRY_A))
    assert err.value.status_code == 404
    assert await pay_router.payments_collection().find_one({"_id": foreign_id})


# ── salary slips: payroll ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_salary_slip_read_and_list_are_tenant_scoped(db):
    await salary_router.salary_slips_collection().insert_one(
        {"laundry_id": LAUNDRY_A, "employee_id": "e1", "status": "FINALIZED",
         "period_start": "2026-10-01", "period_end": "2026-10-31",
         "created_at": "2026-10-01"}
    )
    (foreign_id,) = await _seed(
        salary_router.salary_slips_collection, LAUNDRY_B,
        employee_id="e9", status="FINALIZED",
        period_start="2026-10-01", period_end="2026-10-31", created_at="2026-10-01",
    )

    own = await salary_router.list_salary_slips(
        None, None, None, None, False, 50, 0, tenant_user(LAUNDRY_A)
    )
    assert own["total"] == 1

    with pytest.raises(HTTPException) as err:
        await salary_router.get_salary_slip(str(foreign_id), tenant_user(LAUNDRY_A))
    assert err.value.status_code == 404
