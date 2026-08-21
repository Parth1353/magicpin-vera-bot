import pytest

from vera_bot import guard
from vera_bot.guard import CASE_STUDY_BODIES


def test_urls_are_stripped():
    cleaned, changed = guard.strip_urls("Read more: https://magicpin.com/blog today")
    assert changed and "http" not in cleaned and "magicpin.com" not in cleaned


def test_internal_jargon_is_detected_and_softened():
    body = "Your ctr_below_peer_median flag says m_001 needs a suppression key"
    assert guard.find_jargon(body)
    assert "_" not in guard.soften_jargon(body)


def test_category_taboos_block():
    report = guard.check("This is guaranteed to work for you. Want it?",
                         taboos=["guaranteed", "miracle"])
    assert not report.ok
    assert any("taboo" in b for b in report.blocking)


def test_placeholder_value_blocks():
    report = guard.check("We can hold a None slot for you. Want it?", taboos=[])
    assert not report.ok
    assert any("placeholder" in b for b in report.blocking)


@pytest.mark.parametrize("body,expected", [
    ("3 FREE Trial Classes is live right now", False),
    ("a trial of 2,100 patients shows 38% lower recurrence", True),
    ("the DCI circular revises the dose limit", True),
    ("your calls are down 50% this week", False),
])
def test_citation_is_required_only_for_real_claims(body, expected):
    assert guard.needs_citation(body) is expected


def test_ask_counting_is_per_sentence():
    assert guard.count_asks("Want me to draft it?") == 1
    assert guard.count_asks("Correct me in one line and I'll build it.") == 1
    assert guard.count_asks("Want A? Reply YES for B?") == 2
    assert guard.count_asks("Here are the numbers.") == 0


def test_plagiarism_flags_case_studies_but_not_shared_facts():
    for body in CASE_STUDY_BODIES:
        assert guard.plagiarism_score(body) >= guard.PLAGIARISM_THRESHOLD
    original = ("Dr. Sameer, views at Bright Smile are down 20% while calls are up 16% — "
                "the traffic is better, not worse.")
    assert guard.plagiarism_score(original) < guard.PLAGIARISM_THRESHOLD


def test_repetition_blocks():
    body = "Dr. Meera, your calls are down. Want me to look?"
    assert not guard.check(body, seen_bodies=[body]).ok
