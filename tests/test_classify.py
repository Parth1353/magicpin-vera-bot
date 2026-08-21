import pytest

from vera_bot.replyer import classify


@pytest.mark.parametrize("message,expected", [
    # the three replay scenarios the judge runs, verbatim
    ("Thank you for contacting us! Our team will respond shortly.", "auto_reply"),
    ("Ok lets do it. Whats next?", "commit"),
    ("Stop messaging me. This is useless spam.", "opt_out"),
    # from api-call-examples.md
    ("Not interested. Stop messaging me.", "opt_out"),
    ("Btw can you also help me with my GST filing this month?", "off_topic"),
    ("Yes please send the abstract. Also draft the patient WhatsApp.", "commit"),
    ("Why are you bothering me. This is useless.", "hostile"),
    # the brief's Pattern D intent-handoff failure
    ("Mujhe magicpin judrna hai", "join_intent"),
    ("I want to join magicpin", "join_intent"),
    # everyday merchant replies
    ("haan kar do", "commit"),
    ("How much will this cost?", "question_price"),
    ("Too expensive right now", "objection_price"),
    ("tried this before, no results", "objection_value"),
    ("Call me next week, busy now", "defer"),
    ("Can I speak to a real person?", "human_handoff"),
    ("thanks", "ack"),
    ("no need", "negative"),
    ("", "empty"),
])
def test_intents(message, expected):
    assert classify(message).intent == expected


def test_commitment_beats_the_question_mark_it_ends_with():
    """'Ok lets do it. Whats next?' is a green light, not a question."""
    result = classify("Ok lets do it. Whats next?")
    assert result.intent == "commit"
    assert result.is_question is True


def test_opt_out_beats_hostility():
    assert classify("This is useless spam. Stop messaging me.").intent == "opt_out"


def test_repeated_text_reads_as_an_auto_reply_even_without_canned_phrasing():
    message = "We are looking into your request and will update you in due course"
    assert classify(message).intent != "auto_reply"
    assert classify(message, previous_inbound=[message]).intent == "auto_reply"


@pytest.mark.parametrize("message,hinglish", [
    ("kitna time lagega?", True),
    ("shukriya", True),
    ("haan bhej do", True),
    ("Yes please send it", False),
    ("see you at the sale", False),
])
def test_language_detection_per_turn(message, hinglish):
    assert classify(message).hinglish is hinglish
