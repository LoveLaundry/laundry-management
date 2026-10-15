"""Tenant isolation for the laundry-management domain.

Same contract as bill_service.tenancy (see architecture_updated_system):
the trusted tenant comes ONLY from signed JWT claims; every tenant query
is scoped; cross-tenant access fails closed (404 oracle); pre-tenancy
documents without ``laundry_id`` read as LoveLaundry's, the sole historical
tenant, so deployment order cannot break production.
"""
from typing import Any, Dict, Optional

from fastapi import HTTPException

LOVE_LAUNDRY_ID = "lovelaundry"

ACTIVE = "ACTIVE"


def _claims_entries(current_user: dict) -> list:
    entries = current_user.get("laundries") or []
    return entries if isinstance(entries, list) else []


def is_platform_context(current_user: dict) -> bool:
    return bool(current_user.get("platform_admin")) or bool(current_user.get("support_entry"))


def resolve_laundry_id(current_user: Optional[dict]) -> str:
    if not isinstance(current_user, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    laundry_id = (current_user.get("laundry_id") or "").strip().lower()
    if not laundry_id:
        raise HTTPException(
            status_code=400,
            detail="No active laundry in this session — switch laundry first",
        )
    if is_platform_context(current_user):
        return laundry_id
    for entry in _claims_entries(current_user):
        if not isinstance(entry, dict):
            continue
        if (entry.get("id") or "").strip().lower() == laundry_id:
            if (entry.get("status") or "").strip().upper() == ACTIVE:
                return laundry_id
            raise HTTPException(
                status_code=403,
                detail="This laundry is not active — contact an administrator",
            )
    raise HTTPException(status_code=403, detail="No active assignment in that laundry")


def laundry_match(laundry_id: str) -> dict:
    if laundry_id == LOVE_LAUNDRY_ID:
        return {
            "$or": [
                {"laundry_id": laundry_id},
                {"laundry_id": {"$exists": False}},
                {"laundry_id": None},
            ]
        }
    return {"laundry_id": laundry_id}


def scoped_query(query: Optional[Dict[str, Any]], laundry_id: str) -> Dict[str, Any]:
    scoped = dict(query or {})
    match = laundry_match(laundry_id)
    if "$or" in match and scoped:
        return {"$and": [scoped, match]}
    if "$or" in match:
        return match
    scoped.update(match)
    return scoped


def match_stage(laundry_id: str) -> dict:
    return {"$match": laundry_match(laundry_id)}


def assert_same_laundry(doc: Optional[Dict[str, Any]], laundry_id: str, label: str = "record"):
    actual = None
    if isinstance(doc, dict):
        actual = doc.get("laundry_id") or LOVE_LAUNDRY_ID
    if actual != laundry_id:
        raise HTTPException(status_code=404, detail=f"{label} not found")


def owned_filter(oid, laundry_id: str) -> dict:
    filt: Dict[str, Any] = {"_id": oid}
    filt.update(laundry_match(laundry_id))
    return filt


async def stamp_missing_laundry_ids(collections: Dict[str, Any], laundry_id: str = LOVE_LAUNDRY_ID) -> Dict[str, int]:
    stamped: Dict[str, int] = {}
    for name, collection in collections.items():
        result = await collection.update_many(
            {"$or": [{"laundry_id": {"$exists": False}}, {"laundry_id": None}]},
            {"$set": {"laundry_id": laundry_id}},
        )
        stamped[name] = result.modified_count
    return stamped
