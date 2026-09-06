import os
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer
import jwt

from dotenv import load_dotenv

load_dotenv()

import logging as _auth_log
_auth_logger = _auth_log.getLogger("auth")
_jwt_secret = os.getenv("JWT_SECRET")
if not _jwt_secret:
    _auth_logger.warning("JWT_SECRET not set — using insecure fallback. Set JWT_SECRET in production!")
_jwt_secret = _jwt_secret or "CHANGE-ME-IN-PRODUCTION-love-laundry-2026"
JWT_SECRET = _jwt_secret
JWT_ALGORITHM = "HS256"

security = HTTPBearer()


def verify_token(token: str) -> dict:
    """Verify and decode a JWT token (issued by user-service)."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")


def get_current_user(credentials=Security(security)) -> dict:
    token = credentials.credentials
    return verify_token(token)


ROLE_CAPABILITIES = {
    "ADMIN": [
        "management:read", "management:write",
        "customer:read", "customer:write",
        "item:read", "item:write",
        "transaction:read", "transaction:write",
        "expense:read", "expense:write",
        "employee:read", "employee:write",
        "salary:read", "salary:write",
        "payment:read", "payment:write",
        "report:read", "dashboard:read", "import:write",
    ],
    "MANAGER": [
        "management:read", "management:write",
        "customer:read", "customer:write",
        "item:read", "item:write",
        "transaction:read", "transaction:write",
        "expense:read", "expense:write",
        "employee:read", "employee:write",
        "salary:read", "salary:write",
        "payment:read", "payment:write",
        "report:read", "dashboard:read", "import:write",
    ],
    "STAFF": [
        "management:read",
        "customer:read",
        "item:read",
        "transaction:read", "transaction:write",
        "expense:read",
        "employee:read",
        "salary:read",
        "payment:read",
        "report:read", "dashboard:read",
    ],
}


def require_capability(capability: str):
    """FastAPI dependency to validate fine-grained capabilities."""
    def dependency(current_user: dict = Security(get_current_user)):
        role = str(current_user.get("role") or "").upper()
        capabilities = ROLE_CAPABILITIES.get(role, [])
        if capability not in capabilities:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' is not authorized to perform this action (missing {capability})",
            )
        return current_user

    return dependency