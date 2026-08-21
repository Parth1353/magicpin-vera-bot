from datetime import datetime, timedelta, timezone

from vera_bot.conversation import ConversationStore
from vera_bot.planner import Planner
from vera_bot.store import ContextStore

NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)


def _setup(triggers_payload, merchant_id="m_001"):
    contexts, conversations = ContextStore(), ConversationStore()
    contexts.put("merchant", merchant_id, 1, {"merchant_id": merchant_id,
                                              "category_slug": "dentists", "signals": []})
    for trigger in triggers_payload:
        contexts.put("trigger", trigger["id"], 1, trigger)
    return contexts, conversations, Planner(contexts, conversations)


def _trigger(tid, **kw):
    base = {"id": tid, "merchant_id": "m_001", "customer_id": None, "kind": "perf_dip",
            "scope": "merchant", "source": "internal", "urgency": 3, "payload": {},
            "suppression_key": f"k_{tid}", "expires_at": "2026-12-31T00:00:00Z"}
    base.update(kw)
    return base


def test_one_thread_per_merchant_per_tick_and_the_rest_are_deferred():
    contexts, conversations, planner = _setup([_trigger("t1"), _trigger("t2")])
    chosen, skips = planner.select(["t1", "t2"], NOW)
    assert len(chosen) == 1
    assert planner.deferred_count == 1
    assert any("another thread" in s.reason for s in skips)

    planner.mark_actioned(chosen[0].trigger_id)
    chosen_again, _ = planner.select([], NOW)
    assert len(chosen_again) == 1, "the deferred trigger must come back, not be lost"


def test_higher_urgency_wins():
    contexts, conversations, planner = _setup(
        [_trigger("low", urgency=1), _trigger("high", urgency=5)])
    chosen, _ = planner.select(["low", "high"], NOW)
    assert chosen[0].trigger_id == "high"


def test_suppression_key_already_used_blocks_the_send():
    contexts, conversations, planner = _setup([_trigger("t1")])
    conversations.memory("m_001").suppression_keys.add("k_t1")
    chosen, skips = planner.select(["t1"], NOW)
    assert not chosen
    assert any("suppression key" in s.reason for s in skips)


def test_opted_out_merchants_are_never_messaged():
    contexts, conversations, planner = _setup([_trigger("t1")])
    conversations.memory("m_001").opted_out = True
    chosen, skips = planner.select(["t1"], NOW)
    assert not chosen and any("opted out" in s.reason for s in skips)


def test_an_expired_trigger_the_harness_calls_active_is_still_actioned():
    """`available_triggers` is the judge stating what is live; that outranks fixture dates."""
    contexts, conversations, planner = _setup(
        [_trigger("stale", expires_at="2020-01-01T00:00:00Z")])
    chosen, _ = planner.select(["stale"], NOW, declared={"stale"})
    assert len(chosen) == 1
    assert any("expiry" in r for r in chosen[0].reasons)


def test_an_expired_trigger_we_picked_ourselves_is_dropped():
    contexts, conversations, planner = _setup(
        [_trigger("stale", expires_at="2020-01-01T00:00:00Z")])
    chosen, skips = planner.select(["stale"], NOW, declared=set())
    assert not chosen and any("expired" in s.reason for s in skips)


def test_three_unanswered_nudges_stop_the_thread():
    contexts, conversations, planner = _setup([_trigger("t1")])
    convo = conversations.ensure_conversation("c1", "m_001")
    convo.nudges_without_reply = 3
    chosen, skips = planner.select(["t1"], NOW)
    assert not chosen and any("unanswered" in s.reason for s in skips)


def test_customer_trigger_without_customer_context_is_skipped():
    contexts, conversations, planner = _setup(
        [_trigger("t1", scope="customer", customer_id="c_999")])
    chosen, skips = planner.select(["t1"], NOW)
    assert not chosen and any("no context pushed" in s.reason for s in skips)


def test_conversation_ids_never_collide():
    contexts, conversations, planner = _setup([_trigger("t1")])
    first = planner.claim_conversation_id("conv_x")
    second = planner.claim_conversation_id("conv_x")
    assert first != second


def test_a_clock_that_runs_backwards_does_not_gate_sends():
    """The harness drives a simulated clock; a negative gap is an artefact, not cadence."""
    contexts, conversations, planner = _setup([_trigger("t1")])
    future = (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    conversations.memory("m_001").last_sent_at = future
    chosen, _ = planner.select(["t1"], NOW)
    assert len(chosen) == 1
