"""Derived judgment: the difference between templating and having a point of view.

Nothing in here invents data. Every number is arithmetic over two context numbers, and
the result is registered in the FactSheet's ledger together with the inputs, so the
message can always show its working ("2,410 views x the 3.0% local median = ~22 more
actions a month"). That is what makes a derived number verifiable rather than fabricated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .facts import DigestFact, FactSheet, OfferFact
from .utils import (as_list, human_date, indian_commas, month_range_covers, num, pct,
                    rupees, signed_pct, squeeze, token_set)

_DIP_WORDS = ("dip", "lull", "low", "drop", "slow", "taper", "lowest", "off-season", "slowdown")
#: words too generic to count as a real match between a search trend and a listing
_WEAK_TERMS = frozenset({"free", "near", "price", "cost", "best", "offer", "offers", "new",
                         "the", "and", "for", "with", "your", "get", "buy", "one", "combo",
                         "pack", "plan", "day", "days", "month", "care", "service", "services"})
_PEAK_WORDS = ("peak", "surge", "spike", "rush", "2x", "3x", "4x", "high", "boom")


# --------------------------------------------------------------------------- #
# comparisons
# --------------------------------------------------------------------------- #

@dataclass
class Comparison:
    """One merchant metric held against its category benchmark."""

    metric: str
    label: str
    mine: float
    peer: float
    unit: str = "count"          # "count" | "ratio"
    better_is_high: bool = True

    @property
    def gap_ratio(self) -> float:
        if not self.peer:
            return 0.0
        return (self.mine - self.peer) / self.peer

    @property
    def ahead(self) -> bool:
        return self.mine >= self.peer if self.better_is_high else self.mine <= self.peer

    @property
    def magnitude(self) -> float:
        return abs(self.gap_ratio)

    def render_mine(self) -> str:
        return pct(self.mine, 1) if self.unit == "ratio" else num(int(round(self.mine)))

    def render_peer(self) -> str:
        return pct(self.peer, 1) if self.unit == "ratio" else num(int(round(self.peer)))


@dataclass
class Angle:
    """A candidate thing to say, scored against every other candidate."""

    id: str
    score: float
    why_now: str = ""                       # the trigger-anchored opening
    evidence: list[str] = field(default_factory=list)
    insight: str = ""                       # the judgment the merchant could not compute
    proposal: str = ""                      # the work Vera offers to do
    citation: str = ""
    levers: list[str] = field(default_factory=list)
    cta_kind: str = "binary_yes_no"
    tags: set[str] = field(default_factory=set)


@dataclass
class Insights:
    sheet: FactSheet
    comparisons: dict[str, Comparison] = field(default_factory=dict)

    # headline derived numbers
    conversion_gap_actions: int | None = None     # extra monthly actions at peer conversion
    conversion_surplus_actions: int | None = None
    actions_now: int | None = None                # views x ctr, the listing's monthly actions
    actions_at_peer: int | None = None            # views x peer ctr
    lapsed_count: int | None = None
    lapsed_label: str = ""
    lapsed_value: int | None = None
    lapsed_unit_price: float | None = None
    post_gap_days: int | None = None
    retention_gap: float | None = None

    # knowledge picks
    top_digest: DigestFact | None = None
    fresh_digest: DigestFact | None = None
    ranked_digest: list[DigestFact] = field(default_factory=list)
    top_trend: dict | None = None
    season: dict | None = None
    season_mood: str = ""                          # "dip" | "peak" | ""

    # catalogue picks
    lead_offer: OfferFact | None = None
    suggested_offer: OfferFact | None = None
    discount_offer_to_replace: OfferFact | None = None

    # reads
    divergence: str = ""                           # views vs calls moving opposite ways
    contrarian: str = ""                           # when the obvious play is the wrong one
    movement: str = ""                             # what changed since the last push
    momentum: str = ""                             # this-week direction in plain words

    angles: list[Angle] = field(default_factory=list)

    def best_angle(self, exclude: set[str] | None = None) -> Angle | None:
        exclude = exclude or set()
        for angle in self.angles:
            if angle.id not in exclude:
                return angle
        return None


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def derive(sheet: FactSheet) -> Insights:
    ins = Insights(sheet=sheet)
    _compare_to_peers(sheet, ins)
    _read_momentum(sheet, ins)
    _value_the_lapsed_list(sheet, ins)
    _rank_knowledge(sheet, ins)
    _pick_offers(sheet, ins)
    _read_movement(sheet, ins)
    _find_contrarian(sheet, ins)
    _build_angles(sheet, ins)
    return ins


# --------------------------------------------------------------------------- #
# peers
# --------------------------------------------------------------------------- #

def _compare_to_peers(sheet: FactSheet, ins: Insights) -> None:
    peer = sheet.peer or {}

    def add(metric: str, label: str, mine: Any, benchmark: Any, unit: str = "count") -> None:
        if mine is None or benchmark in (None, 0):
            return
        try:
            ins.comparisons[metric] = Comparison(metric, label, float(mine), float(benchmark), unit)
        except (TypeError, ValueError):
            return

    add("ctr", "listing conversion", sheet.ctr, peer.get("avg_ctr"), unit="ratio")
    add("views", "profile views", sheet.views, peer.get("avg_views_30d"))
    add("calls", "calls", sheet.calls, peer.get("avg_calls_30d"))
    add("directions", "direction requests", sheet.directions, peer.get("avg_directions_30d"))

    ctr_cmp = ins.comparisons.get("ctr")
    if ctr_cmp and sheet.views:
        # CTR is actions / views. Quoting only the *gap* ("83 more calls" against a listing
        # taking 7) reads as invented even when the arithmetic is right, so both totals are
        # computed and the message shows the before and after rather than the delta alone.
        ins.actions_now = sheet.allow(int(round(ctr_cmp.mine * float(sheet.views))),
                                      "derived.views*ctr")
        ins.actions_at_peer = sheet.allow(int(round(ctr_cmp.peer * float(sheet.views))),
                                          "derived.views*peer_ctr")
        swing = (ctr_cmp.peer - ctr_cmp.mine) * float(sheet.views)
        if swing > 0.5:
            ins.conversion_gap_actions = sheet.allow(int(round(swing)),
                                                     "derived.views*(peer_ctr-ctr)")
        elif swing < -0.5:
            ins.conversion_surplus_actions = sheet.allow(int(round(-swing)),
                                                         "derived.views*(ctr-peer_ctr)")

    # posting cadence against the category's own norm
    stale = sheet.signal("stale_posts")
    cadence = peer.get("avg_post_freq_days")
    if stale and stale.number and cadence:
        gap = int(stale.number) - int(cadence)
        if gap > 0:
            ins.post_gap_days = sheet.allow(gap, "derived.stale_posts-peer_post_freq")

    for key in ("retention_6mo_pct", "retention_3mo_pct", "repeat_customer_pct"):
        mine = sheet.customer_aggregate.get(key)
        benchmark = peer.get(key)
        if mine is not None and benchmark:
            ins.retention_gap = round(float(mine) - float(benchmark), 3)
            sheet.allow(abs(ins.retention_gap), f"derived.{key}_gap")
            break


def _read_momentum(sheet: FactSheet, ins: Insights) -> None:
    views_d, calls_d = sheet.views_delta, sheet.calls_delta
    if views_d is None and calls_d is None:
        return

    parts = []
    if views_d is not None:
        parts.append(f"views {signed_pct(views_d)}")
    if calls_d is not None:
        parts.append(f"calls {signed_pct(calls_d)}")
    ins.momentum = " and ".join(parts) + " week on week" if parts else ""

    if views_d is not None and calls_d is not None:
        if views_d <= -0.05 and calls_d >= 0.05:
            ins.divergence = ("fewer people are landing on the listing but more of them are "
                              "picking up the phone — the traffic you are getting is better, "
                              "not worse")
        elif views_d >= 0.05 and calls_d <= -0.05:
            ins.divergence = ("more people are seeing the listing and fewer are acting on it — "
                              "that is a listing problem, not a demand problem")


def _value_the_lapsed_list(sheet: FactSheet, ins: Insights) -> None:
    aggregate = sheet.customer_aggregate or {}
    for key, label in (("lapsed_180d_plus", "past six months"),
                       ("lapsed_90d_plus", "past three months"),
                       ("lapsed_customers", "gone quiet")):
        if aggregate.get(key):
            ins.lapsed_count = int(aggregate[key])
            ins.lapsed_label = label
            break
    if ins.lapsed_count is None:
        return

    price = None
    for offer in sheet.active_offers + sheet.inactive_offers:
        if offer.price:
            price = offer.price
            break
    if price is None:
        prices = sorted(o.price for o in sheet.catalog if o.price)
        if prices:
            price = prices[len(prices) // 2]
    if not price:
        return

    ins.lapsed_unit_price = price
    # Rounded down to the nearest 500 so it reads as the estimate it is.
    raw = ins.lapsed_count * price
    ins.lapsed_value = sheet.allow(int(raw // 500 * 500) or int(raw),
                                   "derived.lapsed_count*offer_price")
    sheet.allow(int(raw), "derived.lapsed_count*offer_price.exact")


# --------------------------------------------------------------------------- #
# knowledge ranking
# --------------------------------------------------------------------------- #

_KIND_AFFINITY = {
    "research_digest": {"research": 30, "trend": 10, "tech": 8, "cde": 5},
    "regulation_change": {"compliance": 40, "research": 8},
    "compliance_alert": {"compliance": 40},
    "supply_alert": {"alert": 40, "supply": 30, "compliance": 15},
    "cde_opportunity": {"cde": 40, "tech": 10},
    "category_trend_movement": {"trend": 35, "tech": 10},
    "competitor_opened": {"compete": 30, "trend": 20, "tech": 10},
    "category_seasonal": {"seasonal": 40, "trend": 12},
    "seasonal_perf_dip": {"seasonal": 35, "trend": 10},
    "perf_dip": {"trend": 18, "tech": 12, "seasonal": 12},
    "perf_spike": {"trend": 20, "seasonal": 12},
    "festival_upcoming": {"seasonal": 30, "trend": 10},
    "ipl_match_today": {"seasonal": 35, "trend": 15},
    "review_theme_emerged": {"tech": 20, "trend": 10},
    "gbp_unverified": {"trend": 25, "tech": 15},
    "milestone_reached": {"trend": 15},
    "dormant_with_vera": {"trend": 15, "research": 10, "tech": 10},
    "curious_ask_due": {"trend": 20, "seasonal": 15},
    "winback_eligible": {"trend": 18, "tech": 12},
    "renewal_due": {"trend": 15, "tech": 12},
}


def _rank_knowledge(sheet: FactSheet, ins: Insights) -> None:
    trigger = sheet.trigger
    kind = trigger.kind if trigger else ""
    affinity = _KIND_AFFINITY.get(kind, {})

    merchant_terms = token_set(" ".join(filter(None, [
        sheet.business_name, sheet.locality, sheet.city,
        " ".join(o.title for o in sheet.active_offers + sheet.inactive_offers),
        " ".join(s.name.replace("_", " ") for s in sheet.signals),
        " ".join(str(t.get("theme", "")).replace("_", " ") for t in sheet.review_themes),
    ])))

    for item in sheet.digest:
        score = 1.0
        because = []

        if trigger and trigger.digest_item and item.id == trigger.digest_item.id:
            score += 100
            because.append("named by the trigger")
        if item.is_new:
            score += 30
            because.append("just landed in this week's digest")
        score += affinity.get(item.kind, 0)

        text_terms = token_set(f"{item.title} {item.summary} {item.actionable}")
        overlap = len(text_terms & merchant_terms)
        if overlap:
            score += min(18, overlap * 4)
            because.append("overlaps what this listing already sells")

        if sheet.city and sheet.city.lower() in f"{item.title} {item.summary}".lower():
            score += 12
            because.append(f"names {sheet.city}")

        if item.kind in ("compliance", "alert") and re.search(r"\d{4}-\d{2}-\d{2}", item.title + item.summary):
            score += 12
            because.append("carries a hard deadline")

        if item.kind == "seasonal":
            score += 8 if _season_matches_now(sheet, item) else -4

        if item.source:
            score += 4      # citable items beat uncitable ones

        item.relevance = score
        item.relevance_because = "; ".join(because)

    ins.ranked_digest = sorted(sheet.digest, key=lambda d: (-d.relevance, d.id))
    ins.top_digest = ins.ranked_digest[0] if ins.ranked_digest else None
    ins.fresh_digest = next((d for d in ins.ranked_digest if d.is_new), None)

    # seasonal beat for the current month
    if sheet.seasonal_now:
        ins.season = sheet.seasonal_now[0]
        note = str(ins.season.get("note", "")).lower()
        if any(w in note for w in _DIP_WORDS):
            ins.season_mood = "dip"
        elif any(w in note for w in _PEAK_WORDS):
            ins.season_mood = "peak"

    # trend signal most connected to this merchant and to why we are messaging
    trigger_terms = token_set(" ".join(filter(None, [
        kind.replace("_", " "),
        str(trigger.intent_topic or "") if trigger else "",
        " ".join(str(v) for v in (trigger.payload or {}).values()
                 if isinstance(v, str)) if trigger else "",
    ])))
    best, best_score = None, 0.0
    for trend in sheet.trends:
        query = str(trend.get("query", ""))
        score = float(trend.get("delta_yoy") or 0) * 10
        terms = token_set(query) - _WEAK_TERMS
        if terms & (merchant_terms - _WEAK_TERMS):
            score += 12
        if terms & (trigger_terms - _WEAK_TERMS):
            score += 14
        if sheet.city and sheet.city.lower() in query.lower():
            score += 10
        if score > best_score:
            best, best_score = trend, score
    ins.top_trend = best


def _season_matches_now(sheet: FactSheet, item: DigestFact) -> bool:
    blob = f"{item.title} {item.summary}"
    for beat in sheet.seasonal_now:
        if token_set(str(beat.get("note", ""))) & token_set(blob):
            return True
    return bool(re.search(r"\b(apr|april|may|jun|june)\b", blob, re.I)) and sheet.now.month in (4, 5, 6)


# --------------------------------------------------------------------------- #
# catalogue
# --------------------------------------------------------------------------- #

def _pick_offers(sheet: FactSheet, ins: Insights) -> None:
    for offer in sheet.active_offers:
        if offer.is_discount_style and not offer.is_service_at_price:
            ins.discount_offer_to_replace = offer
    ins.lead_offer = next((o for o in sheet.active_offers if o.is_service_at_price),
                          sheet.active_offers[0] if sheet.active_offers else None)

    if ins.lead_offer and not ins.discount_offer_to_replace:
        return

    # Nothing live (or only a blunt discount): recommend from the category catalogue,
    # preferring a service-at-price item that lines up with the strongest local trend.
    trend_terms = token_set(str((ins.top_trend or {}).get("query", "")))
    best, best_score = None, -1.0
    for candidate in sheet.catalog:
        if any(candidate.title == o.title for o in sheet.active_offers):
            continue
        score = 0.0
        if candidate.is_service_at_price:
            score += 6
        if candidate.kind == "free_service":
            score += 3
        if trend_terms & token_set(candidate.title):
            score += 8
        if candidate.audience == "new_user":
            score += 2
        if candidate.price and candidate.price <= 999:
            score += 3          # low-ticket entry offers convert on a listing
        if score > best_score:
            best, best_score = candidate, score
    ins.suggested_offer = best


# --------------------------------------------------------------------------- #
# adaptation + contrarian reads
# --------------------------------------------------------------------------- #

def _read_movement(sheet: FactSheet, ins: Insights) -> None:
    change = sheet.merchant_change
    if change is None or not getattr(change, "metrics", None):
        return
    parts = []
    for label, (before, after) in list(change.metrics.items())[:2]:
        if isinstance(before, float) or isinstance(after, float):
            if 0 < float(after) < 1:
                parts.append(f"{label} moved from {pct(before, 1)} to {pct(after, 1)}")
                continue
        parts.append(f"{label} moved from {num(before)} to {num(after)}")
        sheet.allow(before, "context_change.before")
        sheet.allow(after, "context_change.after")
    ins.movement = "; ".join(parts)


def _find_contrarian(sheet: FactSheet, ins: Insights) -> None:
    trigger = sheet.trigger
    if not trigger:
        return
    kind = trigger.kind

    # 1. A dip the calendar already explains is not a dip worth spending against.
    if kind in ("seasonal_perf_dip", "perf_dip") and (trigger.expected_seasonal
                                                      or ins.season_mood == "dip"):
        ins.contrarian = ("this is the calendar rather than your listing, so holding the "
                          "budget beats spending into it")
        return

    # 2. The digest may say the obvious match-night promo is the wrong call.
    if kind == "ipl_match_today":
        weeknight = trigger.event.get("is_weeknight")
        evidence = next((d for d in ins.ranked_digest
                         if "ipl" in f"{d.id} {d.title}".lower()), None)
        if weeknight is False and evidence:
            ins.contrarian = ("a weekend match is the one to sit out — the order data has "
                              "Saturday matches pulling covers down, not up")
        elif weeknight and evidence:
            ins.contrarian = "weeknight matches are the ones that actually add covers"
        return

    # 3. A spike is only useful if you can name what caused it and repeat it.
    if kind == "perf_spike":
        driver = trigger.payload.get("likely_driver")
        if driver:
            ins.contrarian = (f"worth locking in — it traces back to "
                              f"{squeeze(str(driver).replace('_', ' '))}, which is repeatable")
        return

    # 4. Above-benchmark listings should not be sold a fix they do not need.
    ctr_cmp = ins.comparisons.get("ctr")
    if ctr_cmp and ctr_cmp.ahead and ctr_cmp.magnitude > 0.15:
        ins.contrarian = ("your conversion is already ahead of the local median, so the ceiling "
                          "here is reach, not the listing itself")


# --------------------------------------------------------------------------- #
# angles
# --------------------------------------------------------------------------- #

def _build_angles(sheet: FactSheet, ins: Insights) -> None:
    """Rank every honest thing the bot could lead with for this merchant."""
    angles: list[Angle] = []
    trigger = sheet.trigger

    ctr_cmp = ins.comparisons.get("ctr")
    if ins.conversion_gap_actions and ctr_cmp:
        angles.append(Angle(
            id="conversion_gap",
            score=7.0 + min(3.0, ctr_cmp.magnitude * 4),
            evidence=[f"{num(sheet.views)} views in {sheet.window_days} days converting at "
                      f"{ctr_cmp.render_mine()} against a {ctr_cmp.render_peer()} "
                      f"{sheet.peer_scope_label} median"],
            insight=f"on that traffic it is the difference between about "
                    f"{num(ins.actions_now)} listing actions a month and "
                    f"{num(ins.actions_at_peer)} — calls, directions and leads together",
            levers=["loss_aversion", "social_proof", "specificity"],
            tags={"performance"},
        ))

    if ins.lapsed_count and ins.lapsed_value:
        angles.append(Angle(
            id="lapsed_list",
            score=6.5,
            evidence=[f"{num(ins.lapsed_count)} {sheet.customer_noun_plural} on your list have "
                      f"not been back in the {ins.lapsed_label}"],
            insight=f"at your {rupees(ins.lapsed_unit_price)} entry service that is about "
                    f"{rupees(ins.lapsed_value)} of repeat work sitting idle",
            levers=["loss_aversion", "specificity"],
            tags={"retention"},
        ))

    if ins.top_digest and ins.top_digest.relevance > 20:
        item = ins.top_digest
        angles.append(Angle(
            id="digest",
            score=6.0 + min(4.0, item.relevance / 30.0),
            evidence=[item.title],
            insight=item.actionable or item.summary,
            citation=item.source,
            levers=["specificity", "reciprocity", "curiosity"],
            tags={"knowledge"},
        ))

    if not sheet.active_offers and ins.suggested_offer:
        angles.append(Angle(
            id="no_offer",
            score=6.2,
            evidence=["there is nothing live on your listing for someone to act on right now"],
            insight=f"{ins.suggested_offer.title} is the entry offer that pulls in this category",
            levers=["loss_aversion", "effort_externalisation"],
            tags={"offers"},
        ))

    if not sheet.verified:
        uplift = (trigger.payload.get("estimated_uplift_pct") if trigger else None)
        insight = "unverified listings get held back in local results until Google confirms them"
        if uplift:
            insight = (f"verified listings in this category typically pick up "
                       f"{pct(uplift)} more visibility once Google confirms them")
        angles.append(Angle(
            id="unverified",
            score=6.8,
            evidence=["your Google listing is still showing as unverified"],
            insight=insight,
            levers=["loss_aversion", "effort_externalisation"],
            tags={"profile"},
        ))

    if ins.post_gap_days:
        stale = sheet.signal("stale_posts")
        angles.append(Angle(
            id="stale_posts",
            score=5.4,
            evidence=[f"your last Google post went up {num(stale.number)} days ago against a "
                      f"{num(sheet.peer.get('avg_post_freq_days'))}-day cadence for "
                      f"{sheet.peer_scope_label}"],
            insight="listings that go quiet drift down local results",
            levers=["social_proof", "effort_externalisation"],
            tags={"content"},
        ))

    negative = sheet.theme("neg")
    if negative:
        angles.append(Angle(
            id="review_theme",
            score=5.8,
            evidence=[f"{num(negative.get('occurrences_30d'))} reviews this month raised "
                      f"{squeeze(str(negative.get('theme', '')).replace('_', ' '))}"],
            insight="one recurring complaint costs more reach than a dozen good reviews add",
            levers=["loss_aversion", "reciprocity"],
            tags={"reputation"},
        ))

    if ins.top_trend:
        trend = ins.top_trend
        angles.append(Angle(
            id="trend",
            score=5.0 + float(trend.get("delta_yoy") or 0) * 2,
            evidence=[f"searches for \"{trend.get('query')}\" are "
                      f"{signed_pct(trend.get('delta_yoy'))} year on year"
                      + (f" in the {trend.get('segment_age')} band"
                         if trend.get("segment_age") and trend.get("segment_age") != "all" else "")],
            insight="demand is moving before the listings in this area are",
            levers=["curiosity", "social_proof"],
            tags={"demand"},
        ))

    if ins.season and ins.season_mood:
        angles.append(Angle(
            id="season",
            score=4.6,
            evidence=[f"{ins.season.get('month_range')} is "
                      f"{squeeze(str(ins.season.get('note', '')).split('—')[0])} in this category"],
            insight="planning against the calendar beats reacting to it",
            levers=["specificity"],
            tags={"seasonal"},
        ))

    if sheet.subscription_status == "expired" and sheet.days_since_expiry:
        angles.append(Angle(
            id="lapsed_plan",
            score=5.6,
            evidence=[f"your plan lapsed {num(sheet.days_since_expiry)} days ago and the listing "
                      f"has been running without upkeep since"],
            insight="the listing keeps taking views either way; it just stops being worked on",
            levers=["loss_aversion"],
            tags={"commercial"},
        ))

    ins.angles = sorted(angles, key=lambda a: (-a.score, a.id))
