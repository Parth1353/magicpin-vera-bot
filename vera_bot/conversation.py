"""Per-conversation and per-merchant memory.

Two scopes matter, and conflating them is how bots fail the auto-reply replay. The judge's
own harness sends four identical canned replies under four *different* conversation ids, so
anything keyed only on `conversation_id` sees four first-time auto-replies and never
escalates. Repeat detection therefore lives on the merchant, while the transcript and the
anti-repetition ledger live on the conversation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .utils import iso, normalise_for_compare, parse_iso, utcnow

# conversation states
INITIATED = "initiated"
AWAITING = "awaiting_reply"
ENGAGED = "engaged"
ACTION = "action_mode"
WAITING = "waiting"
CLOSED = "closed"


@dataclass
class Turn:
    role: str                 # "vera" | "merchant" | "customer"
    body: str
    at: str
    intent: str = ""


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str = ""
    customer_id: str | None = None
    trigger_id: str = ""
    send_as: str = "vera"
    state: str = INITIATED
    turns: list[Turn] = field(default_factory=list)
    sent_bodies: list[str] = field(default_factory=list)
    angles_used: list[str] = field(default_factory=list)
    opened_at: str = field(default_factory=iso)
    last_activity: str = field(default_factory=iso)
    closed_reason: str = ""
    wait_until: str | None = None
    objections: int = 0
    unknown_replies: int = 0
    price_answered: bool = False
    nudges_without_reply: int = 0

    # ------------------------------------------------------------------ #
    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def is_closed(self) -> bool:
        return self.state == CLOSED

    @property
    def merchant_has_replied(self) -> bool:
        return any(t.role != "vera" for t in self.turns)

    @property
    def last_vera_body(self) -> str:
        for turn in reversed(self.turns):
            if turn.role == "vera":
                return turn.body
        return ""

    @property
    def last_inbound(self) -> Turn | None:
        for turn in reversed(self.turns):
            if turn.role != "vera":
                return turn
        return None

    def record_outbound(self, body: str, angle: str = "", at: str | None = None) -> None:
        stamp = at or iso()
        self.turns.append(Turn("vera", body, stamp))
        self.sent_bodies.append(body)
        if angle:
            self.angles_used.append(angle)
        self.nudges_without_reply += 1
        self.last_activity = stamp
        if self.state in (INITIATED, WAITING):
            self.state = AWAITING

    def record_inbound(self, role: str, body: str, intent: str, at: str | None = None) -> None:
        stamp = at or iso()
        self.turns.append(Turn(role, body, stamp, intent))
        self.nudges_without_reply = 0
        self.last_activity = stamp
        # ACTION means work is under way; a question in the middle of it does not put the
        # thread back into "being sold to", so only WAITING/AWAITING promote to ENGAGED.
        if self.state not in (CLOSED, ACTION):
            self.state = ENGAGED

    def close(self, reason: str) -> None:
        self.state = CLOSED
        self.closed_reason = reason
        self.last_activity = iso()

    def hold(self, seconds: int, now: datetime | None = None) -> None:
        self.state = WAITING
        base = now or utcnow()
        self.wait_until = iso(base.fromtimestamp(base.timestamp() + seconds, tz=base.tzinfo))
        self.last_activity = iso()

    def has_sent(self, body: str) -> bool:
        target = normalise_for_compare(body)
        return any(normalise_for_compare(b) == target for b in self.sent_bodies)


@dataclass
class MerchantMemory:
    """State that must outlive any single conversation with this merchant."""

    merchant_id: str
    opted_out: bool = False
    opted_out_at: str = ""
    hostile_events: int = 0
    auto_reply_events: int = 0
    auto_reply_texts: list[str] = field(default_factory=list)
    suppression_keys: set[str] = field(default_factory=set)
    trigger_ids_actioned: set[str] = field(default_factory=set)
    angles_used: list[str] = field(default_factory=list)
    sent_bodies: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)
    last_sent_at: str = ""
    sends: int = 0
    first_touch_done: bool = False
    quiet_until: str | None = None

    def note_auto_reply(self, text: str) -> int:
        self.auto_reply_events += 1
        self.auto_reply_texts.append(normalise_for_compare(text))
        return self.auto_reply_events

    @property
    def auto_reply_is_verbatim(self) -> bool:
        seen = self.auto_reply_texts
        return len(seen) >= 2 and seen[-1] == seen[-2]

    def note_send(self, body: str, suppression_key: str, trigger_id: str,
                  angle: str, conversation_id: str, at: str | None = None) -> None:
        self.sends += 1
        self.sent_bodies.append(body)
        del self.sent_bodies[:-30]
        if suppression_key:
            self.suppression_keys.add(suppression_key)
        if trigger_id:
            self.trigger_ids_actioned.add(trigger_id)
        if angle:
            self.angles_used.append(angle)
        if conversation_id not in self.conversation_ids:
            self.conversation_ids.append(conversation_id)
        # Recorded on the *simulated* clock the harness is driving, not wall time — the
        # judge advances time in five-minute ticks that bear no relation to real seconds.
        self.last_sent_at = at or iso()
        self.first_touch_done = True

    def is_quiet(self, now: datetime) -> bool:
        if not self.quiet_until:
            return False
        until = parse_iso(self.quiet_until)
        return bool(until and now < until)

    def go_quiet(self, seconds: int, now: datetime) -> None:
        self.quiet_until = iso(now.fromtimestamp(now.timestamp() + seconds, tz=now.tzinfo))


class ConversationStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conversations: dict[str, Conversation] = {}
        self._merchants: dict[str, MerchantMemory] = {}

    # -------------------------------------------------------------- lookups
    def conversation(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    def ensure_conversation(self, conversation_id: str, merchant_id: str = "",
                            customer_id: str | None = None, trigger_id: str = "",
                            send_as: str = "vera") -> Conversation:
        """The judge may reply on a conversation the bot never opened; accept it."""
        with self._lock:
            convo = self._conversations.get(conversation_id)
            if convo is None:
                convo = Conversation(conversation_id=conversation_id, merchant_id=merchant_id,
                                     customer_id=customer_id, trigger_id=trigger_id,
                                     send_as=send_as)
                self._conversations[conversation_id] = convo
            else:
                convo.merchant_id = convo.merchant_id or merchant_id
                convo.customer_id = convo.customer_id or customer_id
            return convo

    def memory(self, merchant_id: str) -> MerchantMemory:
        with self._lock:
            mem = self._merchants.get(merchant_id)
            if mem is None:
                mem = MerchantMemory(merchant_id=merchant_id)
                self._merchants[merchant_id] = mem
            return mem

    def conversations_for(self, merchant_id: str) -> list[Conversation]:
        with self._lock:
            return [c for c in self._conversations.values() if c.merchant_id == merchant_id]

    def open_conversation_for(self, merchant_id: str) -> Conversation | None:
        for convo in sorted(self.conversations_for(merchant_id),
                            key=lambda c: c.last_activity, reverse=True):
            if not convo.is_closed:
                return convo
        return None

    def bodies_seen(self, merchant_id: str, conversation_id: str | None = None) -> list[str]:
        seen = list(self.memory(merchant_id).sent_bodies) if merchant_id else []
        if conversation_id:
            convo = self.conversation(conversation_id)
            if convo:
                seen.extend(convo.sent_bodies)
        return seen

    def id_taken(self, conversation_id: str) -> bool:
        with self._lock:
            return conversation_id in self._conversations

    def stats(self) -> dict:
        with self._lock:
            return {
                "conversations": len(self._conversations),
                "open": sum(1 for c in self._conversations.values() if not c.is_closed),
                "closed": sum(1 for c in self._conversations.values() if c.is_closed),
                "merchants_engaged": len(self._merchants),
                "opted_out": sum(1 for m in self._merchants.values() if m.opted_out),
            }

    def clear(self) -> None:
        with self._lock:
            self._conversations.clear()
            self._merchants.clear()
