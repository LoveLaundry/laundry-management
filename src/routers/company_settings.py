from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..auth_helper import require_capability
from ..database.main_db import company_settings_collection
from ..models import CompanySettingsUpdate
from ..router_utils import serialize, log_audit

router = APIRouter(tags=["Company Settings"])

DEFAULT_SETTINGS = {
    "working_days_per_week": 6,
    "working_days_pattern": [0, 1, 2, 3, 4, 5],
    "default_overtime_rate": 0,
    "salary_basis_days": 30,
}


@router.get("/company-settings")
async def get_settings(
    current_user: dict = Depends(require_capability("employee:read")),
):
    doc = await company_settings_collection().find_one({"key": "main"})
    if doc:
        return serialize(doc, [])
    return {
        "key": "main",
        **DEFAULT_SETTINGS,
    }


@router.put("/company-settings")
async def update_settings(
    payload: CompanySettingsUpdate,
    current_user: dict = Depends(require_capability("employee:write")),
):
    existing = await company_settings_collection().find_one({"key": "main"})
    updates = payload.model_dump(exclude_none=True)

    if not existing:
        doc = {"key": "main", **DEFAULT_SETTINGS, **updates, "updated_at": datetime.now(timezone.utc)}
        result = await company_settings_collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        await log_audit(
            str(current_user.get("user_id", "")),
            "create", "company_settings", "main", details=updates,
        )
        return serialize(doc, [])

    updates["updated_at"] = datetime.now(timezone.utc)
    await company_settings_collection().update_one({"key": "main"}, {"$set": updates})
    await log_audit(
        str(current_user.get("user_id", "")),
        "update", "company_settings", "main", details=updates,
    )
    updated = await company_settings_collection().find_one({"key": "main"})
    return serialize(updated, [])
