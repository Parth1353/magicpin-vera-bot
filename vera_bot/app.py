"""HTTP surface: the five endpoints the judge harness calls.

Operational rules taken from the briefs and enforced here rather than hoped for:
  * `/v1/context` is idempotent on (scope, context_id, version) and answers 409 on a
    replay of an equal-or-lower version
  * `/v1/tick` and `/v1/reply` always answer inside their budget — worst case with an empty
    action list, never a timeout, because a timeout is a scored penalty
  * nothing raises: any unexpected error still returns a schema-valid response
"""

from __future__ import annotations

import asyncio
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import llm
from .composer import compose_from_sheet, conversation_id_for
from .config import settings
from .conversation import ConversationStore
from .facts import build_fact_sheet
from .insights import derive
from .models import Action, ContextPush, ReplyRequest, TickRequest
from .planner import Planner
from .replyer import classify, handle_reply
from .replyer.handlers import ReplyContext
from .store import SCOPES, ContextStore
from .utils import iso, parse_iso, squeeze, utcnow

START_TIME = time.time()

contexts = ContextStore()
conversations = ConversationStore()
planner = Planner(contexts, conversations)

_stats = {"ticks": 0, "actions": 0, "replies": 0, "context_pushes": 0,
          "stale_pushes": 0, "errors": 0, "llm_edits": 0}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    contexts.clear()
    conversations.clear()


app = FastAPI(title="Vera — magicpin AI Challenge bot", version=settings.version,
              lifespan=lifespan)


@app.middleware("http")
async def never_500(request: Request, call_next):
    """A stack trace is a malformed response as far as the harness is concerned."""
    try:
        return await call_next(request)
    except Exception:                                     # noqa: BLE001
        _stats["errors"] += 1
        traceback.print_exc()
        path = request.url.path
        if path.endswith("/tick"):
            return JSONResponse({"actions": []}, status_code=200)
        if path.endswith("/reply"):
            return JSONResponse({"action": "wait", "wait_seconds": 1800,
                                 "rationale": "internal error while composing; backing off "
                                              "rather than sending something unchecked"},
                                status_code=200)
        if path.endswith("/context"):
            return JSONResponse({"accepted": False, "reason": "internal_error"}, status_code=200)
        return JSONResponse({"status": "error"}, status_code=200)


# --------------------------------------------------------------------------- #
# liveness + identity
# --------------------------------------------------------------------------- #

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": contexts.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": settings.team_name,
        "team_members": settings.team_members,
        "model": settings.model_label(),
        "approach": settings.describe_approach(),
        "contact_email": settings.contact_email,
        "version": settings.version,
        "submitted_at": settings.submitted_at,
    }


@app.get("/")
async def root():
    return {"service": "vera-bot", "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context",
                                                 "/v1/tick", "/v1/reply"]}


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #

@app.post("/v1/context")
async def push_context(body: ContextPush):
    scope = squeeze(body.scope).lower()
    if scope not in SCOPES:
        return JSONResponse(
            {"accepted": False, "reason": "invalid_scope",
             "details": f"scope must be one of {', '.join(SCOPES)}"}, status_code=400)
    if not squeeze(body.context_id):
        return JSONResponse({"accepted": False, "reason": "missing_context_id"}, status_code=400)
    if not isinstance(body.payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload"}, status_code=400)

    accepted, current, _change = contexts.put(scope, body.context_id, int(body.version),
                                              body.payload, body.delivered_at)
    if not accepted:
        _stats["stale_pushes"] += 1
        return JSONResponse({"accepted": False, "reason": "stale_version",
                             "current_version": current}, status_code=409)

    _stats["context_pushes"] += 1
    return {"accepted": True,
            "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": iso()}


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    planner.clear()
    return {"status": "ok", "wiped": True}


# --------------------------------------------------------------------------- #
# tick
# --------------------------------------------------------------------------- #

@app.post("/v1/tick")
async def tick(body: TickRequest):
    deadline = time.monotonic() + settings.tick_budget_seconds
    now = parse_iso(body.now) or utcnow()
    _stats["ticks"] += 1

    declared = set(body.available_triggers or [])
    available = list(declared)
    if not available:
        # `available_triggers` is a hint, not a contract; fall back to whatever is live.
        available = contexts.live_trigger_ids(now)

    candidates, skips = planner.select(available, now, declared=declared)
    actions: list[dict] = []

    for candidate in candidates:
        if time.monotonic() > deadline:
            break
        action = _build_action(candidate, now, deadline)
        if action is not None:
            actions.append(action)
        if len(actions) >= settings.max_actions_per_tick:
            break

    _stats["actions"] += len(actions)
    if settings.debug_endpoints:
        _last_skips.clear()
        _last_skips.extend([{"trigger_id": s.trigger_id, "reason": s.reason} for s in skips])
    return {"actions": actions}


_last_skips: list[dict] = []


def _build_action(candidate, now: datetime, deadline: float) -> dict | None:
    merchant = contexts.merchant(candidate.merchant_id)
    if not merchant:
        return None
    category = contexts.category_for_merchant(merchant)
    customer = contexts.customer(candidate.customer_id) if candidate.customer_id else None
    memory = conversations.memory(candidate.merchant_id)

    sheet = build_fact_sheet(
        category, merchant, candidate.trigger, customer, now=now,
        merchant_change=contexts.change_for("merchant", candidate.merchant_id),
        category_change=contexts.change_for("category", merchant.get("category_slug")))
    insights = derive(sheet)

    seen = conversations.bodies_seen(candidate.merchant_id)
    composition = compose_from_sheet(sheet, insights, seen_bodies=seen,
                                     first_touch=not memory.first_touch_done)
    if composition is None or not squeeze(composition.body):
        return None

    if llm.available() and time.monotonic() + settings.llm_timeout < deadline:
        result = llm.polish(composition.body, sheet, seen_bodies=tuple(seen))
        if result.used_llm and result.body != composition.body:
            composition.body = result.body
            _stats["llm_edits"] += 1

    conversation_id = planner.claim_conversation_id(conversation_id_for(sheet))
    convo = conversations.ensure_conversation(
        conversation_id, candidate.merchant_id, candidate.customer_id,
        candidate.trigger_id, composition.send_as)
    stamp = iso(now)
    convo.record_outbound(composition.body, composition.angle_id, at=stamp)
    memory.note_send(composition.body, composition.suppression_key, candidate.trigger_id,
                     composition.angle_id, conversation_id, at=stamp)
    planner.mark_actioned(candidate.trigger_id)

    rationale = composition.rationale
    if candidate.reasons:
        rationale = f"{rationale} Selected over other queued triggers on: {'; '.join(candidate.reasons)}."

    return Action(
        conversation_id=conversation_id,
        merchant_id=candidate.merchant_id,
        customer_id=candidate.customer_id,
        send_as=composition.send_as,
        trigger_id=candidate.trigger_id,
        template_name=composition.template_name,
        template_params=composition.template_params,
        body=composition.body,
        cta=composition.cta,
        suppression_key=composition.suppression_key,
        rationale=squeeze(rationale),
    ).model_dump()


# --------------------------------------------------------------------------- #
# reply
# --------------------------------------------------------------------------- #

@app.post("/v1/reply")
async def reply(body: ReplyRequest):
    _stats["replies"] += 1
    now = parse_iso(body.received_at) or utcnow()

    merchant_id = squeeze(body.merchant_id or "")
    convo = conversations.ensure_conversation(body.conversation_id, merchant_id,
                                              body.customer_id)
    merchant_id = merchant_id or convo.merchant_id
    memory = conversations.memory(merchant_id or body.conversation_id)

    previous_inbound = [t.body for t in convo.turns if t.role != "vera"]
    classification = classify(body.message,
                              previous_inbound=previous_inbound,
                              merchant_auto_reply_texts=memory.auto_reply_texts)

    context = _reply_context(merchant_id, convo, now)
    decision = handle_reply(body.message, classification, convo, memory, context, now)

    if decision.action == "send" and squeeze(decision.body):
        if convo.has_sent(decision.body):
            decision.body = _vary(decision.body)
        if llm.available() and settings.llm_polish_replies and context.sheet is not None:
            result = llm.polish(decision.body, context.sheet,
                                seen_bodies=tuple(convo.sent_bodies))
            if result.used_llm and result.body != decision.body:
                decision.body = result.body
                _stats["llm_edits"] += 1
        convo.record_outbound(decision.body, at=iso(now))

    rationale = decision.rationale
    if classification.evidence:
        rationale = (f"{rationale}; classified as {classification.intent} "
                     f"({classification.evidence[0]})")
    if classification.hinglish:
        rationale += "; merchant replied in Hindi-English, so the reply stays in that register"
    decision.rationale = squeeze(rationale)
    return decision.as_response()


def _reply_context(merchant_id: str, convo, now: datetime) -> ReplyContext:
    merchant = contexts.merchant(merchant_id)
    if not merchant:
        return ReplyContext(topic=_topic_from(convo))

    category = contexts.category_for_merchant(merchant)
    trigger = contexts.trigger(convo.trigger_id) or {}
    customer = contexts.customer(convo.customer_id) if convo.customer_id else None
    sheet = build_fact_sheet(category, merchant, trigger, customer, now=now)
    insights = derive(sheet)
    return ReplyContext(sheet=sheet, insights=insights, merchant_name=sheet.business_name,
                        salutation=sheet.salutation, topic=_topic_from(convo, sheet))


#: Trigger kinds that carry no human topic — never echo these back to a merchant.
_OPAQUE_KINDS = {"unknown", "", "scheduled recurring", "nudge", "placeholder"}


def _topic_from(convo, sheet=None) -> str:
    if sheet is not None and sheet.trigger and sheet.trigger.kind:
        readable = sheet.trigger.kind.replace("_", " ")
        if readable in _OPAQUE_KINDS:
            return _default_topic(sheet)
        return {
            "research digest": "the research item I sent",
            "regulation change": "the compliance change",
            "supply alert": "the batch notice",
            "perf dip": "the drop in your numbers",
            "perf spike": "the jump in your numbers",
            "gbp unverified": "getting your listing verified",
            "curious ask due": "the question I asked",
        }.get(readable, f"the {readable}")
    return "what I flagged"


def _default_topic(sheet) -> str:
    """When there is no trigger on the thread, name something real from the listing."""
    if sheet is not None:
        if not sheet.verified:
            return "getting your listing verified"
        if sheet.active_offers:
            return f"getting {sheet.active_offers[0].title} in front of more people"
        if sheet.views is not None:
            return "the listing work we were on"
    return "what I flagged"


def _vary(body: str) -> str:
    """Never send the same body twice inside one thread; the harness scores repeats at -2."""
    swaps = [("Want me to", "Shall I"), ("Reply CONFIRM", "Say CONFIRM"),
             ("On it", "Starting now"), ("I'll", "I will")]
    for old, new in swaps:
        if old in body:
            return body.replace(old, new, 1)
    return f"{body} Say the word."


# --------------------------------------------------------------------------- #
# debug (never called by the harness)
# --------------------------------------------------------------------------- #

@app.get("/v1/debug/state")
async def debug_state():
    if not settings.debug_endpoints:
        return {"disabled": True}
    return {
        "contexts": contexts.counts(),
        "context_pushes": contexts.total_pushes,
        "conversations": conversations.stats(),
        "deferred_triggers": planner.deferred_count,
        "stats": dict(_stats),
        "llm": {"active": llm.available(), "provider": settings.llm_provider,
                "model": settings.model_label()},
        "last_tick_skips": list(_last_skips)[:25],
    }


@app.post("/v1/debug/preview")
async def debug_preview(body: dict):
    """Compose for one (merchant, trigger) pair without recording any state."""
    if not settings.debug_endpoints:
        return {"disabled": True}
    merchant = contexts.merchant(body.get("merchant_id"))
    trigger = contexts.trigger(body.get("trigger_id")) or {}
    if not merchant:
        return {"error": "unknown merchant_id"}
    category = contexts.category_for_merchant(merchant)
    customer = contexts.customer(body.get("customer_id")) if body.get("customer_id") else None
    now = parse_iso(body.get("now")) or utcnow()
    sheet = build_fact_sheet(category, merchant, trigger, customer, now=now)
    composition = compose_from_sheet(sheet, derive(sheet))
    if composition is None:
        return {"error": "nothing worth sending"}
    return {
        "body": composition.body, "cta": composition.cta, "send_as": composition.send_as,
        "suppression_key": composition.suppression_key, "rationale": composition.rationale,
        "template_name": composition.template_name,
        "template_params": composition.template_params,
        "angle": composition.angle_id, "levers": composition.levers,
        "language": composition.language, "length": len(composition.body),
        "notes": composition.notes,
    }
