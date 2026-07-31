"""
Team API key extraction middleware.

Extract the caller's API key, resolve it to a team from config, and attach
it to request.state.team. Requests with a missing or unknown key are
rejected with 401.

/admin/* is exempt here entirely - it uses a completely separate identity
space and auth dependency (app.routers.admin.require_admin), since an admin
key is not a team key and shouldn't authenticate to either surface. This is
also why /admin/* isn't handled by attaching something to request.state
here: mixing the two auth schemes into one middleware would make it easy to
accidentally accept a team key on an admin route or vice versa.

Explicitly NOT in scope here: rate limiting, budgets, quotas - those are
enforced later in the request lifecycle (see app/routers/chat.py), not in
this middleware.

Metrics note (Step 14): rejections here happen before FastAPI routes the
request to app/routers/chat.py at all, so its own metrics/tracing
instrumentation never runs for them - a missing/invalid API key would
otherwise be an invisible gap in "error rate by team/model/provider/error
type". Recorded directly here instead, with team/model as "unknown" (both
are genuinely unresolvable at this layer) so the error_type ("unauthorized")
still shows up in gateway_errors_total.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import get_config
from app.http_auth import extract_api_key
from app.metrics import ERRORS_TOTAL, REQUESTS_TOTAL

# Exact paths that don't require a team API key.
EXEMPT_PATHS = {
    "/health", "/health/providers", "/health/circuit-breakers", "/metrics",
    "/docs", "/openapi.json", "/redoc",
}
# Path prefixes that don't require a team API key (separate auth schemes).
EXEMPT_PREFIXES = ("/admin",)


def _record_auth_rejection() -> None:
    REQUESTS_TOTAL.labels(team="unknown", model="unknown", provider="n/a").inc()
    ERRORS_TOTAL.labels(
        team="unknown", model="unknown", provider="n/a", error_type="unauthorized"
    ).inc()


class TeamAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        api_key = extract_api_key(request, fallback_header="x-api-key")
        if not api_key:
            _record_auth_rejection()
            return JSONResponse(
                status_code=401,
                content={"error": "Missing API key"},
            )

        team = get_config().get_team_by_api_key(api_key)
        if team is None:
            _record_auth_rejection()
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid API key"},
            )

        request.state.team = team
        return await call_next(request)
