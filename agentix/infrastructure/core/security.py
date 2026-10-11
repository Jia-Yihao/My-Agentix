"""
Security utilities for API key authentication and JWT token management.
"""

import secrets
from typing import Optional

from fastapi import Header, HTTPException, status

from agentix.config import settings


async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> str:
    """FastAPI dependency that validates the X-API-Key header.

    Returns the validated API key or raises 401.
    """
    if not settings.REQUIRE_API_KEY:
        return ""

    if not x_api_key or not secrets.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return x_api_key
