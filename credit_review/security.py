"""The services' API key: one header, compared in constant time, and a missing setting means closed, not open."""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="The service's API key.")


def require_key(key: Annotated[str | None, Depends(api_key_header)]) -> None:
    expected = os.environ.get("CREDIT_API_KEY", "")
    if not expected:      # fail closed: a missing setting never means "open"
        raise HTTPException(503, "the service has no API key configured")
    if not key or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-API-Key", headers={"WWW-Authenticate": "ApiKey"})
