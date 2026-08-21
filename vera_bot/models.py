"""Request and response schemas for the five judge-facing endpoints.

Deliberately permissive on input: the harness is the source of truth and a 422 on a field
we could have tolerated costs a scored action. Strict on output, because a missing action
field is an explicit -2 in the scoring rubric.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextPush(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: str
    context_id: str
    version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    delivered_at: str | None = None


class TickRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    now: str | None = None
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str = ""
    received_at: str | None = None
    turn_number: int = 1


class Action(BaseModel):
    """Every field here is required by the harness; omitting one is scored as malformed."""

    conversation_id: str
    merchant_id: str
    customer_id: str | None = None
    send_as: str
    trigger_id: str
    template_name: str
    template_params: list[str]
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: list[Action] = Field(default_factory=list)
