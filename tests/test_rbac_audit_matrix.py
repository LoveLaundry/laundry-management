"""RBAC + audit matrix for laundry-management.

Data-driven matrix asserting the role/capability contract shipped in
``auth_helper.ROLE_CAPABILITIES`` (including the new PLATFORM_ADMIN entry),
the ``log_audit`` writer schema, tenant-scoped audit storage, and a
structural sweep proving every mutating route declares an auth dependency.

No live MongoDB is touched: collections are backed by mongomock-motor.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("MASTER_KEY", "test-master-key-1234567890")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import inspect

import pytest
from fastapi import HTTPException

from src import auth_helper
from src.auth_helper import ROLE_CAPABILITIES, require_capability

# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------
ROLES = ["ADMIN", "MANAGER", "STAFF", "PLATFORM_ADMIN"]

CAPABILITIES = sorted({cap for caps in ROLE_CAPABILITIES.values() for cap in caps})

_FULL = {c for c in CAPABILITIES}
_STAFF = {
    "management:read",
    "customer:read", "customer:write",
    "item:read",
    "transaction:read", "transaction:write",
    "payment:read",
    "dashboard:read",
}

CAPABILITY_MATRIX = {
    "ADMIN": _FULL,
    "MANAGER": _FULL,
    "STAFF": _STAFF,
    # New: platform operators inherit the full ADMIN capability set.
    "PLATFORM_ADMIN": _FULL,
}


def _run(user: dict, capability: str):
    return require_capability(capability)(user)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("capability", CAPABILITIES)
def test_capability_matrix(role, capability):
    user = {"auth_id": f"u-{role}", "role": role}
    should_allow = capability in CAPABILITY_MATRIX[role]
    if should_allow:
        assert _run(user, capability) is user
    else:
        with pytest.raises(HTTPException) as exc:
            _run(user, capability)
        assert exc.value.status_code == 403


def test_matrix_matches_shipped_table():
    for role, advertised in CAPABILITY_MATRIX.items():
        assert set(ROLE_CAPABILITIES[role]) == advertised, role


def test_platform_admin_can_write_sensitive_domains():
    """Regression: PLATFORM_ADMIN was previously denied every domain endpoint."""
    for cap in ("salary:write", "expense:write", "employee:write", "report:read", "management:write"):
        assert _run({"role": "PLATFORM_ADMIN"}, cap)


def test_staff_cannot_touch_payroll_or_expenses():
    for cap in ("salary:read", "expense:read", "employee:read", "report:read", "management:write"):
        with pytest.raises(HTTPException) as exc:
            _run({"role": "STAFF"}, cap)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["", "ADMIN ", None, "SUPERUSER"])
def test_unknown_roles_denied(role):
    user = {"role": role} if role is not None else {}
    with pytest.raises(HTTPException) as exc:
        _run(user, "management:read")
    assert exc.value.status_code == 403


def test_role_case_normalised():
    assert _run({"role": "admin"}, "management:read")


# ---------------------------------------------------------------------------
# log_audit schema
# ---------------------------------------------------------------------------
async def test_log_audit_schema_and_aliases():
    from src import router_utils

    class Coll:
        def __init__(self):
            self.docs = []

        async def insert_one(self, doc):
            self.docs.append(doc)
            return type("R", (), {"inserted_id": "x"})()

    coll = Coll()
    ok = await router_utils.log_audit("u-1", "create", "customer", "c-1", {"name": "x"}, audit_collection=coll)
    assert ok is True
    doc = coll.docs[0]
    # canonical
    assert doc["auth_id"] == "u-1"
    assert doc["entity_type"] == "customer"
    assert doc["entity_id"] == "c-1"
    assert doc["action"] == "create"
    assert doc["details"] == {"name": "x"}
    assert doc["timestamp"] is not None
    # legacy aliases
    assert doc["user_id"] == "u-1"
    assert doc["entity"] == "customer"
    assert doc["created_at"] is not None


async def test_log_audit_accepts_session_dict():
    from src import router_utils

    class Coll:
        def __init__(self):
            self.docs = []

        async def insert_one(self, doc):
            self.docs.append(doc)
            return type("R", (), {"inserted_id": "x"})()

    coll = Coll()
    session = {"user_id": "u-9", "role": "ADMIN", "laundry_id": "Acme"}
    await router_utils.log_audit(session, "update", "item", "i-1", audit_collection=coll)
    doc = coll.docs[0]
    assert doc["auth_id"] == "u-9"
    assert doc["laundry_id"] == "acme"  # normalised


async def test_log_audit_returns_false_on_failure():
    from src import router_utils

    class Boom:
        async def insert_one(self, _):
            raise RuntimeError("db down")

    assert await router_utils.log_audit("u", "a", "e", "1", audit_collection=Boom()) is False


# ---------------------------------------------------------------------------
# Structural sweep: every mutating route declares an auth dependency
# ---------------------------------------------------------------------------
AUTH_MARKERS = ("require_capability", "get_current_user", "security")
PUBLIC_PATHS = {"/", "/health", "/api/health", "/docs", "/openapi.json", "/redoc", "/health"}


def _flat(dependant) -> str:
    names = []
    stack = [dependant]
    while stack:
        current = stack.pop()
        call = getattr(current, "call", None)
        if call is not None:
            names.append(getattr(call, "__name__", ""))
        stack.extend(getattr(current, "dependencies", []) or [])
    return " ".join(names)


def _all_mutating_routes():
    from fastapi.routing import APIRoute

    import src.main as main

    from src.routers import routers

    seen = set()
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            path = route.path
            if path in PUBLIC_PATHS:
                continue
            methods = route.methods or set()
            if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            key = (path, tuple(sorted(methods)))
            if key in seen:
                continue
            seen.add(key)
            yield path, methods, route.dependant


def test_every_mutating_route_is_guarded():
    unguarded = []
    for path, methods, dependant in _all_mutating_routes():
        names = _flat(dependant)
        if not any(marker in names for marker in AUTH_MARKERS):
            unguarded.append(f"{sorted(methods)} {path}")
    assert not unguarded, "Unguarded mutating routes:\n  " + "\n  ".join(unguarded)


def test_sweep_is_not_vacuous():
    guarded = sum(
        1 for _p, _m, d in _all_mutating_routes()
        if any(marker in _flat(d) for marker in AUTH_MARKERS)
    )
    assert guarded > 50


# ---------------------------------------------------------------------------
# Audit endpoint: admin-only, tenant-scoped, both schemas readable
# ---------------------------------------------------------------------------
def test_audit_endpoint_requires_admin():
    from src.routers import dashboard

    dep = require_capability("management:read")
    # STAFF holds management:read, so the handler must add its own role gate.
    # We assert the handler source references the write-capability check.
    src = inspect.getsource(dashboard.list_audit_logs)
    assert "management:write" in src


def test_audit_endpoint_is_registered():
    from fastapi.routing import APIRoute

    from src.routers import dashboard

    paths = [r.path for r in dashboard.router.routes if isinstance(r, APIRoute)]
    assert "/audit-logs" in paths


def test_audit_serializer_reads_both_schemas():
    from src.routers.dashboard import _serialize_audit

    canonical = _serialize_audit({
        "auth_id": "u", "action": "a", "entity_type": "customer",
        "entity_id": "1", "details": {}, "timestamp": None,
    })
    assert canonical["auth_id"] == "u"
    assert canonical["entity_type"] == "customer"

    legacy = _serialize_audit({
        "user_id": "u", "action": "a", "entity": "customer",
        "entity_id": "1", "details": {}, "created_at": None,
    })
    assert legacy["auth_id"] == "u"
    assert legacy["entity_type"] == "customer"


# ---------------------------------------------------------------------------
# Tenant scoping of the audit collection
# ---------------------------------------------------------------------------
async def test_audit_collection_is_tenant_scoped(monkeypatch):
    """Writes via the scoped collection are stamped with the request tenant."""
    from mongomock_motor import AsyncMongoMockClient

    from src.tenant_scope import (
        TenantScopedCollection,
        reset_request_claims,
        set_request_claims,
    )

    client = AsyncMongoMockClient()
    raw = client["m"]["audit_logs"]

    claims = {
        "user_id": "u-1", "role": "ADMIN",
        "laundry_id": "acme",
        "laundries": [{"id": "acme", "status": "ACTIVE"}],
    }
    token = set_request_claims(claims)
    try:
        scoped = TenantScopedCollection(raw, "audit_logs")
        await scoped.insert_one({"action": "a", "entity_type": "customer"})
        docs = [d async for d in raw.find({})]
        assert docs and docs[0]["laundry_id"] == "acme"
    finally:
        reset_request_claims(token)
