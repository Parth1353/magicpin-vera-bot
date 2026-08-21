"""The artifacts challenge-brief.md §7 asks for, at the paths and signatures it names."""

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)

FIVE_KEYS = {"body", "cta", "send_as", "suppression_key", "rationale"}


# ── §7.1 bot.py ────────────────────────────────────────────────────────────
def test_bot_module_exposes_the_documented_signature():
    import bot
    params = list(inspect.signature(bot.compose).parameters)
    assert params[:4] == ["category", "merchant", "trigger", "customer"]


def test_bot_is_runnable_as_uvicorn_bot_app():
    import bot
    assert hasattr(bot, "app"), "challenge-testing-brief.md §7 runs `uvicorn bot:app`"


def test_compose_returns_the_five_documented_keys(categories, merchants, triggers):
    import bot
    trigger = triggers["trg_001_research_digest_dentists"]
    out = bot.compose(categories["dentists"], merchants[trigger["merchant_id"]], trigger,
                      now=NOW)
    assert FIVE_KEYS <= set(out)
    assert out["send_as"] == "vera"
    assert out["body"].strip()


def test_compose_handles_the_optional_customer_argument(categories, merchants, customers,
                                                        triggers):
    import bot
    trigger = triggers["trg_003_recall_due_priya"]
    out = bot.compose(categories["dentists"], merchants[trigger["merchant_id"]], trigger,
                      customers[trigger["customer_id"]], now=NOW)
    assert out["send_as"] == "merchant_on_behalf"
    assert out["customer_id"] == trigger["customer_id"]


def test_compose_is_deterministic(categories, merchants, triggers):
    import bot
    trigger = triggers["trg_001_research_digest_dentists"]
    args = (categories["dentists"], merchants[trigger["merchant_id"]], trigger)
    assert bot.compose(*args, now=NOW) == bot.compose(*args, now=NOW)


def test_compose_returns_empty_rather_than_guessing_with_no_context():
    import bot
    out = bot.compose({}, {}, {}, now=NOW)
    assert isinstance(out, dict)


# ── §7.2 submission.jsonl ──────────────────────────────────────────────────
def test_submission_jsonl_has_thirty_valid_rows():
    rows = [json.loads(line) for line in
            (ROOT / "submission.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == 30
    assert sorted(r["test_id"] for r in rows) == [f"T{i:02d}" for i in range(1, 31)]
    for row in rows:
        assert FIVE_KEYS <= set(row)
        assert row["body"].strip()
        assert "http" not in row["body"].lower()


# ── §7.3 README ────────────────────────────────────────────────────────────
def test_readme_stays_about_one_page():
    words = len((ROOT / "README.md").read_text().split())
    assert words <= 700, f"brief asks for one page; README is {words} words"


def test_readme_carries_the_live_bot_url():
    assert "onrender.com" in (ROOT / "README.md").read_text()


# ── §7.4 conversation_handlers.py (optional tiebreaker) ────────────────────
def test_respond_exposes_the_documented_signature():
    import conversation_handlers as ch
    params = list(inspect.signature(ch.respond).parameters)
    assert params[:2] == ["state", "merchant_message"]


@pytest.fixture()
def handlers():
    import conversation_handlers as ch
    ch.reset()
    yield ch
    ch.reset()


def test_respond_walks_the_auto_reply_ladder(handlers, categories, merchants):
    canned = "Thank you for contacting us! Our team will respond shortly."
    actions = [handlers.respond(
        {"conversation_id": f"c{i}", "merchant_id": "m_001",
         "category": categories["dentists"],
         "merchant": merchants["m_001_drmeera_dentist_delhi"]}, canned)["action"]
        for i in range(3)]
    assert actions == ["send", "wait", "end"]


def test_respond_switches_to_action_on_commitment(handlers, categories, merchants):
    from vera_bot.voice import QUALIFYING_MARKERS
    result = handlers.respond(handlers.ConversationState(
        conversation_id="c_intent", merchant_id="m_001",
        category=categories["dentists"], merchant=merchants["m_001_drmeera_dentist_delhi"],
        turns=[{"from": "vera", "body": "Dr. Meera, one finding this week worth two minutes."}],
    ), "Ok lets do it. Whats next?")
    assert result["action"] == "send"
    body = result["body"].lower()
    assert any(w in body for w in ("draft", "confirm", "sending", "next"))
    assert not any(marker in body for marker in QUALIFYING_MARKERS)


def test_respond_ends_on_an_explicit_stop(handlers):
    result = handlers.respond({"conversation_id": "c_stop", "merchant_id": "m_002"},
                              "Stop messaging me. This is useless spam.")
    assert result["action"] == "end"


def test_respond_works_with_no_context_at_all(handlers):
    result = handlers.respond({"conversation_id": "c_bare"}, "yes please send it")
    assert result["action"] in ("send", "wait", "end")
    assert result["rationale"]


def test_respond_accepts_a_plain_dict_or_a_state_object(handlers):
    a = handlers.respond({"conversation_id": "c_a", "merchant_id": "m_x"}, "thanks")
    b = handlers.respond(handlers.ConversationState(conversation_id="c_b", merchant_id="m_y"),
                         "thanks")
    assert a["action"] and b["action"]
