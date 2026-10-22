"""PLATFORM_ADMIN holds every capability (support sessions must operate)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.auth_helper import ROLE_CAPABILITIES, require_capability


def test_platform_admin_holds_full_capabilities():
    assert set(ROLE_CAPABILITIES["PLATFORM_ADMIN"]) == set(ROLE_CAPABILITIES["ADMIN"])
    dep = require_capability("salary:write")
    caller = {"auth_id": "root", "role": "PLATFORM_ADMIN"}
    assert dep(caller) is caller
