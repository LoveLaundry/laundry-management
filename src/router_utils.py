from typing import Optional, Any

from bson import ObjectId
from fastapi import HTTPException

from .crypto_helper import decrypt_dict


def parse_object_id(value: str, label: str = "id") -> ObjectId:
    """Parse and validate a string into a MongoDB ObjectId."""
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value}")


def serialize(doc: dict, sensitive_fields: list, exclude_fields: Optional[list] = None) -> dict:
    """Decrypt a stored document and convert _id to id. Excludes encryption metadata and search tokens."""
    exclude = exclude_fields or []
    if "encryption_metadata" in doc:
        try:
            decrypted = decrypt_dict(doc, sensitive_fields)
        except (ValueError, KeyError):
            decrypted = {
                k: v
                for k, v in doc.items()
                if k != "encryption_metadata" and not k.endswith("_search")
            }
    else:
        decrypted = {k: v for k, v in doc.items() if not k.endswith("_search")}

    result = {}
    for key, val in decrypted.items():
        if key == "_id":
            result["id"] = str(val)
        elif key not in exclude:
            result[key] = val
    return result


async def log_audit(
    auth_id,
    action: str,
    entity_type: str,
    entity_id: Optional[str],
    details: Optional[dict] = None,
    audit_collection: Any = None,
    laundry_id: Optional[str] = None,
) -> bool:
    """Write an audit trail entry.

    ``auth_id`` may be either the raw actor id (legacy call sites) or the
    FastAPI session dict, from which the actor id and tenant are derived so
    every entry is stamped with the laundry it belongs to.

    Writes the canonical field set (``auth_id``/``entity_type``/``timestamp``)
    plus the legacy aliases (``user_id``/``entity``/``created_at``) that the
    audit readers still key on, so a single write is readable from either
    convention. Returns ``True`` when the write succeeded; a failed write is
    logged (never silently swallowed) and returns ``False``.
    """
    from datetime import datetime, timezone
    import logging

    logger = logging.getLogger("laundry-management.audit")

    if isinstance(auth_id, dict):
        session = auth_id
        auth_id = str(session.get("user_id") or session.get("auth_id") or "")
        if laundry_id is None:
            laundry_id = (session.get("laundry_id") or "").strip().lower() or None
    else:
        auth_id = str(auth_id)

    if audit_collection is None:
        from .database.main_db import audit_logs_collection

        audit_collection = audit_logs_collection()

    now = datetime.now(timezone.utc)
    entry = {
        # canonical
        "auth_id": auth_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": details or {},
        "timestamp": now,
        # legacy aliases kept for readers that still key on them
        "user_id": auth_id,
        "entity": entity_type,
        "created_at": now,
    }
    if laundry_id:
        entry["laundry_id"] = laundry_id
    try:
        await audit_collection.insert_one(entry)
        return True
    except Exception:
        logger.warning(
            "Audit write failed action=%s entity_type=%s entity_id=%s auth_id=%s",
            action, entity_type, entity_id, auth_id, exc_info=True,
        )
        return False