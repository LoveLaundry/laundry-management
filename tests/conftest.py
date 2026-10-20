"""Shared test harness for laundry-management.

The service refuses to import without its secrets (deliberate: a missing
MASTER_KEY must never silently fall back to a hard-coded key). The harness
supplies deterministic test-only values *before* any ``src.*`` import.
Real environment variables always win.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MASTER_KEY", "test-master-key-1234567890")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

LOVE_LAUNDRY_ID = "lovelaundry"


def auth_user(
    name: str = "Alice",
    role: str = "ADMIN",
    laundry_id: str = LOVE_LAUNDRY_ID,
    status: str = "ACTIVE",
) -> dict:
    """A signed-session-shaped user dict — the claims production JWTs carry."""
    return {
        "auth_id": name.lower(),
        "user_id": name.lower(),
        "user_name": name,
        "role": role,
        "laundry_id": laundry_id,
        "laundries": [{"id": laundry_id, "role": role, "status": status}],
    }


@pytest.fixture
def auth_user_factory():
    return auth_user
