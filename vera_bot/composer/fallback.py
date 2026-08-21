"""The composer that runs when the trigger tells us almost nothing.

Three quarters of the expanded trigger set carries `payload: {"placeholder": true}`, and
four fifths of the merchants have no offers, no signals, no history and no review themes.
That is the realistic case, not the edge case: a bot that is only good when the payload is
rich will score on a third of the test pairs and fall over on the rest.

What is always present is the merchant's identity, subscription and 30-day performance,
plus a fully populated category context. That is enough to be specific every time — so
this composer reads the trigger's *kind* for the reason to send, and then goes to the
merchant's own numbers and the category's benchmarks for something worth saying.
"""

from __future__ import annotations

from ..facts import FactSheet
from ..insights import Insights
from ..utils import num, oxford, pick, signed_pct, squeeze
from ..voice import profile_for
from .base import Plan

#: how each kind explains itself when the payload is empty
_WHY_NOW = {
    "research_digest": "this week's category reading is in",
    "perf_dip": "your week-on-week numbers moved the wrong way",
    "perf_spike": "your week-on-week numbers jumped",
    "milestone_reached": "you crossed a mark on the listing",
    "dormant_with_vera": "we haven't spoken in a while and I'd rather come back with something useful",
    "review_theme_emerged": "a pattern showed up in your recent reviews",
    "competitor_opened": "the listings around you changed",
    "festival_upcoming": "there's a date coming that shifts demand in your category",
    "recall_due": "one of your regulars is due back",
    "customer_lapsed_soft": "someone on your list has started drifting",
    "customer_lapsed_hard": "someone on your list has stopped coming",
    "appointment_tomorrow": "there's a booking on tomorrow's sheet",
    "chronic_refill_due": "a repeat customer is due",
    "trial_followup": "a first-timer is at the point where they decide",
    "renewal_due": "your plan comes up for renewal",
    "curious_ask_due": "it's the weekly check-in, and this one is me asking you",
    "gbp_unverified": "your Google listing still isn't verified",
    "supply_alert": "there's a supply notice for your category",
    "category_seasonal": "the season is turning in your category",
    "winback_eligible": "your plan lapsed and the listing is still running",
    "active_planning_intent": "picking up what we were planning",
    "seasonal_perf_dip": "your numbers dipped in the window where they usually do",
}


def compose_fallback(sheet: FactSheet, ins: Insights) -> Plan:
    trigger = sheet.trigger
    kind = trigger.kind if trigger else "scheduled_recurring"
    voice = profile_for(sheet.category_slug)

    plan = Plan(kind=kind, angle_id="fallback", template=f"vera_{_template_stem(kind)}_v1",
                hindi_slot="shall_i_do")

    why = _WHY_NOW.get(kind)
    if not why and trigger and trigger.translated:
        why = f"there's {trigger.translated}"
    if not why:
        why = f"{voice.opener(sheet.merchant_id, kind)} — something on your listing worth two minutes"
    plan.why_now = why

    # ---- evidence: the merchant's own numbers first, always ----------------
    evidence: list[str] = []
    if sheet.views is not None:
        line = f"{num(sheet.views)} views on your listing in the last {sheet.window_days} days"
        if sheet.calls is not None:
            line += f" and {num(sheet.calls)} calls off them"
        evidence.append(line)

    cmp_ = ins.comparisons.get("ctr")
    if cmp_:
        direction = "ahead of" if cmp_.ahead else "against"
        evidence.append(f"that's {cmp_.render_mine()} of views turning into action, {direction} "
                        f"the {cmp_.render_peer()} median for {sheet.peer_scope_label}")
    elif ins.momentum:
        evidence.append(ins.momentum)

    plan.evidence = evidence

    # ---- the judgment ------------------------------------------------------
    angle = _pick_supporting_angle(ins)
    if ins.contrarian:
        plan.insight = ins.contrarian
    elif ins.divergence:
        plan.insight = ins.divergence
    elif ins.conversion_gap_actions and ins.actions_now is not None:
        plan.insight = (f"on the traffic you already get that is about {num(ins.actions_now)} "
                        f"listing actions a month against {num(ins.actions_at_peer)} at the "
                        f"local median")
    elif ins.conversion_surplus_actions and ins.actions_now is not None:
        plan.insight = (f"you're already ahead of the local median — about "
                        f"{num(ins.actions_now)} listing actions a month against "
                        f"{num(ins.actions_at_peer)} — so the ceiling here is reach, not the "
                        f"listing")
    elif angle:
        plan.insight = angle.insight
        # An angle that came with a source brought a claim with it; take the claim as well,
        # otherwise the message states the advice without the evidence behind it.
        if angle.citation and angle.evidence:
            plan.evidence.insert(0, angle.evidence[0])
            plan.max_evidence = max(plan.max_evidence, 3)
    elif ins.top_trend:
        plan.insight = (f"searches for \"{ins.top_trend.get('query')}\" are "
                        f"{signed_pct(ins.top_trend.get('delta_yoy'))} year on year and your "
                        f"listing doesn't mention it")

    # ---- a bounded proposal -----------------------------------------------
    fixes = _fixes(sheet, ins)
    if fixes:
        plan.proposal = (f"the two things that move this fastest — {oxford(fixes[:2], 'and')}"
                         if len(fixes) > 1 else
                         f"the one thing that moves this fastest — {fixes[0]}")
        plan.ask = ("Want me to take those on this week?" if len(fixes) > 1
                    else "Want me to get that done this week?")
    else:
        artefact = voice.artefact(sheet.merchant_id, kind)
        plan.proposal = artefact
        plan.ask = f"Want me to get that ready?"

    # A citation is only honest if the message actually used the thing it cites. Attaching
    # a source to a message built purely from the merchant's own numbers reads as a
    # fabricated reference, which is the single worst thing this bot could do.
    if ins.top_digest and ins.top_digest.relevance > 25 and not plan.citation:
        used = _mentions_digest(plan, ins.top_digest)
        if used:
            plan.citation = ins.top_digest.source

    plan.levers = ["specificity", "loss_aversion", "effort_externalisation"]
    plan.because(
        f"trigger payload was {'a placeholder' if trigger and trigger.is_placeholder else 'thin'}, "
        f"so the message is grounded in the merchant's own 30-day numbers and the category "
        f"benchmark rather than in the trigger",
        f"kept the ask to a single bounded action",
    )
    if angle:
        plan.because(f"selected the '{angle.id}' angle as the strongest signal available on this listing")
    return plan


def _mentions_digest(plan: Plan, item) -> bool:
    """Does the drafted message carry a claim that came out of this digest item?"""
    drafted = " ".join([plan.why_now, *plan.evidence, plan.insight, plan.proposal]).lower()
    if not drafted.strip():
        return False
    source_terms = {w for w in (item.title + " " + item.summary + " " + item.actionable).lower()
                    .replace(",", " ").replace(".", " ").split() if len(w) > 5}
    if not source_terms:
        return False
    overlap = sum(1 for w in source_terms if w in drafted)
    return overlap >= 3


def _pick_supporting_angle(ins: Insights):
    for angle in ins.angles:
        if angle.insight:
            return angle
    return None


def _fixes(sheet: FactSheet, ins: Insights) -> list[str]:
    fixes: list[str] = []
    if not sheet.verified:
        fixes.append("getting the listing verified with Google")
    if not sheet.active_offers and ins.suggested_offer:
        fixes.append(f"putting {ins.suggested_offer.title} live on it")
    if sheet.subscription_status == "expired":
        fixes.append("switching the upkeep back on")
    stale = sheet.signal("stale_posts")
    if stale and stale.number:
        fixes.append(f"a post — the last one went up {num(stale.number)} days ago")
    negative = sheet.theme("neg")
    if negative:
        theme = squeeze(str(negative.get("theme", "")).replace("_", " "))
        fixes.append(f"a public reply on the {theme} reviews")
    return fixes


def _template_stem(kind: str) -> str:
    stem = squeeze(kind).replace("-", "_")
    return stem or "nudge"
