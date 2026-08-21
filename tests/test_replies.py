"""The three Phase 4 replay scenarios, plus the conversation rules around them."""

import pytest

from vera_bot.conversation import ACTION, ConversationStore
from vera_bot.replyer import classify, handle_reply
from vera_bot.replyer.handlers import ReplyContext
from vera_bot.voice import QUALIFYING_MARKERS

AUTO_REPLY = "Thank you for contacting us! Our team will respond shortly."
# judge_simulator.py's own pass condition for the intent-transition scenario
ACTIONING = ("done", "sending", "draft", "here", "confirm", "proceed", "next")


@pytest.fixture()
def store():
    return ConversationStore()


def _reply(store, conversation_id, merchant_id, message, ctx=None):
    convo = store.ensure_conversation(conversation_id, merchant_id)
    memory = store.memory(merchant_id)
    result = classify(message, previous_inbound=[t.body for t in convo.turns if t.role != "vera"],
                      merchant_auto_reply_texts=memory.auto_reply_texts)
    return handle_reply(message, result, convo, memory, ctx or ReplyContext(salutation="Dr. Meera"))


def test_auto_reply_ladder_is_keyed_on_the_merchant_not_the_conversation(store):
    """The harness sends four canned replies under four different conversation ids."""
    actions = [_reply(store, f"conv_auto_{i}", "m_001", AUTO_REPLY).action
               for i in range(1, 5)]
    assert actions[0] == "send"     # flag it once for the owner
    assert actions[1] == "wait"     # back off a day
    assert actions[2] == "end"      # no human on the line
    assert actions[3] == "end"


def test_auto_reply_backoff_is_a_full_day(store):
    _reply(store, "c1", "m_001", AUTO_REPLY)
    decision = _reply(store, "c2", "m_001", AUTO_REPLY)
    assert decision.wait_seconds == 86_400


def test_intent_transition_switches_to_action_and_stops_qualifying(store):
    decision = _reply(store, "conv_intent_1", "m_001", "Ok lets do it. Whats next?")
    assert decision.action == "send"
    body = decision.body.lower()
    assert any(word in body for word in ACTIONING)
    assert not any(marker in body for marker in QUALIFYING_MARKERS)


def test_join_intent_routes_to_action_not_qualification(store):
    decision = _reply(store, "c", "m_001", "Mujhe magicpin judrna hai")
    body = decision.body.lower()
    assert decision.action == "send"
    assert not any(marker in body for marker in QUALIFYING_MARKERS)


def test_hostile_with_stop_request_ends(store):
    decision = _reply(store, "conv_hostile", "m_001", "Stop messaging me. This is useless spam.")
    assert decision.action == "end"
    assert store.memory("m_001").opted_out is True


def test_hostility_without_a_stop_request_gets_one_apology(store):
    decision = _reply(store, "c", "m_001", "Why are you bothering me. This is useless.")
    assert decision.action == "send"
    assert any(word in decision.body.lower() for word in ("sorry", "apolog", "won't"))


def test_off_topic_stays_on_mission(store):
    ctx = ReplyContext(salutation="Dr. Meera", topic="the JIDA item")
    decision = _reply(store, "c", "m_001", "Btw can you also help me with my GST filing?", ctx)
    assert decision.action == "send"
    assert "ca" in decision.body.lower()
    assert "jida" in decision.body.lower()


def test_opt_out_is_honoured_on_later_turns(store):
    _reply(store, "c", "m_001", "Stop messaging me")
    assert _reply(store, "c2", "m_001", "what were you saying").action == "end"


def test_deferral_maps_to_the_window_the_merchant_asked_for(store):
    assert _reply(store, "a", "m_001", "call me next week").wait_seconds == 604_800
    assert _reply(store, "b", "m_002", "tomorrow please").wait_seconds == 86_400


def test_a_second_commitment_advances_rather_than_restarting(store):
    first = _reply(store, "c", "m_001", "yes please send it")
    second = _reply(store, "c", "m_001", "go ahead")
    assert first.body != second.body
    assert store.conversation("c").state == ACTION


def test_unknown_replies_wind_down_instead_of_looping(store):
    actions = [_reply(store, "c", "m_001", text).action
               for text in ("the blue one mostly", "purple sometimes", "green")]
    assert actions == ["send", "wait", "end"]
