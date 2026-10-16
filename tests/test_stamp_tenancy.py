"""Tests for the legacy laundry_id stamping CLI (scripts/stamp_tenancy.py)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MASTER_KEY", "test-master-key-1234567890")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mongomock_motor import AsyncMongoMockClient

from scripts.stamp_tenancy import (
    MISSING_LAUNDRY_ID,
    select_collections,
    stamp_database,
)


async def _seed():
    client = AsyncMongoMockClient()
    db = client["main"]
    await db["customers"].insert_many(
        [
            {"laundry_id": "lovelaundry", "n": 1},
            {"laundry_id": "acme", "n": 2},
            {"n": 3},
            {"n": 4, "laundry_id": None},
        ]
    )
    await db["idempotency_keys"].insert_one({"key": "k"})
    return db


async def test_dry_run_counts_without_writing():
    db = await _seed()

    counts = await stamp_database(db, "lovelaundry", apply=False)

    assert counts["customers"] == 2
    assert await db["customers"].count_documents(MISSING_LAUNDRY_ID) == 2


async def test_apply_stamps_only_missing_and_never_overwrites():
    db = await _seed()

    counts = await stamp_database(db, "lovelaundry", apply=True)

    assert counts["customers"] == 2
    assert await db["customers"].count_documents(MISSING_LAUNDRY_ID) == 0
    assert await db["customers"].count_documents({"laundry_id": "lovelaundry"}) == 3
    assert await db["customers"].find_one({"laundry_id": "acme"}) is not None


async def test_apply_skips_infrastructure_collections():
    db = await _seed()

    counts = await stamp_database(db, "lovelaundry", apply=True)

    assert "idempotency_keys" not in counts


async def test_apply_is_idempotent():
    db = await _seed()
    await stamp_database(db, "lovelaundry", apply=True)

    assert await stamp_database(db, "lovelaundry", apply=True) == {"customers": 0}


async def test_only_and_skip_filters():
    db = await _seed()

    only = await stamp_database(db, "lovelaundry", only={"customers"})
    assert set(only) == {"customers"}

    skipped = await stamp_database(db, "lovelaundry", skip={"customers"})
    assert "customers" not in skipped


def test_select_collections_excludes_infra_and_system():
    names = ["customers", "system.profile", "idempotency_keys", "transactions"]

    assert select_collections(names) == ["customers", "transactions"]
    assert select_collections(names, only={"customers"}) == ["customers"]
    assert select_collections(names, skip={"transactions"}) == ["customers"]
