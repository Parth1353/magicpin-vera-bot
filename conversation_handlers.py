"""Multi-turn handling — the optional tiebreaker in `challenge-brief.md` §7.4.

    respond(state, merchant_message) -> dict

Given the conversation so far and the merchant's latest message, decide the next move:
`send`, `wait`, or `end`. This is the same machinery the live `/v1/reply` endpoint uses,
exposed as a pure function so it can be driven from a script or a notebook without
standing the server up.

The three behaviours the replay test grades are all decided here:

  * an auto-reply is detected on the first turn and the thread is wound down in three
    moves (flag it for the owner, back off a day, close) rather than burning turns on an
    autoresponder — and the counter is keyed on the *merchant*, so four canned replies
    under four different conversation ids still escalate
  * "ok let's do it" switches from qualifying to executing, with no further questions
  * an explicit stop closes the thread; frustration without a stop gets one apology
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vera_bot.conversation import ConversationStore
from vera_bot.facts import build_fact_sheet
from vera_bot.insights import derive
from vera_bot.replyer import classify, handle_reply
from vera_bot.replyer.handlers import ReplyContext
from vera_bot.utils import parse_iso, utcnow

__all__ = ["ConversationState", "respond", "reset"]

#: One store per process, so repeated calls to `respond` for the same merchant share the
#: memory that auto-reply detection and opt-out handling depend on.
_STORE = ConversationStore()


@dataclass
class ConversationState:
    """Everything `respond` needs to know about a conversation in flight.

    Every field is optional. With no contexts supplied the reply is still correct — it just
    cannot quote the merchant's numbers back at them, so it stays general instead.
    """

    conversation_id: str = "conv_default"
    merchant_id: str = ""
    customer_id: str | None = None
    turns: list[dict] = field(default_factory=list)     # [{"from": "vera"|"merchant", "body": ...}]
    category: dict | None = None
    merchant: dict | None = None
    trigger: dict | None = None
    customer: dict | None = None
    now: datetime | None = None

    @classmethod
    def coerce(cls, value: "ConversationState | dict") -> "ConversationState":
        if isinstance(value, cls):
            return value
        data = dict(value or {})
        # accept the loose shapes a caller might reasonably pass
        data.setdefault("turns", data.pop("history", []) or data.pop("messages", []) or [])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def respond(state: "ConversationState | dict", merchant_message: str) -> dict[str, Any]:
    """Produce the next move in a live conversation.

    Returns one of:
        {"action": "send", "body": str, "cta": str, "rationale": str}
        {"action": "wait", "wait_seconds": int, "rationale": str}
        {"action": "end", "rationale": str}
    """
    state = ConversationState.coerce(state)
    now = state.now or parse_iso(getattr(state, "received_at", None)) or utcnow()

    convo = _STORE.ensure_conversation(state.conversation_id, state.merchant_id,
                                       state.customer_id)
    memory = _STORE.memory(state.merchant_id or state.conversation_id)

    # Replay any prior turns the caller supplied that we have not already recorded.
    for turn in state.turns[len(convo.turns):]:
        who = str(turn.get("from") or turn.get("role") or "merchant").lower()
        text = str(turn.get("body") or turn.get("message") or "")
        if not text:
            continue
        if who in ("vera", "bot", "assistant"):
            convo.record_outbound(text)
        else:
            convo.record_inbound(who, text, classify(text).intent)

    classification = classify(
        merchant_message,
        previous_inbound=[t.body for t in convo.turns if t.role != "vera"],
        merchant_auto_reply_texts=memory.auto_reply_texts)

    context = ReplyContext()
    if state.merchant:
        sheet = build_fact_sheet(state.category, state.merchant, state.trigger or {},
                                 state.customer, now=now)
        context = ReplyContext(sheet=sheet, insights=derive(sheet),
                               merchant_name=sheet.business_name,
                               salutation=sheet.salutation)

    decision = handle_reply(merchant_message, classification, convo, memory, context, now)
    if decision.action == "send" and decision.body:
        convo.record_outbound(decision.body)

    result = decision.as_response()
    result["intent"] = classification.intent
    return result


def reset() -> None:
    """Clear conversation and merchant memory. Mainly for tests and repeated experiments."""
    _STORE.clear()
