"""Fail-closed tenant enforcement at the collection boundary.

Why a proxy instead of editing every query
------------------------------------------
This service reaches MongoDB exclusively through the ``*_collection()`` helpers
in ``database/main_db.py``. Wrapping those helpers means a tenant predicate is
applied to every read and write in one place, instead of relying on ~100 call
sites each remembering to add ``laundry_id``. A missed call site is a silent
data leak, so the default has to be safe rather than opt-in.

The same proxy exists in ``quotation-service`` (synchronous pymongo build of
this file). ``bill_service`` / ``worker-service`` enforce the identical
contract, but explicitly at each call site through their ``tenancy.py`` helpers
(``scoped_query`` / ``owned_filter`` / ``match_stage``) rather than at the
collection boundary. The rules below are shared by all four services:

* The tenant comes only from signed JWT claims, via
  :func:`tenancy.resolve_laundry_id` — never from a body, query string or
  header.
* No resolvable tenant => the operation raises. There is no unscoped fallback.
* Inserts always carry the tenant; writing a document that names a different
  tenant is a programming error and raises rather than silently crossing over.
* Aggregations get a leading ``$match`` so pipelines cannot widen the scope.

``laundry_match`` keeps the transitional rule for documents that predate
multi-tenancy (they can only belong to LoveLaundry), so this can be deployed
before the backfill runs without hiding production data.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from fastapi import HTTPException

from .tenancy import laundry_match, resolve_laundry_id, scoped_query

_claims: ContextVar[Optional[dict]] = ContextVar("claims", default=None)
_tenant: ContextVar[str] = ContextVar("tenant", default="")


def set_request_claims(claims: Optional[dict]):
    """Bind the verified token claims to the current request."""
    _tenant.set("")
    return _claims.set(claims)


def reset_request_claims(token) -> None:
    _claims.reset(token)
    _tenant.set("")


def current_laundry_id() -> str:
    """Resolve (and memoise) the tenant for this request, or raise.

    Raises 400 when the session has not selected a laundry and 403 when the
    membership is not active — both fail closed before any query runs.
    """
    tenant = _tenant.get()
    if tenant:
        return tenant
    tenant = resolve_laundry_id(_claims.get())
    _tenant.set(tenant)
    return tenant


class TenantScopedCollection:
    """Motor collection wrapper that injects the tenant predicate.

    Only the operations this service actually uses are intercepted; anything
    else falls through to the raw collection so unrelated behaviour (index
    creation, etc.) keeps working.
    """

    __slots__ = ("_collection", "_name")

    def __init__(self, collection, name: str):
        self._collection = collection
        self._name = name

    # ── helpers ────────────────────────────────────────────────────────────
    def _scope(self, filt: Optional[dict]) -> dict:
        return scoped_query(filt or {}, current_laundry_id())

    def _stamp(self, document: Any) -> Any:
        tenant = current_laundry_id()
        if not isinstance(document, dict):
            return document
        declared = document.get("laundry_id")
        if declared is not None and str(declared).strip().lower() != tenant:
            raise HTTPException(
                status_code=400,
                detail=f"Refusing to write a {self._name} record for another laundry",
            )
        stamped = dict(document)
        stamped["laundry_id"] = tenant
        return stamped

    # ── reads ──────────────────────────────────────────────────────────────
    def find(self, filter=None, *args, **kwargs):
        return self._collection.find(self._scope(filter), *args, **kwargs)

    async def find_one(self, filter=None, *args, **kwargs):
        return await self._collection.find_one(self._scope(filter), *args, **kwargs)

    async def count_documents(self, filter=None, *args, **kwargs):
        return await self._collection.count_documents(self._scope(filter), *args, **kwargs)

    def aggregate(self, pipeline, *args, **kwargs):
        tenant = current_laundry_id()
        stages = list(pipeline or [])
        # A leading $match cannot be widened by later stages, so scope is fixed
        # before the caller's pipeline runs.
        scoped = [{"$match": laundry_match(tenant)}, *stages]
        return self._collection.aggregate(scoped, *args, **kwargs)

    # ── writes ─────────────────────────────────────────────────────────────
    async def insert_one(self, document, *args, **kwargs):
        return await self._collection.insert_one(self._stamp(document), *args, **kwargs)

    async def insert_many(self, documents, *args, **kwargs):
        return await self._collection.insert_many([self._stamp(d) for d in documents], *args, **kwargs)

    async def update_one(self, filter, update, *args, **kwargs):
        return await self._collection.update_one(self._scope(filter), update, *args, **kwargs)

    async def update_many(self, filter, update, *args, **kwargs):
        return await self._collection.update_many(self._scope(filter), update, *args, **kwargs)

    async def replace_one(self, filter, replacement, *args, **kwargs):
        return await self._collection.replace_one(
            self._scope(filter), self._stamp(replacement), *args, **kwargs
        )

    async def delete_one(self, filter, *args, **kwargs):
        return await self._collection.delete_one(self._scope(filter), *args, **kwargs)

    async def delete_many(self, filter, *args, **kwargs):
        return await self._collection.delete_many(self._scope(filter), *args, **kwargs)


    async def find_one_and_update(self, filter, update, *args, **kwargs):
        return await self._collection.find_one_and_update(
            self._scope(filter), update, *args, **kwargs
        )

    async def find_one_and_delete(self, filter, *args, **kwargs):
        return await self._collection.find_one_and_delete(self._scope(filter), *args, **kwargs)

    async def distinct(self, key, filter=None, *args, **kwargs):
        return await self._collection.distinct(key, self._scope(filter), *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._collection, item)


def decode_token(token: str) -> Optional[dict]:
    """Verify a bearer token, returning claims or None. Never raises."""
    import os

    import jwt

    secret = os.getenv("JWT_SECRET")
    if not secret:
        return None
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


class TenantClaimsMiddleware:
    """Bind verified JWT claims to the request so the collection guard can scope.

    Deliberately does not reject unauthenticated requests here: the auth
    dependency already does that per route, and raising from middleware would
    bypass the app's exception handlers. Data access is what fails closed.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        claims = None
        if raw.lower().startswith("bearer "):
            claims = decode_token(raw.split(" ", 1)[1].strip())

        token = set_request_claims(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_claims(token)
