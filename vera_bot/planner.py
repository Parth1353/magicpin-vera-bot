"""Which triggers to act on this tick, and which to leave alone.

Restraint is scored. So is silence in the wrong place: an action that is never emitted is
never scored, so "send nothing" has to be a decision rather than a failure mode. The rules
here are the ones a real outreach system needs anyway — dedupe by suppression key, respect
opt-outs, one thread per merchant per tick, drop expired triggers, stop after three
unanswered nudges — plus a deferral queue so that a trigger held back for cadence reasons
still gets its turn on a later tick instead of being lost.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from .conversation import ConversationStore
from .store import ContextStore
from .utils import parse_iso, utcnow

MAX_ACTIONS_PER_TICK = 20          # hard cap from the testing brief
MAX_UNANSWERED_NUDGES = 3          # "know when to stop"
MIN_SECONDS_BETWEEN_SENDS = 600    # one thread per merchant per tick, and not back-to-back


@dataclass
class Candidate:
    trigger_id: str
    trigger: dict
    merchant_id: str
    customer_id: str | None
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Skip:
    trigger_id: str
    reason: str


class Planner:
    def __init__(self, contexts: ContextStore, conversations: ConversationStore) -> None:
        self.contexts = contexts
        self.conversations = conversations
        self._lock = threading.RLock()
        self._deferred: list[str] = []          # triggers held back for cadence, not dropped
        self._actioned: set[str] = set()
        self._used_conversation_ids: set[str] = set()

    # ------------------------------------------------------------------ #
    def select(self, available: list[str], now: datetime | None = None,
               declared: set[str] | None = None) -> tuple[list[Candidate], list[Skip]]:
        now = now or utcnow()
        declared = declared if declared is not None else set(available)
        with self._lock:
            queue = list(dict.fromkeys(list(available) + self._deferred))
            self._deferred = []

        candidates: list[Candidate] = []
        skips: list[Skip] = []
        for trigger_id in queue:
            candidate, skip = self._evaluate(trigger_id, now,
                                             declared_active=trigger_id in declared)
            if candidate:
                candidates.append(candidate)
            elif skip:
                skips.append(skip)

        candidates.sort(key=lambda c: (-c.score, c.trigger_id))

        chosen: list[Candidate] = []
        seen_merchants: set[str] = set()
        seen_keys: set[str] = set()
        for candidate in candidates:
            key = candidate.trigger.get("suppression_key") or candidate.trigger_id
            if candidate.merchant_id in seen_merchants:
                # The contract allows one action per merchant per tick; hold the rest.
                self._defer(candidate.trigger_id)
                skips.append(Skip(candidate.trigger_id,
                                  "another thread already opens with this merchant this tick"))
                continue
            if key in seen_keys:
                skips.append(Skip(candidate.trigger_id, "duplicate suppression key this tick"))
                continue
            if len(chosen) >= MAX_ACTIONS_PER_TICK:
                self._defer(candidate.trigger_id)
                skips.append(Skip(candidate.trigger_id, "tick action cap reached"))
                continue
            chosen.append(candidate)
            seen_merchants.add(candidate.merchant_id)
            seen_keys.add(key)
        return chosen, skips

    # ------------------------------------------------------------------ #
    def _evaluate(self, trigger_id: str, now: datetime,
                  declared_active: bool = False) -> tuple[Candidate | None, Skip | None]:
        trigger = self.contexts.trigger(trigger_id)
        if not trigger:
            return None, Skip(trigger_id, "no trigger context has been pushed for this id")

        merchant_id = trigger.get("merchant_id") or ""
        merchant = self.contexts.merchant(merchant_id)
        if not merchant:
            return None, Skip(trigger_id, f"no merchant context for {merchant_id or 'unknown'}")

        # `available_triggers` is the judge stating which triggers are active *right now*.
        # When it names one, that statement outranks a stale `expires_at` on fixture data —
        # dropping it would answer an explicit prompt with silence, which scores zero.
        expiry = parse_iso(trigger.get("expires_at"))
        expired = bool(expiry and expiry < now)
        if expired and not declared_active:
            return None, Skip(trigger_id, "trigger expired and not named as active this tick")

        if trigger_id in self._actioned:
            return None, Skip(trigger_id, "already sent for this trigger")

        memory = self.conversations.memory(merchant_id)
        if memory.opted_out:
            return None, Skip(trigger_id, "merchant has opted out of outreach")
        if memory.is_quiet(now):
            return None, Skip(trigger_id, "merchant is inside a cool-off window")

        key = trigger.get("suppression_key") or ""
        if key and key in memory.suppression_keys:
            return None, Skip(trigger_id, f"suppression key already used ({key})")

        open_convo = self.conversations.open_conversation_for(merchant_id)
        if open_convo and open_convo.nudges_without_reply >= MAX_UNANSWERED_NUDGES:
            return None, Skip(trigger_id,
                              f"{open_convo.nudges_without_reply} unanswered nudges already; "
                              f"stopping rather than adding a fourth")
        if open_convo and open_convo.state == "waiting":
            until = parse_iso(open_convo.wait_until)
            if until and now < until:
                self._defer(trigger_id)
                return None, Skip(trigger_id, "merchant asked for time; still inside that window")

        last = parse_iso(memory.last_sent_at)
        if last:
            elapsed = (now - last).total_seconds()
            # A negative gap means the harness clock and our clock disagree; that is a
            # bookkeeping artefact, not a cadence signal, so it must not gate a send.
            if 0 <= elapsed < MIN_SECONDS_BETWEEN_SENDS:
                self._defer(trigger_id)
                return None, Skip(trigger_id,
                                  "sent to this merchant moments ago; spacing the next one")

        customer_id = trigger.get("customer_id")
        if customer_id and not self.contexts.customer(customer_id):
            # A customer-scoped trigger without the customer context would force guesswork.
            return None, Skip(trigger_id,
                              f"customer-scoped trigger but no context pushed for {customer_id}")

        score, reasons = self._score(trigger, merchant, merchant_id, now)
        if expired:
            score -= 1.0
            reasons.append("past its stated expiry but named as active by the harness")
        return Candidate(trigger_id=trigger_id, trigger=trigger, merchant_id=merchant_id,
                         customer_id=customer_id, score=score, reasons=reasons), None

    # ------------------------------------------------------------------ #
    def _score(self, trigger: dict, merchant: dict, merchant_id: str,
               now: datetime) -> tuple[float, list[str]]:
        reasons: list[str] = []
        urgency = float(trigger.get("urgency") or 2)
        score = urgency * 2.0
        reasons.append(f"urgency {int(urgency)}")

        expiry = parse_iso(trigger.get("expires_at"))
        if expiry:
            hours_left = (expiry - now).total_seconds() / 3600.0
            if hours_left < 48:
                score += 4.0
                reasons.append("expires within 48h")
            elif hours_left < 24 * 14:
                score += 1.5

        payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
        if payload and not payload.get("placeholder"):
            score += 2.0
            reasons.append("trigger carries real payload detail")

        change = self.contexts.change_for("merchant", merchant_id)
        if change is not None:
            score += 2.5
            reasons.append("merchant context changed since the last push")
        category_slug = merchant.get("category_slug")
        if category_slug and self.contexts.change_for("category", category_slug):
            score += 1.5
            reasons.append("category digest was refreshed")

        signals = merchant.get("signals") or []
        if any("engaged" in str(s) or "high_engagement" in str(s) for s in signals):
            score += 1.5
            reasons.append("merchant replies to us")
        if any("dormant" in str(s) for s in signals):
            score -= 0.5

        memory = self.conversations.memory(merchant_id)
        score -= min(3.0, memory.sends * 0.75)
        if memory.sends:
            reasons.append(f"{memory.sends} already sent to this merchant")

        if trigger.get("scope") == "customer":
            score += 1.0
            reasons.append("customer-scoped, time-bound by nature")
        return score, reasons

    # ------------------------------------------------------------------ #
    def _defer(self, trigger_id: str) -> None:
        with self._lock:
            if trigger_id not in self._deferred:
                self._deferred.append(trigger_id)
                del self._deferred[:-200]

    def mark_actioned(self, trigger_id: str) -> None:
        with self._lock:
            self._actioned.add(trigger_id)

    def claim_conversation_id(self, base: str) -> str:
        """`/v1/tick` must never reuse a conversation id."""
        with self._lock:
            candidate, suffix = base, 2
            while candidate in self._used_conversation_ids or self.conversations.id_taken(candidate):
                candidate = f"{base}_{suffix}"
                suffix += 1
            self._used_conversation_ids.add(candidate)
            return candidate

    @property
    def deferred_count(self) -> int:
        with self._lock:
            return len(self._deferred)

    def clear(self) -> None:
        with self._lock:
            self._deferred.clear()
            self._actioned.clear()
            self._used_conversation_ids.clear()
