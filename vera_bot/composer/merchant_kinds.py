"""Merchant-facing composers, one per trigger kind.

Each function answers three questions in order: why is this landing now, what does the
merchant not already know, and what is the one thing Vera will do about it. Anything a
composer cannot substantiate from the contexts it simply leaves out.
"""

from __future__ import annotations

from ..facts import FactSheet
from ..insights import Insights
from ..utils import (human_date, num, oxford, pct, pick, rupees, signed_pct, squeeze,
                     trim_trailing_punct)
from ..utils import text
from ..voice import hindi, profile_for
from .base import Plan
from .evidence import peer_line, perf_line, volume_line
from .registry import merchant_kind

# --------------------------------------------------------------------------- #
# shared fragments
# --------------------------------------------------------------------------- #

def _perf_line(sheet: FactSheet, ins: Insights) -> str:
    return perf_line(sheet, with_vocab=True)


def _peer_line(sheet: FactSheet, ins: Insights) -> str:
    return peer_line(sheet, ins)


def _artefact(sheet: FactSheet, seed: str, offset: int = 0) -> str:
    return profile_for(sheet.category_slug).artefact(sheet.merchant_id, seed, offset=offset)


def _offer_phrase(sheet: FactSheet, ins: Insights) -> str:
    if ins.lead_offer:
        return ins.lead_offer.title
    if ins.suggested_offer:
        return ins.suggested_offer.title
    return ""


def _digest_evidence(sheet: FactSheet, item) -> list[str]:
    """One compact, checkable claim carrying the numbers, then the caveat as a second line.

    The scale of a study ("2,100-patient") and its effect size ("38% lower") are the two
    facts that make a research claim verifiable, so they belong in the same sentence — a
    long-message trim must never be able to drop one and keep the other.
    """
    import re as _re
    summary = squeeze(item.summary)
    sentences = [s for s in _re.split(r"(?<=[.!?])\s+", summary) if s] if summary else []
    title = trim_trailing_punct(item.title)
    has_figure = lambda text: bool(_re.search(r"\d", text or ""))       # noqa: E731

    # The claim that earns the citation is the one with the numbers in it. Some digest
    # items carry the figure in the title and prose in the summary; others the reverse.
    if sentences and has_figure(sentences[0]):
        headline = sentences[0]
        caveat = sentences[1] if len(sentences) > 1 else ""
    elif has_figure(title):
        headline = title
        caveat = sentences[0] if sentences else ""
    else:
        headline = sentences[0] if sentences else title
        caveat = sentences[1] if len(sentences) > 1 else ""

    if item.trial_n:
        scale = f"{num(item.trial_n)} {sheet.customer_noun_plural}"
        if _re.search(r"\btrial\b", headline, _re.I):
            headline = _re.sub(r"\btrial\b", f"trial of {scale}", headline,
                               count=1, flags=_re.I)
        else:
            headline = f"a trial of {scale} found {headline[:1].lower()}{headline[1:]}"

    out = [headline]
    if caveat and len(caveat) < 110:
        out.append(caveat)
    elif not summary:
        out.append(trim_trailing_punct(item.title))
    return [o for o in out if squeeze(o)]


def _cohort_hook(sheet: FactSheet, item) -> str:
    """Tie a category finding to this merchant's own roster where the data allows it."""
    aggregate = sheet.customer_aggregate or {}
    segment = (item.segment or "").lower()
    if "high_risk" in segment and aggregate.get("high_risk_adult_count"):
        return (f"{num(aggregate['high_risk_adult_count'])} of your {sheet.customer_noun_plural} "
                f"are flagged high-risk, so this is not a general-interest item for you")
    if aggregate.get("chronic_rx_count") and "chronic" in segment:
        return (f"you have {num(aggregate['chronic_rx_count'])} chronic-prescription "
                f"{sheet.customer_noun_plural} on file")
    total = aggregate.get("total_unique_ytd") or aggregate.get("total_active_members")
    if total:
        return (f"across the {num(total)} {sheet.customer_noun_plural} you have seen this year, "
                f"it will apply to a slice, not all of them")
    return ""


# --------------------------------------------------------------------------- #
# knowledge triggers
# --------------------------------------------------------------------------- #

@merchant_kind("research_digest", "category_research_digest_release")
def research_digest(sheet: FactSheet, ins: Insights) -> Plan | None:
    item = (sheet.trigger.digest_item if sheet.trigger else None) or ins.top_digest
    if not item:
        return None

    plan = Plan(kind="research_digest", angle_id="digest", citation=item.source,
                template="vera_research_digest_v1", hindi_slot="worth_a_look")
    plan.why_now = pick([
        "one item out of this week's reading worth two minutes",
        "this week's digest had exactly one thing I'd flag for you",
        "one finding landed this week that changes a default",
    ], sheet.merchant_id, "why_research")
    plan.evidence = _digest_evidence(sheet, item)
    plan.insight = _cohort_hook(sheet, item) or item.actionable
    plan.proposal = _artefact(sheet, "research")
    plan.ask = pick([
        f"Want me to pull the abstract and {plan.proposal}?",
        f"Say yes and I'll send the abstract with {plan.proposal}.",
    ], sheet.merchant_id, "ask_research")
    plan.proposal = ""      # folded into the ask, so it is not said twice
    plan.levers = ["specificity", "reciprocity", "curiosity"]
    return plan.because(f"selected digest item {item.id} because it is the one the trigger names"
                        if sheet.trigger and sheet.trigger.digest_item else
                        f"selected digest item {item.id} as the highest-relevance item for this listing",
                        "source cited so the claim is checkable")


@merchant_kind("regulation_change", "compliance_alert", "regulation_update")
def regulation_change(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    item = (trigger.digest_item if trigger else None) or next(
        (d for d in ins.ranked_digest if d.kind == "compliance"), None)
    if not item:
        return None

    deadline = (trigger.deadline if trigger else None) or item.date
    days = trigger.days_to_deadline if trigger else None
    when = ""
    if deadline:
        when = f"effective {human_date(deadline, with_year=True)}"
        if days and 0 < days < 400:
            when += f", which is {num(days)} days out"

    plan = Plan(kind="regulation_change", angle_id="compliance", citation=item.source,
                template="vera_compliance_notice_v1", hindi_slot="shall_i_do")
    plan.why_now = f"a compliance change you'll want on the calendar, {when}" if when \
        else "a compliance change worth acting on before it bites"
    plan.evidence = _digest_evidence(sheet, item)
    plan.insight = item.actionable or "the audit trail matters more than the equipment here"
    plan.ask = pick([
        "Want me to write up a one-page checklist you can hand your team?",
        "Shall I draft the checklist and the note for your records?",
    ], sheet.merchant_id, "ask_reg")
    plan.cta = "binary_yes_no"
    plan.levers = ["loss_aversion", "specificity", "effort_externalisation"]
    return plan.because("compliance framing with a hard date, not a general reminder",
                        "offered the artefact rather than asking them to read the circular")


@merchant_kind("cde_opportunity", "training_opportunity")
def cde_opportunity(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    item = (trigger.digest_item if trigger else None) or next(
        (d for d in ins.ranked_digest if d.kind == "cde"), None)
    if not item:
        return None

    payload = trigger.payload if trigger else {}
    credits = payload.get("credits") or item.__dict__.get("credits")
    fee = squeeze(str(payload.get("fee") or "").replace("_", " "))
    when = human_date(item.date, with_year=True) if item.date else ""

    detail = []
    if when:
        detail.append(when)
    if credits:
        detail.append(f"{num(credits)} credits")
    if fee:
        detail.append(fee)

    plan = Plan(kind="cde_opportunity", angle_id="cde", citation=item.source,
                template="vera_cde_invite_v1", hindi_slot="shall_i_do")
    plan.why_now = (trim_trailing_punct(item.title)
                    + (f" — {oxford(detail, 'and')}" if detail else ""))
    plan.evidence = [squeeze(item.summary)]
    # A local chapter event is a short trip; that is a real reason for this merchant.
    if sheet.city and sheet.city.lower() in f"{item.title} {item.source}".lower():
        plan.insight = (f"it is your own {sheet.city} chapter, so it is a short evening "
                        f"rather than a trip")
    elif fee and "free" in fee.lower():
        plan.insight = "no cost if your membership is current, and the date is the only cost"
    else:
        plan.insight = "dated, so it wants a yes or a no rather than a maybe"
    plan.ask = "Want me to hold the date and send you a reminder the morning of?"
    plan.levers = ["specificity", "effort_externalisation", "curiosity"]
    return plan.because("dated event, so the message forces a decision instead of parking it")


@merchant_kind("category_trend_movement", "trend_signal")
def trend_movement(sheet: FactSheet, ins: Insights) -> Plan | None:
    trend = ins.top_trend
    if not trend:
        return None
    plan = Plan(kind="category_trend_movement", angle_id="trend",
                template="vera_demand_shift_v1", hindi_slot="shall_i_send")
    plan.why_now = "demand in your category moved before the listings did"
    plan.evidence = [f"searches for \"{trend.get('query')}\" are {signed_pct(trend.get('delta_yoy'))} "
                     f"year on year"
                     + (f", concentrated in the {trend.get('segment_age')} band"
                        if trend.get("segment_age") and trend.get("segment_age") != "all" else "")]
    if _offer_phrase(sheet, ins):
        plan.insight = (f"your listing does not say anything about it yet — "
                        f"{_offer_phrase(sheet, ins)} is the closest thing you have live")
    plan.ask = f"Want me to write {_artefact(sheet, 'trend')} around it?"
    plan.levers = ["curiosity", "social_proof", "loss_aversion"]
    return plan.because("led with the demand signal because the listing has no matching copy")


# --------------------------------------------------------------------------- #
# competitive + calendar
# --------------------------------------------------------------------------- #

@merchant_kind("competitor_opened")
def competitor_opened(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    rival = (trigger.competitor if trigger else {}) or {}
    name = rival.get("name")
    if not name:
        return None

    distance = rival.get("distance_km")
    their_offer = rival.get("offer")
    opened = rival.get("opened")

    where = f"{num(distance)} km away" if distance else f"in {sheet.locality or sheet.city}"
    since = f" since {human_date(opened, with_year=True)}" if opened else ""

    plan = Plan(kind="competitor_opened", angle_id="competitor",
                template="vera_competitor_watch_v1", hindi_slot="shall_i_do")
    plan.why_now = f"{name} has been live {where}{since}"
    evidence = []
    if their_offer:
        evidence.append(f"they are leading with {their_offer}")
    if ins.lead_offer:
        evidence.append(f"you are at {ins.lead_offer.title}")
    plan.evidence = evidence or [f"a new listing opened {where}"]

    cmp_ = ins.comparisons.get("ctr")
    if their_offer and ins.lead_offer and ins.lead_offer.price and _price_of(their_offer):
        plan.insight = ("matching them on price is the losing move — a new listing has no "
                        "reviews and no history, and that is the gap you should be widening")
    elif cmp_ and cmp_.ahead:
        plan.insight = (f"you convert at {cmp_.render_mine()} against a {cmp_.render_peer()} "
                        f"median, so reputation is your advantage here, not price")
    else:
        plan.insight = ("the first ninety days are when a new listing takes its share, so this "
                        "is the window that matters")
    plan.ask = (f"Want me to put your review count and your {sheet.customer_noun} history "
                f"in front of people searching this area?")
    plan.levers = ["loss_aversion", "social_proof", "curiosity"]
    return plan.because("named only the competitor facts supplied in the trigger",
                        "argued against price-matching rather than reflexively discounting")


def _price_of(title: str) -> float | None:
    import re
    hit = re.search(r"₹\s*([\d,]+)", title or "")
    return float(hit.group(1).replace(",", "")) if hit else None


@merchant_kind("festival_upcoming", "festival")
def festival_upcoming(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    festival = (trigger.festival if trigger else {}) or {}
    name = festival.get("name")
    days_until = festival.get("days_until")
    relevant = festival.get("relevant_to") or []

    name = text(name)
    if not name:
        return None     # placeholder payload: the fallback composer has more to work with
    if relevant and sheet.category_slug and sheet.category_slug not in relevant:
        return None     # the trigger itself says this festival is not for this vertical

    plan = Plan(kind="festival_upcoming", angle_id="season",
                template="vera_seasonal_plan_v1", hindi_slot="shall_i_send")

    # A festival six months out is not a reason to message today. Say so, and use the
    # window that is actually open instead — that is the judgment, not the calendar entry.
    if days_until and int(days_until) > 45:
        near = ins.season
        plan.why_now = (f"{name} is still {num(days_until)} days out, so this is not a "
                        f"{name} message")
        if near:
            plan.evidence = [f"the window that is actually open is {near.get('month_range')} — "
                             f"{squeeze(str(near.get('note', '')))}"]
        elif ins.top_trend:
            plan.evidence = [f"what is moving right now is \"{ins.top_trend.get('query')}\", "
                             f"{signed_pct(ins.top_trend.get('delta_yoy'))} year on year"]
        else:
            return None
        plan.insight = (f"I'll come back on {name} when the booking window opens; today the "
                        f"return is in the nearer one")
        plan.ask = f"Want me to build this week around that instead?"
        plan.levers = ["specificity", "reciprocity"]
        return plan.because(f"declined to send a {name} nudge {num(days_until)} days early and "
                            f"redirected to the season that is live",
                            "restraint on the literal trigger is the decision being made here")

    when = human_date(festival.get("date"), with_year=True) if festival.get("date") else ""
    plan.why_now = f"{name} lands {when}" if when else f"{name} is close"
    plan.evidence = [f"that is {num(days_until)} days to get your listing and your offer ready"
                     if days_until else "the booking window opens now"]
    if ins.season:
        plan.evidence.append(squeeze(str(ins.season.get("note", ""))))
    plan.insight = (f"{ins.lead_offer.title} is what you have live, and festival traffic "
                    f"converts better on a named service than on a discount"
                    if ins.lead_offer else
                    "festival traffic converts on a named service at a named price, not on a flat discount")
    plan.ask = f"Want me to draft {_artefact(sheet, 'festival')} for it?"
    plan.levers = ["specificity", "loss_aversion", "effort_externalisation"]
    return plan.because("festival is inside the planning window, so the ask is concrete")


@merchant_kind("ipl_match_today", "local_event", "local_news_event")
def match_day(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    event = (trigger.event if trigger else {}) or {}
    match = event.get("match")
    venue = event.get("venue")
    when = event.get("time")

    header = " ".join(filter(None, [match, f"at {venue}" if venue else ""]))
    if when:
        parsed = human_date(when)
        header += f", {parsed}" if parsed else ""

    plan = Plan(kind="ipl_match_today", angle_id="event",
                template="vera_matchday_plan_v1", hindi_slot="shall_i_do")
    plan.why_now = header or "there's a match in your catchment tonight"

    evidence = []
    ipl_item = next((d for d in ins.ranked_digest if "ipl" in f"{d.id}{d.title}".lower()), None)
    if ipl_item:
        evidence.append(squeeze(ipl_item.summary) or ipl_item.title)
        plan.citation = ipl_item.source
    if ins.lead_offer:
        evidence.append(f"you already have {ins.lead_offer.title} running")
    plan.evidence = evidence or ["match nights move covers in both directions"]

    plan.insight = ins.contrarian or ("the play is to push what is already live rather than "
                                      "stand up a new offer with hours to go")
    plan.ask = f"Want me to set up {_artefact(sheet, 'match')} for tonight?"
    plan.levers = ["specificity", "loss_aversion", "effort_externalisation"]
    return plan.because("used the category order data to decide whether to lean in or sit out",
                        "leveraged the live offer instead of inventing a new one")


@merchant_kind("category_seasonal", "seasonal_shift")
def category_seasonal(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    moves = trigger.seasonal_trends if trigger else []
    readable = [_readable_trend(m) for m in moves]
    readable = [r for r in readable if r]

    plan = Plan(kind="category_seasonal", angle_id="season",
                template="vera_shelf_shift_v1", hindi_slot="shall_i_do")
    season = ins.season
    plan.why_now = ("the seasonal swing in your category has started"
                    if not season else
                    f"{season.get('month_range')} is when this category turns over")

    if readable:
        up = [r for r in readable if "up" in r]
        down = [r for r in readable if "down" in r]
        line = oxford(up[:3], "and")
        if down:
            line += f", while {oxford(down[:2], 'and')}"
        plan.evidence = [line]
    elif season:
        plan.evidence = [squeeze(str(season.get("note", "")))]
    else:
        return None

    item = next((d for d in ins.ranked_digest if d.kind == "seasonal"), None)
    if item:
        plan.citation = item.source
        plan.insight = item.actionable or squeeze(item.summary)
    if not plan.insight:
        plan.insight = "the shelf that moves first is the one people find first"
    plan.ask = f"Want me to get {_artefact(sheet, 'season')} ready before the swing?"
    plan.levers = ["specificity", "effort_externalisation"]
    return plan.because("used the exact demand moves in the trigger rather than a generic "
                        "seasonal reminder")


def _readable_trend(token: str) -> str:
    """`ORS_demand_+40` -> 'ORS demand is up 40%'."""
    import re
    hit = re.match(r"^(.*?)_?([+-]\d+)$", squeeze(str(token)))
    if not hit:
        return squeeze(str(token).replace("_", " "))
    label = squeeze(hit.group(1).replace("_", " "))
    value = int(hit.group(2))
    return f"{label} is {'up' if value >= 0 else 'down'} {abs(value)}%"


# --------------------------------------------------------------------------- #
# performance
# --------------------------------------------------------------------------- #

@merchant_kind("perf_dip")
def perf_dip(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    metric = text((trigger.metric if trigger else None), "calls")
    delta = trigger.delta_pct if trigger else None
    window = text((trigger.window if trigger else None), "7d").replace("d", " days")
    baseline = trigger.baseline if trigger else None

    if delta is None:
        if sheet.calls_delta is not None and sheet.calls_delta < 0:
            delta, metric = sheet.calls_delta, "calls"
        elif sheet.views_delta is not None and sheet.views_delta < 0:
            delta, metric = sheet.views_delta, "views"

    plan = Plan(kind="perf_dip", angle_id="performance",
                template="vera_perf_dip_v1", hindi_slot="shall_i_do")

    if delta is not None:
        plan.why_now = f"your {metric} are {signed_pct(delta)} over the last {window}"
    else:
        plan.why_now = f"your {metric} slipped this week"

    evidence = []
    if baseline:
        evidence.append(f"against a normal week of about {num(baseline)}")
    perf = _perf_line(sheet, ins)
    if perf:
        evidence.append(perf)
    plan.evidence = evidence

    # Diagnose rather than announce — the dip has a shape and the shape names the fix.
    if ins.divergence:
        plan.insight = ins.divergence
    elif ins.season and ins.season_mood == "dip":
        plan.insight = (f"before you read too much into it, {ins.season.get('month_range')} is "
                        f"{squeeze(str(ins.season.get('note', '')).split('—')[0])} in this category")
    elif ins.conversion_gap_actions:
        plan.insight = (f"the traffic is still arriving — it is the listing that is not "
                        f"closing, and at the local median that same traffic would be doing "
                        f"about {num(ins.actions_at_peer)} actions a month instead of "
                        f"{num(ins.actions_now)}")
    else:
        plan.insight = "worth separating a demand problem from a listing problem before spending"

    fixes = _fix_list(sheet, ins)
    if fixes:
        plan.proposal = (f"the two fixes that move this fastest: {oxford(fixes[:2], 'and')}"
                         if len(fixes) > 1 else f"the fix that moves this fastest: {fixes[0]}")
    plan.ask = (("Want me to start on those today?" if len(fixes) > 1
                 else "Want me to start on that today?") if fixes else
                f"Want me to put {_artefact(sheet, 'dip')} together?")
    plan.levers = ["loss_aversion", "specificity", "effort_externalisation"]
    return plan.because("diagnosed the dip instead of restating it",
                        "named a bounded fix so the reply costs one word")


def _fix_list(sheet: FactSheet, ins: Insights) -> list[str]:
    """Concrete, listing-level fixes that this merchant's own context supports."""
    fixes: list[str] = []
    if not sheet.verified:
        fixes.append("getting your Google listing verified")
    if not sheet.active_offers and ins.suggested_offer:
        fixes.append(f"putting {ins.suggested_offer.title} live")
    stale = sheet.signal("stale_posts")
    if stale and stale.number:
        fixes.append(f"a post — your last one went up {num(stale.number)} days ago")
    elif sheet.has_signal("no_recent_post"):
        fixes.append("getting a post back up on the listing")
    negative = sheet.theme("neg")
    if negative:
        fixes.append(f"a public reply on the {squeeze(str(negative.get('theme','')).replace('_',' '))} reviews")
    return fixes


@merchant_kind("seasonal_perf_dip")
def seasonal_perf_dip(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    delta = trigger.delta_pct if trigger else None
    metric = squeeze(str((trigger.metric if trigger else "") or "views"))

    plan = Plan(kind="seasonal_perf_dip", angle_id="season_reframe",
                template="vera_seasonal_reframe_v1", hindi_slot="no_pressure")
    plan.why_now = (f"your {metric} are {signed_pct(delta)} this week — before you act on that, "
                    f"one piece of context" if delta is not None else
                    f"your {metric} dipped this week — one piece of context first")

    evidence = []
    beat = ins.season
    if beat:
        note = squeeze(str(beat.get("note", "")))
        headline = squeeze(note.split("—")[0])
        evidence.append(f"across this category {beat.get('month_range')} is the "
                        f"{headline} of the year")
    item = next((d for d in ins.ranked_digest if d.kind == "seasonal"), None)
    if item:
        evidence.append(squeeze(item.summary))
        plan.citation = item.source
    plan.evidence = evidence or ["this is the expected shape of the season"]

    plan.insight = ins.contrarian or ("this is the calendar rather than the listing, so spending "
                                      "into it is the expensive mistake")

    members = (sheet.customer_aggregate or {}).get("total_active_members") \
        or (sheet.customer_aggregate or {}).get("total_unique_ytd")
    if members:
        plan.proposal = (f"something for the {num(members)} {sheet.customer_noun_plural} you "
                         f"already have, which is where the return is this month")
    plan.ask = "Want me to draft that this week?"
    plan.levers = ["reciprocity", "specificity", "loss_aversion"]
    return plan.because("told the merchant not to spend, which is the opposite of the obvious play",
                        "redirected the same effort to retention where the season favours it")


@merchant_kind("perf_spike", "milestone_reached")
def good_news(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    kind = trigger.kind if trigger else "perf_spike"
    plan = Plan(kind=kind, angle_id="momentum",
                template="vera_momentum_v1", hindi_slot="shall_i_do")

    if kind == "milestone_reached":
        milestone = trigger.milestone if trigger else {}
        now_value, target = milestone.get("now"), milestone.get("target")
        metric = {"review_count": "reviews", "rating": "star rating",
                  "photo_count": "photos"}.get(milestone.get("metric"),
                                               text(milestone.get("metric"), "reviews"))
        if now_value and target:
            gap = sheet.allow(int(target) - int(now_value), "derived.milestone_gap")
            plan.why_now = f"you're at {num(now_value)} {metric} — {num(gap)} short of {num(target)}"
            plan.insight = (f"the last few are the ones that stick, because {num(target)} reads "
                            f"differently to someone choosing between two listings")
        elif now_value:
            plan.why_now = f"you've crossed {num(now_value)} {metric}"
        elif volume_line(sheet):
            plan.why_now = f"a marker worth noting — {volume_line(sheet)}"
        else:
            plan.why_now = "you crossed a mark on the listing this week"
        peer_reviews = (sheet.peer or {}).get("avg_review_count")
        if peer_reviews and now_value:
            ratio = float(now_value) / float(peer_reviews)
            standing = ("comfortably ahead of it" if ratio >= 1.25 else
                        "just ahead of it" if ratio >= 1.02 else
                        "still short of it" if ratio < 0.98 else "right on it")
            plan.evidence = [f"the median listing in {sheet.peer_scope_label} sits at "
                             f"{num(peer_reviews)}, so you're {standing}"]
        elif perf_line(sheet):
            plan.evidence = [perf_line(sheet, with_vocab=True), peer_line(sheet, ins)]
        plan.proposal = (f"a short ask your team can send to this week's {sheet.customer_noun_plural} "
                         f"— that's usually enough to close a gap this size")
        plan.ask = "Want me to write it?"
        plan.levers = ["social_proof", "curiosity", "effort_externalisation"]
        return plan.because("framed the milestone as a gap to close rather than a pat on the back")

    delta = trigger.delta_pct if trigger else None
    metric = text((trigger.metric if trigger else None), "calls")
    driver = text((trigger.payload or {}).get("likely_driver"))
    baseline = trigger.baseline if trigger else None

    # A placeholder payload still leaves the merchant's own 7-day movement to work with.
    if delta is None:
        if sheet.calls_delta is not None and sheet.calls_delta > 0:
            delta, metric = sheet.calls_delta, "calls"
        elif sheet.views_delta is not None and sheet.views_delta > 0:
            delta, metric = sheet.views_delta, "views"

    plan.why_now = (f"your {metric} are {signed_pct(delta)} this week"
                    if delta is not None else f"your {metric} moved up this week")
    evidence = []
    if baseline:
        evidence.append(f"up from a baseline around {num(baseline)}")
    elif perf_line(sheet):
        evidence.append(perf_line(sheet, with_vocab=True))
    if driver:
        evidence.append(f"it traces back to your {driver}")
    plan.evidence = evidence
    plan.insight = (f"that's a repeatable cause, not a lucky week — worth running again while "
                    f"it's still working" if driver else
                    "a spike is only useful if you can name what caused it and do it again")
    plan.proposal = (f"another one like your {driver}, same shape" if driver
                     else f"a repeat of it as {_artefact(sheet, 'spike')}")
    plan.ask = "Want me to line it up for this week?"
    plan.levers = ["curiosity", "social_proof", "reciprocity"]
    return plan.because("attributed the spike to a named cause so it can be repeated")


# --------------------------------------------------------------------------- #
# commercial + relationship
# --------------------------------------------------------------------------- #

@merchant_kind("renewal_due", "subscription_expiring")
def renewal_due(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    days = payload.get("days_remaining", sheet.days_remaining)
    amount = payload.get("renewal_amount")

    plan = Plan(kind="renewal_due", angle_id="commercial",
                template="vera_renewal_v1", hindi_slot="shall_i_do")
    plan.why_now = (f"your {text(sheet.plan, '')} plan renews in {num(days)} days".replace("  ", " ")
                    if days is not None else "your plan is up for renewal")

    perf = _perf_line(sheet, ins)
    if perf:
        plan.evidence.append(perf)
    cmp_ = ins.comparisons.get("ctr")
    if cmp_ and not cmp_.ahead:
        plan.evidence.append(_peer_line(sheet, ins))

    fixes = _fix_list(sheet, ins)
    if fixes:
        # Fixing the listing before asking for money is both honest and better business.
        plan.insight = (f"I'd rather not ask you to renew a listing that isn't pulling its "
                        f"weight — {oxford(fixes[:2], 'and')} are the two things holding it back")
        plan.proposal = "both of those before the renewal date, at no extra cost"
        plan.ask = "Want me to start on them today?"
        plan.because("led with fixing the listing rather than with the invoice")
    else:
        if amount:
            plan.insight = (f"{rupees(amount)} for the cycle, and the listing has been earning "
                            f"against it")
        plan.proposal = "a one-page summary of what the last cycle actually returned"
        plan.ask = "Want that before you decide?"
        plan.because("gave the merchant the evidence to decide instead of a reminder to pay")
    plan.levers = ["reciprocity", "specificity", "loss_aversion"]
    return plan


@merchant_kind("winback_eligible", "subscription_expired")
def winback(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    since = payload.get("days_since_expiry", sheet.days_since_expiry)
    dip = payload.get("perf_dip_pct")
    lapsed = payload.get("lapsed_customers_added_since_expiry")

    plan = Plan(kind="winback_eligible", angle_id="commercial",
                template="vera_winback_v1", hindi_slot="shall_i_do")
    plan.why_now = (f"it's been {num(since)} days since your plan lapsed"
                    if since else "your plan has been lapsed a while")

    evidence = []
    if dip is not None:
        evidence.append(f"traffic is {signed_pct(dip)} since then")
    if lapsed:
        evidence.append(f"{num(lapsed)} {sheet.customer_noun_plural} have gone quiet in that window")
    elif ins.lapsed_count:
        evidence.append(f"{num(ins.lapsed_count)} {sheet.customer_noun_plural} haven't been back "
                        f"in the {ins.lapsed_label}")
    plan.evidence = evidence or [_perf_line(sheet, ins)]

    if ins.lapsed_value:
        owns_price = any(o.price == ins.lapsed_unit_price for o in
                         sheet.active_offers + sheet.inactive_offers)
        priced = (f"your {rupees(ins.lapsed_unit_price)} service" if owns_price
                  else f"a typical {rupees(ins.lapsed_unit_price)} service in this category")
        plan.insight = (f"the {num(ins.lapsed_count)} on your list who haven't been back in "
                        f"the {ins.lapsed_label}, at {priced}, come to roughly "
                        f"{rupees(ins.lapsed_value)} of repeat work — more than the plan costs")
    else:
        plan.insight = ("the listing keeps taking views either way; what stopped is anyone "
                        "working on it")
    plan.proposal = (f"a win-back message for that list, which you can send whether or not you "
                     f"come back on the plan")
    plan.ask = "Want me to draft it?"
    plan.levers = ["loss_aversion", "reciprocity", "specificity"]
    return plan.because("valued the dormant list in rupees using their own entry price",
                        "offered value that does not depend on them paying first")


@merchant_kind("dormant_with_vera", "reengagement")
def dormant(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    silent_days = payload.get("days_since_last_merchant_message")

    # Re-engagement earns attention with something new, never with "you haven't replied".
    angle = ins.best_angle(exclude={"lapsed_plan"})
    if angle is None:
        return None

    plan = Plan(kind="dormant_with_vera", angle_id=angle.id,
                template="vera_reengage_v1", hindi_slot="worth_a_look")
    plan.proposal = ""
    plan.why_now = pick([
        "not chasing the last thread — this is new and it's worth the two minutes",
        "different subject to last time, and this one is worth your two minutes",
    ], sheet.merchant_id, "dormant")
    plan.evidence = list(angle.evidence)
    plan.insight = angle.insight
    # only carry the angle's citation if the angle's evidence came with it
    plan.citation = angle.citation if angle.id == "digest" else ""
    plan.proposal = ""              # the artefact is named once, in the ask
    plan.ask = f"Want me to turn that into {_artefact(sheet, 'dormant')}?"
    plan.levers = list(angle.levers) + ["curiosity"]
    return plan.because(
        f"merchant has been quiet{f' for {num(silent_days)} days' if silent_days else ''}, so the "
        f"re-open leads with new value rather than a follow-up on the old thread",
        f"picked the '{angle.id}' angle as the strongest unused signal on this listing")


@merchant_kind("curious_ask_due", "scheduled_recurring")
def curious_ask(sheet: FactSheet, ins: Insights) -> Plan | None:
    """The 'ask the merchant' family — production Vera's biggest gap, per the brief.

    A blind question is cheap. A question with an informed guess attached is the one that
    gets answered, because disagreeing is easier than composing an answer.
    """
    plan = Plan(kind="curious_ask_due", angle_id="curious",
                template="vera_curious_ask_v1", cta="open_ended", hindi_slot="one_line")

    guess = None
    positive = sheet.theme("pos")
    if positive and positive.get("common_quote"):
        guess = squeeze(str(positive.get("theme", "")).replace("_", " "))
        quote = squeeze(str(positive["common_quote"]))
        plan.evidence = [f"your reviews keep coming back to it — one this month read "
                         f"\"{quote}\""]
    elif ins.lead_offer:
        guess = ins.lead_offer.title
        plan.evidence = []          # the guess belongs in the question, not before it
    elif ins.top_trend:
        # A search query is what customers type, not a service on this merchant's menu, so
        # it can set up the question but must never be offered as the answer.
        plan.evidence = [f"searches for \"{ins.top_trend.get('query')}\" in your area are "
                         f"{signed_pct(ins.top_trend.get('delta_yoy'))} year on year"]

    plan.why_now = pick([
        "my turn to ask you something, because my data only sees half of it",
        "one question this week, and you'll know the answer better than my numbers do",
        "flipping it around this week — this one only you can answer",
    ], sheet.merchant_id, "curious")

    artefact = _artefact(sheet, "curious")
    plan.plain_insight = True
    if guess:
        plan.insight = (f"which of your services people have been walking in and asking for "
                        f"this week — my money is on {guess}")
        plan.ask = pick([
            "Correct me in one line and this week's post gets built around the real answer.",
            "One line back, right or wrong, and I'll have the copy across by this evening.",
        ], sheet.merchant_id, "curious_ask")
    else:
        plan.insight = (f"which of your services people have been walking in and asking for "
                        f"this week")
        plan.ask = f"One line back and I'll turn it into {artefact}."
    plan.levers = ["asking_the_merchant", "reciprocity", "curiosity"]
    return plan.because("asking-the-merchant is the highest-response lever for an engaged listing",
                        "attached an informed guess so the reply is a yes/no rather than an essay",
                        "value is offered up front, before anything is asked in return")


@merchant_kind("active_planning_intent")
def active_planning(sheet: FactSheet, ins: Insights) -> Plan | None:
    """The merchant already said yes. Deliver the artefact; do not ask another question."""
    trigger = sheet.trigger
    topic = squeeze(str((trigger.intent_topic if trigger else "") or "").replace("_", " "))
    said = squeeze(str((trigger.merchant_last_message if trigger else "") or ""))

    plan = Plan(kind="active_planning_intent", angle_id="action",
                template="vera_planning_draft_v1", cta="binary_confirm_cancel",
                hindi_slot="i_drafted")
    plan.why_now = (f"picking up {topic} where we left it — here's a first version you can "
                    f"cut into" if topic else "here's the first version you asked for")

    draft = _draft_for_topic(sheet, ins, topic)
    plan.evidence = draft["lines"]
    plan.insight = draft["insight"]
    plan.proposal = ""
    plan.evidence_priority = 90      # the draft is the deliverable, never trim it away
    plan.ask = "Reply CONFIRM and I'll set it up, or send edits and I'll redo it."
    plan.levers = ["effort_externalisation", "specificity", "reciprocity"]
    plan.max_evidence = 4
    return plan.because(
        f"merchant had already committed ({said[:60]}…), so this is delivery, not qualification"
        if said else "merchant had already committed, so this is delivery, not qualification",
        "every price in the draft is derived from their own live offer, and marked as editable")


def _draft_for_topic(sheet: FactSheet, ins: Insights, topic: str) -> dict:
    """Build a concrete, editable proposal out of what the two sides already agreed.

    When the conversation history already contains a shape both sides accepted — the Zen
    Yoga thread carries "4-week program, 3 classes/week, age 7-12, ₹2,499" — restating that
    and moving it forward beats inventing a fresh structure over the top of it.
    """
    import re as _re
    base = ins.lead_offer
    lines: list[str] = []
    insight = ""

    agreed = _agreed_shape(sheet)
    if agreed:
        lines.append(f"what we landed on: {agreed}")
        lines.append(_operational_line(sheet))
        insight = ("all of that came out of our last exchange, so change any part of it and "
                   "I'll rework the rest around your change")
        return {"lines": [l for l in lines if l][:3], "insight": insight}

    if base and base.price:
        price = base.price
        tier_a = sheet.allow(int(round(price * 0.88 / 5) * 5), "derived.tier_from_offer_price")
        tier_b = sheet.allow(int(round(price * 0.80 / 5) * 5), "derived.tier_from_offer_price")
        tier_c = sheet.allow(int(round(price * 0.72 / 5) * 5), "derived.tier_from_offer_price")
        lines.append(f"built off your {base.title}: {rupees(tier_a)} each from 10 up, "
                     f"{rupees(tier_b)} from 25, {rupees(tier_c)} from 50")
        lines.append("orders confirmed the day before, one delivery window, one point of contact")
        insight = (f"the tiers come off your own {rupees(price)} price, so your margin holds — "
                   f"change any number and I'll rework the rest around it")
    else:
        catalogue = ins.suggested_offer
        if catalogue:
            lines.append(f"anchor it on {catalogue.title}, which is the entry price this "
                         f"category converts on")
        beat = ins.season
        if beat:
            lines.append(f"run it through {beat.get('month_range')} while "
                         f"{squeeze(str(beat.get('note','')).split('—')[0])}")
        insight = "start narrow, one price and one slot, and widen it once it fills"

    return {"lines": [l for l in lines if l][:4], "insight": insight}


#: How each vertical actually takes and fulfils a booking — generic boilerplate here
#: reads as a bot that does not know what business it is talking to.
_OPERATIONS = {
    "gyms": "sign-ups over WhatsApp, payment at the first session, and a cap on group size",
    "salons": "bookings over WhatsApp, a held slot per client, and a same-day reminder",
    "restaurants": "orders confirmed the day before, one delivery window, one point of contact",
    "dentists": "appointments over WhatsApp, a confirmation the evening before, and a "
                "reschedule link if it slips",
    "pharmacies": "orders over WhatsApp, one delivery run a day, and a note when stock is short",
}


def _operational_line(sheet: FactSheet) -> str:
    return _OPERATIONS.get(sheet.category_slug, "")


def _agreed_shape(sheet: FactSheet) -> str:
    """Pull the concrete spec out of the last thing Vera proposed, if it had one."""
    import re as _re
    history = sheet.last_vera_message
    body = squeeze(str((history or {}).get("body", "")))
    if not body:
        return ""
    # Split on separators only — never on a comma, which lives inside "₹2,499".
    clauses = [squeeze(c) for c in _re.split(r"[.;]|\s—\s", body) if _re.search(r"\d", c)]
    clauses = [c for c in clauses if not _re.match(r"^(want|shall|should|can)\b", c, _re.I)]
    # A spec is a proposal, not a passing statistic: it needs more than one figure in it.
    clauses = [c for c in clauses if len(_re.findall(r"\d[\d,]*", c)) >= 2]
    if not clauses:
        return ""
    spec = "; ".join(clauses[:2])
    # "Suggest 4-week program" was Vera proposing; restated as agreed it is just the spec.
    return squeeze(_re.sub(r"^(?:suggest(?:ing|ed)?|propose[d]?|recommend(?:ing|ed)?|"
                           r"how about|maybe)\s+", "", spec, flags=_re.I))


# --------------------------------------------------------------------------- #
# listing hygiene
# --------------------------------------------------------------------------- #

@merchant_kind("gbp_unverified", "profile_incomplete")
def gbp_unverified(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    uplift = payload.get("estimated_uplift_pct")
    # "postcard_or_phone_call" -> "postcard or a phone call", not "postcard or or or phone"
    path = text(payload.get("verification_path")).replace(" or ", " or a ")

    plan = Plan(kind="gbp_unverified", angle_id="unverified",
                template="vera_verify_listing_v1", hindi_slot="shall_i_do")
    plan.why_now = "your Google listing is still unverified, and that is capping everything else"
    evidence = []
    perf = perf_line(sheet)          # plain form: another clause is appended below
    if perf:
        evidence.append(perf + ", all of it against a listing Google has not confirmed")
    if uplift:
        evidence.append(f"verified listings in this category typically pick up "
                        f"{pct(uplift)} more visibility")
    plan.evidence = evidence

    plan.insight = (f"it's {path} and it takes a few minutes at your end — the rest I can do"
                    if path else
                    "it's a short one-time step at your end and I can handle the rest")
    plan.proposal = "everything except the code Google sends you"
    plan.ask = "Want me to start it today?"
    plan.levers = ["loss_aversion", "effort_externalisation", "specificity"]
    return plan.because("verification gates every other improvement, so it outranks other angles",
                        "the ask is bounded to the one step only the owner can do")


@merchant_kind("review_theme_emerged")
def review_theme(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    theme = squeeze(str((trigger.theme if trigger else "") or "").replace("_", " "))
    count = trigger.occurrences if trigger else None
    quote = squeeze(str((trigger.quote if trigger else "") or ""))
    trend = squeeze(str((trigger.payload or {}).get("trend", "")))

    if not theme:
        negative = sheet.theme("neg")
        if not negative:
            return None
        theme = squeeze(str(negative.get("theme", "")).replace("_", " "))
        count = negative.get("occurrences_30d")
        quote = squeeze(str(negative.get("common_quote", "")))

    plan = Plan(kind="review_theme_emerged", angle_id="reputation",
                template="vera_review_theme_v1", hindi_slot="shall_i_send")
    plan.why_now = (f"{num(count)} reviews this month landed on the same thing — {theme}"
                    if count else f"a pattern is forming in your reviews around {theme}")
    evidence = []
    if quote:
        line = f"one of them put it as \"{quote}\""
        if trend == "rising":
            line += ", and the pattern is still building rather than settling"
        evidence.append(line)
    elif trend == "rising":
        evidence.append("the pattern is still building rather than settling")
    plan.evidence = evidence

    plan.insight = ("a public reply is worth more than the fix on its own — people reading "
                    "reviews are watching how you answer, not just what went wrong")
    plan.proposal = "a reply you can post under each of them, in your voice not a corporate one"
    plan.ask = "Want me to write those?"
    plan.levers = ["loss_aversion", "reciprocity", "specificity"]
    return plan.because("used the exact review count and the quoted complaint from the trigger",
                        "targeted the public reply, which is the lever the merchant controls today")


@merchant_kind("supply_alert", "product_recall")
def supply_alert(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    molecules = trigger.molecules if trigger else []
    batches = trigger.batches if trigger else []
    maker = squeeze(str((trigger.payload or {}).get("manufacturer", "")))
    item = (trigger.digest_item if trigger else None) or next(
        (d for d in ins.ranked_digest if d.kind == "alert"), None)

    if not molecules and not item:
        return None

    plan = Plan(kind="supply_alert", angle_id="alert",
                template="vera_supply_alert_v1", hindi_slot="shall_i_send")
    if item:
        plan.citation = item.source

    subject = oxford(molecules[:3], "and") or squeeze(item.title)
    plan.why_now = f"time-sensitive one on {subject}"
    evidence = []
    if batches:
        evidence.append(f"{'batches' if len(batches) > 1 else 'batch'} {oxford(batches, 'and')}"
                        + (f" from {maker}" if maker else "")
                        + ", flagged for sub-potency rather than a safety risk")
    elif item:
        evidence.append(squeeze(item.summary))
    if item and not batches:
        pass
    elif item and "replacement" in squeeze(item.summary).lower():
        evidence.append("replacement runs through the distributor return chain")
    plan.evidence = evidence

    chronic = (sheet.customer_aggregate or {}).get("chronic_rx_count")
    if chronic:
        plan.insight = (f"you have {num(chronic)} chronic-prescription {sheet.customer_noun_plural} "
                        f"on file — I can filter that list down to the ones dispensed these "
                        f"batches rather than alarming all of them")
    else:
        plan.insight = ("the right move is a narrow, factual note to the people actually affected, "
                        "not a broad announcement")
    plan.ask = "Want me to draft that note and the replacement steps?"
    plan.levers = ["loss_aversion", "specificity", "effort_externalisation"]
    return plan.because("highest-urgency trigger in the queue, so it pre-empts everything else",
                        "narrowed the audience using the merchant's own roster count instead of "
                        "guessing how many were affected")
