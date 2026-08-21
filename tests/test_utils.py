from vera_bot import utils


def test_indian_digit_grouping():
    assert utils.indian_commas(1234567) == "12,34,567"
    assert utils.indian_commas(2410) == "2,410"
    assert utils.indian_commas(999) == "999"


def test_percent_and_signed_percent():
    assert utils.pct(0.021) == "2.1%"
    assert utils.pct(0.03) == "3%"
    assert utils.signed_pct(-0.5) == "down 50%"
    assert utils.signed_pct(0.18) == "up 18%"


def test_month_range_wraps_the_year():
    assert utils.month_range_covers("Nov-Feb", 1) is True
    assert utils.month_range_covers("Nov-Feb", 6) is False
    assert utils.month_range_covers("Apr-Jun", 4) is True
    assert utils.month_range_covers("Feb 14", 2) is True


def test_sentence_split_keeps_honorifics_intact():
    assert utils.split_sentences("Dr. Meera, one finding. Next sentence.") == [
        "Dr. Meera, one finding.", "Next sentence."]
    assert len(utils.split_sentences("See JIDA Oct 2026, p. 14. Then this.")) == 2


def test_text_never_renders_the_word_none():
    assert utils.text(None) == ""
    assert utils.text("None") == ""
    assert utils.text("weekday_evening") == "weekday evening"
    assert utils.text(None, "fallback") == "fallback"


def test_pick_is_deterministic():
    options = ["a", "b", "c", "d"]
    assert utils.pick(options, "m_001", "x") == utils.pick(options, "m_001", "x")
    assert utils.pick(options, "m_001", "x") != utils.pick(options, "m_001", "x", offset=1)
