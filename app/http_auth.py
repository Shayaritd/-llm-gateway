"""
Shared helper for pulling an API key out of a request: either a standard
`Authorization: Bearer <key>` header, or a named fallback header. Used by
both TeamAuthMiddleware (app.middleware.auth) and the admin API's
require_admin dependency (app.routers.admin) - identical mechanics, two
completely separate identity spaces.
"""
from __future__ import annotations

from starlette.requests import Request


def extract_api_key(request: Request, fallback_header: str) -> str | None:
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return request.headers.get(fallback_header)
