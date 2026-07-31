"""
Request enrichment and policy injection.

Runs after routing/authorization (app.routing) and before provider dispatch.
Two things can happen here:
  - the request can be rejected outright (pre-provider content filter)
  - the request can be rewritten (system prompt injection)
It also provides the disclaimer-append step applied to the response, on
both the non-streaming and streaming paths.

Explicitly NOT in scope: calling out to a real moderation model/API. The
content filter here is a simple, synchronous, config-driven keyword check -
a real moderation provider integration is a natural later extension of
`enforce_content_filter`, not something this step needs to build.
"""
from __future__ import annotations

from app.config import TeamConfig
from app.schemas import ChatCompletionRequest, ChatMessage


class PolicyError(Exception):
    """Raised when a request is rejected by policy. Carries an HTTP status code."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def enforce_content_filter(team: TeamConfig, request: ChatCompletionRequest) -> None:
    """Reject the request if any message matches a banned keyword for this team."""
    banned = team.policy.banned_keywords
    if not banned:
        return

    for message in request.messages:
        lowered = message.content.lower()
        for keyword in banned:
            if keyword.lower() in lowered:
                raise PolicyError(
                    f"Request rejected by content filter for team '{team.name}'",
                    status_code=400,
                )


def inject_system_prompt(
    team: TeamConfig, request: ChatCompletionRequest
) -> ChatCompletionRequest:
    """Return a new request with the team's configured system prompt merged in.

    If the request already has a system message, the team's policy prompt is
    prepended to it (policy takes precedence but doesn't discard
    caller-provided system instructions). Otherwise one is added at the front.
    """
    system_prompt = team.policy.system_prompt
    if not system_prompt:
        return request

    messages = list(request.messages)
    existing_index = next(
        (i for i, m in enumerate(messages) if m.role == "system"), None
    )

    if existing_index is not None:
        existing = messages[existing_index]
        messages[existing_index] = ChatMessage(
            role="system", content=f"{system_prompt}\n\n{existing.content}"
        )
    else:
        messages.insert(0, ChatMessage(role="system", content=system_prompt))

    return request.model_copy(update={"messages": messages})


def apply_request_policy(
    team: TeamConfig, request: ChatCompletionRequest
) -> ChatCompletionRequest:
    """Full pre-provider policy pipeline: filter first, then enrich."""
    enforce_content_filter(team, request)
    return inject_system_prompt(team, request)


def append_disclaimer(team: TeamConfig, content: str) -> str:
    disclaimer = team.policy.disclaimer
    if not disclaimer:
        return content
    return f"{content}\n\n{disclaimer}"
