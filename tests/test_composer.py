"""Invariants that must hold for every message the bot can produce.

These run over the whole expanded dataset — including the sparse merchants and placeholder
triggers that make up most of it — because the judge's canonical pairs draw from exactly
that population.
"""

import re

import pytest

from vera_bot import guard
from vera_bot.composer import compose, compose_from_sheet, conversation_id_for
from vera_bot.facts import build_fact_sheet
from vera_bot.insights import derive

NUMBER = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")
PLACEHOLDER = re.compile(r"(?<![A-Za-z])(?:None|null|undefined|nan|placeholder)(?![A-Za-z])")


def _category_for(categories, merchant):
    slug = merchant.get("category_slug")
    if slug in categories:
        return categories[slug]
    mid = merchant.get("merchant_id", "").lower()
    return next((c for name, c in categories.items() if name.rstrip("s") in mid), {})


def _all_cases(categories, expanded):
    for trigger_id, trigger in sorted(expanded["triggers"].items()):
        merchant = expanded["merchants"].get(trigger.get("merchant_id"))
        if not merchant:
            continue
        customer = expanded["customers"].get(trigger.get("customer_id"))
        yield trigger_id, _category_for(categories, merchant), merchant, trigger, customer


@pytest.fixture(scope="module")
def compositions(categories, expanded, now):
    out = []
    for trigger_id, category, merchant, trigger, customer in _all_cases(categories, expanded):
        sheet = build_fact_sheet(category, merchant, trigger, customer, now=now)
        composition = compose_from_sheet(sheet, derive(sheet))
        if composition is not None:
            out.append((trigger_id, sheet, composition))
    return out


def test_every_trigger_produces_a_message(categories, expanded, compositions):
    total = sum(1 for _ in _all_cases(categories, expanded))
    assert len(compositions) == total, "the composer silently declined some triggers"
    assert total >= 90


def test_no_placeholder_values_reach_a_merchant(compositions):
    offenders = [(tid, c.body) for tid, _, c in compositions if PLACEHOLDER.search(c.body)]
    assert not offenders, offenders[:3]


def test_no_urls(compositions):
    assert not [tid for tid, _, c in compositions if re.search(r"https?://|www\.", c.body)]


def test_exactly_one_ask_per_message(compositions):
    offenders = [(tid, guard.count_asks(c.body), c.body)
                 for tid, _, c in compositions
                 if c.cta != "none" and guard.count_asks(c.body) != 1]
    assert not offenders, offenders[:3]


def test_every_number_traces_back_to_a_context(compositions):
    offenders = [(tid, sheet.ledger.unknown_numbers(c.body))
                 for tid, sheet, c in compositions if sheet.ledger.unknown_numbers(c.body)]
    assert not offenders, offenders[:3]


def test_specificity_floor_of_two_checkable_numbers(compositions):
    thin = [(tid, c.body) for tid, _, c in compositions
            if len(set(NUMBER.findall(c.body))) < 2]
    assert not thin, thin[:3]


def test_no_category_taboo_language(compositions):
    offenders = [(tid, guard.find_taboos(c.body, sheet.taboos))
                 for tid, sheet, c in compositions if guard.find_taboos(c.body, sheet.taboos)]
    assert not offenders, offenders[:3]


def test_no_internal_jargon(compositions):
    offenders = [(tid, guard.find_jargon(c.body, sheet.vocab))
                 for tid, sheet, c in compositions if guard.find_jargon(c.body, sheet.vocab)]
    assert not offenders, offenders[:3]


def test_nothing_reads_like_a_published_case_study(compositions):
    offenders = [(tid, round(score, 2)) for tid, sheet, c in compositions
                 if (score := guard.plagiarism_score(c.body, guard.context_vocabulary(sheet)))
                 >= guard.PLAGIARISM_THRESHOLD]
    assert not offenders, offenders[:3]


def test_research_and_compliance_claims_carry_a_source(compositions):
    offenders = [(tid, c.body) for tid, _, c in compositions
                 if guard.needs_citation(c.body) and not guard.has_citation(c.body)]
    assert not offenders, offenders[:3]


def test_customer_scoped_triggers_send_on_behalf_of_the_merchant(compositions):
    for tid, sheet, composition in compositions:
        if sheet.customer and sheet.trigger and sheet.trigger.scope == "customer":
            assert composition.send_as == "merchant_on_behalf", tid
        elif not sheet.customer:
            assert composition.send_as == "vera", tid


def test_merchant_or_owner_is_named(compositions):
    misses = []
    for tid, sheet, composition in compositions:
        names = [n.lower() for n in (sheet.salutation, sheet.business_name, sheet.owner_name) if n]
        if names and not any(n in composition.body.lower() for n in names):
            misses.append(tid)
    assert not misses, misses[:3]


def test_bodies_stay_readable_on_a_phone(compositions):
    lengths = [len(c.body) for _, _, c in compositions]
    assert max(lengths) <= 560, f"longest is {max(lengths)}"
    assert sum(lengths) / len(lengths) < 460


def test_composition_is_deterministic(categories, merchants, triggers, now):
    trigger = triggers["trg_001_research_digest_dentists"]
    merchant = merchants[trigger["merchant_id"]]
    first = compose(categories["dentists"], merchant, trigger, None, now=now)
    second = compose(categories["dentists"], merchant, trigger, None, now=now)
    assert first.body == second.body
    assert first.rationale == second.rationale
    assert first.suppression_key == second.suppression_key


def test_conversation_ids_are_decodable(categories, merchants, triggers, now):
    trigger = triggers["trg_003_recall_due_priya"]
    sheet = build_fact_sheet(categories["dentists"], merchants[trigger["merchant_id"]],
                             trigger, None, now=now)
    conversation_id = conversation_id_for(sheet)
    assert conversation_id.startswith("conv_")
    assert "recalldue" in conversation_id


def test_language_preference_is_honoured_for_customers(categories, merchants, customers,
                                                       triggers, now):
    trigger = triggers["trg_003_recall_due_priya"]           # Priya is "hi-en mix"
    composition = compose(categories["dentists"], merchants[trigger["merchant_id"]], trigger,
                          customers[trigger["customer_id"]], now=now)
    assert composition.language == "Hindi-English mix"
    assert re.search(r"\b(ya|hai|aapke|jaisa|khaali|dono)\b", composition.body)


def test_english_only_customer_gets_english(categories, merchants, customers, triggers, now):
    trigger = triggers["trg_015_winback_rashmi"]             # Rashmi is "english"
    composition = compose(categories["gyms"], merchants[trigger["merchant_id"]], trigger,
                          customers[trigger["customer_id"]], now=now)
    assert composition.language == "English"


def test_a_festival_half_a_year_out_is_declined_not_forwarded(categories, merchants,
                                                              triggers, now):
    """Diwali is 188 days away in the dataset; sending a Diwali nudge today is the wrong call."""
    trigger = triggers["trg_006_festival_diwali"]
    composition = compose(categories["salons"], merchants[trigger["merchant_id"]], trigger,
                          None, now=now)
    assert "not a Diwali message" in composition.body
    assert "188" in composition.body


def test_new_context_is_cited_when_the_numbers_move(categories, merchants, triggers, now):
    from vera_bot.store import ContextStore
    store = ContextStore()
    merchant = merchants["m_001_drmeera_dentist_delhi"]
    store.put("merchant", merchant["merchant_id"], 1, merchant)
    updated = {**merchant, "performance": {**merchant["performance"], "views": 3100, "calls": 44}}
    _, _, change = store.put("merchant", merchant["merchant_id"], 2, updated)
    composition = compose(categories["dentists"], updated,
                          triggers["trg_004_perf_dip_bharat"], None, now=now,
                          merchant_change=change)
    assert "3,100" in composition.body or "44" in composition.body


def test_a_citation_is_always_backed_by_a_figure_from_that_source(compositions):
    """A source line exists to back a number. No number from it in the body, no citation."""
    import re as _re
    pattern = _re.compile(r"\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?")
    offenders = []
    for tid, sheet, composition in compositions:
        match = _re.search(r"—\s*([^—]+)$", composition.body)
        if not match:
            continue
        source = match.group(1).strip()
        item = next((d for d in sheet.digest if d.source == source), None)
        if item is None:
            continue
        if not guard.shares_claim(composition.body, f"{item.title} {item.summary}",
                                  item.trial_n):
            offenders.append((tid, source))
    assert not offenders, offenders[:3]


def test_sparse_contexts_still_produce_specific_messages(categories, expanded, now):
    """Most of the dataset is a bare merchant and a placeholder payload. That is the test."""
    from vera_bot.facts import build_fact_sheet as _sheet
    checked = 0
    for trigger in expanded["triggers"].values():
        if not trigger.get("payload", {}).get("placeholder"):
            continue
        merchant = expanded["merchants"].get(trigger["merchant_id"])
        if not merchant or merchant.get("offers") or merchant.get("signals"):
            continue
        customer = expanded["customers"].get(trigger.get("customer_id"))
        sheet = _sheet(_category_for(categories, merchant), merchant, trigger, customer, now=now)
        composition = compose_from_sheet(sheet, derive(sheet))
        assert composition is not None
        assert len(set(NUMBER.findall(composition.body))) >= 2, composition.body
        assert not PLACEHOLDER.search(composition.body)
        checked += 1
    assert checked >= 25, f"only exercised {checked} sparse cases"
