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
    auth_id: str,
    action: str,
    entity_type: str,
    entity_id: Optional[str],
    details: Optional[dict] = None,
    audit_collection: Any = None,
) -> None:
    """Write an audit trail entry."""
    from datetime import datetime, timezone

    if audit_collection is None:
        from .database.main_db import audit_logs_collection

        audit_collection = audit_logs_collection()

    entry = {
        "user_id": auth_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": details or {},
        "created_at": datetime.now(timezone.utc),
    }
    try:
        await audit_collection.insert_one(entry)
    except Exception:
        pass