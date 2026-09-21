"""Idempotency guard for management create endpoints.

Clients retrying a POST (network drop, offline queue) send an
``X-Idempotency-Key`` header. The first request performs the operation and
memorizes key -> a result marker. Retries with the same key are answered
from the memory instead of re-applying the mutation (e.g. duplicating an
expense or an attendance bulk-day run).

Keys are namespaced per user so two admins can never collide.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request

from ..database.main_db import idempotency_keys_collection


def _get_key(request: Request, user_id: str) -> Optional[str]:
    key = request.headers.get("X-Idempotency-Key")
    if not key:
        return None
    cleaned = (key or "").strip()
    if not cleaned or len(cleaned) > 128:
        raise HTTPException(status_code=400, detail="Invalid X-Idempotency-Key header.")
    return f"{user_id}:{cleaned}"


async def was_processed(request: Request, user_id: str) -> bool:
    """True when this key was already processed (caller short-circuits)."""
    key = _get_key(request, user_id)
    if not key:
        return False
    return await idempotency_keys_collection().find_one({"key": key}) is not None


async def mark_processed(request: Request, user_id: str, operation: str, entity_id: Optional[str] = None) -> None:
    """Remember that this key was handled."""
    key = _get_key(request, user_id)
    if not key:
        return
    try:
        await idempotency_keys_collection().insert_one(
            {
                "key": key,
                "operation": operation,
                "entity_id": entity_id,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:
        pass