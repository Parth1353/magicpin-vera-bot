from vera_bot.facts import build_fact_sheet, decode_signal
from vera_bot.insights import derive


def test_signals_are_decoded_never_echoed():
    assert decode_signal("stale_posts:22d").text == "your last Google post went up 22 days ago"
    assert decode_signal("dormant_with_vera_38d").number == 38
    assert decode_signal("engaged_in_last_48h").text
    unknown = decode_signal("some_flag_we_have_never_seen")
    assert unknown.text == "", "an unrecognised flag must never be guessed at"


def test_ledger_accepts_context_numbers_and_rejects_invented_ones(categories, merchants,
                                                                  triggers, now):
    sheet = build_fact_sheet(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                             triggers["trg_001_research_digest_dentists"], None, now=now)
    assert sheet.ledger.unknown_numbers("2,410 views, 38% better, ₹299, JIDA p.14") == []
    assert sheet.ledger.unknown_numbers("we found 987654 new customers") == ["987654"]


def test_dentist_owner_gets_the_honorific(categories, merchants, triggers, now):
    sheet = build_fact_sheet(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                             triggers["trg_001_research_digest_dentists"], None, now=now)
    assert sheet.salutation == "Dr. Meera"


def test_honorific_is_not_doubled_when_the_data_already_has_it(categories, expanded, now):
    merchant = expanded["merchants"]["m_011_dr_sameer_dentist_bangalore"]
    sheet = build_fact_sheet(categories["dentists"], merchant, {}, None, now=now)
    assert sheet.salutation == "Dr. Sameer"
    assert "Dr. Dr." not in sheet.salutation


def test_peer_scope_slug_is_never_exposed_raw(categories, merchants, triggers, now):
    sheet = build_fact_sheet(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                             triggers["trg_001_research_digest_dentists"], None, now=now)
    assert sheet.peer_scope_label == "metro solo practices"
    assert "_" not in sheet.peer_scope_label


def test_placeholder_trigger_is_flagged_and_translated(categories, expanded, now):
    """`chronic_refill_due` on a dentist is a dataset artefact; say the dental version."""
    trigger = expanded["triggers"]["trg_081_chronic_refill_due_m_011_dr_sameer_dent"]
    merchant = expanded["merchants"][trigger["merchant_id"]]
    sheet = build_fact_sheet(categories["dentists"], merchant, trigger, None, now=now)
    assert sheet.trigger.is_placeholder is True
    assert sheet.trigger.translated == "a treatment-plan follow-up that is due"


def test_customer_state_trusts_the_visit_ledger_over_a_contradictory_label(categories,
                                                                          expanded, now):
    customer = expanded["customers"]["c_044_vivaan_for_m_011_dr_sameer_dentist_bangalore"]
    merchant = expanded["merchants"][customer["merchant_id"]]
    sheet = build_fact_sheet(categories["dentists"], merchant, {}, customer, now=now)
    assert customer["state"] == "new" and customer["relationship"]["visits_total"] == 5
    assert sheet.cust["state"] == "returning"


def test_density_separates_rich_from_sparse(categories, merchants, expanded, triggers, now):
    rich = build_fact_sheet(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                            triggers["trg_001_research_digest_dentists"], None, now=now)
    sparse = build_fact_sheet(categories["dentists"],
                              expanded["merchants"]["m_011_dr_sameer_dentist_bangalore"],
                              expanded["triggers"]["trg_081_chronic_refill_due_m_011_dr_sameer_dent"],
                              None, now=now)
    assert rich.is_sparse is False
    assert sparse.is_sparse is True


def test_derived_numbers_show_their_working(categories, merchants, triggers, now):
    """views x (peer_ctr - ctr) is arithmetic over two context numbers, not a guess."""
    sheet = build_fact_sheet(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                             triggers["trg_001_research_digest_dentists"], None, now=now)
    insights = derive(sheet)
    expected = round((0.030 - 0.021) * 2410)
    assert insights.conversion_gap_actions == expected
    assert sheet.ledger.unknown_numbers(str(expected)) == []


def test_contrarian_read_on_an_expected_seasonal_dip(categories, merchants, triggers, now):
    sheet = build_fact_sheet(categories["gyms"], merchants["m_007_powerhouse_gym_bangalore"],
                             triggers["trg_014_seasonal_acquisition_dip_powerhouse"], None,
                             now=now)
    insights = derive(sheet)
    assert "calendar" in insights.contrarian


def test_digest_named_by_the_trigger_wins_the_ranking(categories, merchants, triggers, now):
    sheet = build_fact_sheet(categories["dentists"], merchants["m_001_drmeera_dentist_delhi"],
                             triggers["trg_002_compliance_dci_radiograph"], None, now=now)
    insights = derive(sheet)
    assert insights.top_digest.id == "d_2026W17_dci_radiograph"
