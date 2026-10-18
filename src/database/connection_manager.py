from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from ..config import settings

ROLE_MAIN = "main"
ROLE_SECONDARY = "secondary"
ROLE_LOCAL = "local"

_clients: dict = {}
_mongo_uris: dict = {}


def _client_for(role: str) -> AsyncIOMotorClient:
    """Lazily create (and cache) a motor client for the given role."""
    if role in _clients and _clients[role] is not None:
        return _clients[role]

    if role == ROLE_MAIN:
        uri = settings.resolve_main_uri()
    elif role == ROLE_SECONDARY:
        uri = settings.resolve_secondary_uri()
    elif role == ROLE_LOCAL:
        uri = settings.resolve_local_uri()
    else:
        raise ValueError(f"Unknown role: {role}")

    # Respect explicit per-role config from bill_service-style .env
    if role == ROLE_MAIN and settings.mongodb_main_uri:
        uri = settings.mongodb_main_uri
    elif role == ROLE_SECONDARY and settings.mongodb_secondary_uri:
        uri = settings.mongodb_secondary_uri
    elif role == ROLE_LOCAL and settings.mongodb_local_uri:
        uri = settings.mongodb_local_uri

    _mongo_uris[role] = uri
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    _clients[role] = client
    return client


def _db_name_for(role: str) -> str:
    if role == ROLE_MAIN:
        return settings.resolve_main_db()
    if role == ROLE_SECONDARY:
        return settings.resolve_secondary_db()
    if role == ROLE_LOCAL:
        return settings.resolve_local_db()
    raise ValueError(f"Unknown role: {role}")


def get_database(role: str = ROLE_MAIN):
    client = _client_for(role)
    return client[_db_name_for(role)]


async def ping(role: str = ROLE_MAIN) -> bool:
    """Quick health check — returns True if the role database is reachable."""
    try:
        await get_database(role).command("ping")
        return True
    except Exception:
        return False