"""Top-level entry point for the magicpin AI Challenge.

`challenge-brief.md` §7.1 asks for a `bot.py` exposing

    compose(category, merchant, trigger, customer=None) -> dict

and `challenge-testing-brief.md` §7 shows a reference skeleton run as `uvicorn bot:app`.
Both are provided here so the module can be used either way:

    python -c "import bot, json; print(bot.compose(cat, mer, trg)['body'])"
    uvicorn bot:app --host 0.0.0.0 --port 8080

The implementation lives in the `vera_bot` package; this module is the documented surface
over it, not a second copy of the logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vera_bot.app import app                      # noqa: F401  (`uvicorn bot:app`)
from vera_bot.composer import compose as _compose
from vera_bot.composer import conversation_id_for as _conversation_id_for
from vera_bot.facts import build_fact_sheet
from vera_bot.insights import derive

__all__ = ["compose", "app"]


def compose(category: dict, merchant: dict, trigger: dict,
            customer: dict | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Compose the next message from the four contexts.

    Args:
        category: a CategoryContext dict, as loaded from `dataset/categories/<slug>.json`.
        merchant: a MerchantContext dict.
        trigger:  a TriggerContext dict — the reason this message goes now.
        customer: a CustomerContext dict when the message is sent on the merchant's behalf
                  to one of their own customers; None for merchant-facing messages.
        now:      optional clock override; defaults to the current time. Supplying it makes
                  the output reproducible across runs, which is how the tests pin it.

    Returns:
        A dict with the five keys the brief specifies — `body`, `cta`, `send_as`,
        `suppression_key`, `rationale` — plus the extras the HTTP contract also wants
        (`template_name`, `template_params`, `conversation_id`) and a little provenance
        (`angle`, `levers`, `language`) that makes a decision auditable after the fact.

        Returns an empty dict when there is nothing worth sending: restraint is a valid
        answer, and the caller should treat that as "skip", not as a failure.

    Deterministic: the same four contexts and the same `now` always produce the same
    message. No model is called on this path.
    """
    result = _compose(category, merchant, trigger, customer, now=now)
    if result is None:
        return {}

    sheet = build_fact_sheet(category, merchant, trigger, customer, now=now)
    return {
        # the five keys challenge-brief.md §5 names
        "body": result.body,
        "cta": result.cta,
        "send_as": result.send_as,
        "suppression_key": result.suppression_key,
        "rationale": result.rationale,
        # what the HTTP action shape additionally requires
        "conversation_id": _conversation_id_for(sheet),
        "template_name": result.template_name,
        "template_params": result.template_params,
        "merchant_id": merchant.get("merchant_id"),
        "customer_id": (customer or {}).get("customer_id"),
        "trigger_id": trigger.get("id"),
        # provenance, so a reviewer can see why this angle was chosen
        "angle": result.angle_id,
        "levers": result.levers,
        "language": result.language,
    }
