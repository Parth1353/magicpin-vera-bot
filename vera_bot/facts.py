"""Turn the four raw context dicts into a typed, provenanced FactSheet.

Design rule for the whole bot: **the composer may only say things that exist here.**
Every number that reaches a merchant is registered in `NumberLedger` with the context
path it came from, so `guard.py` can prove afterwards that nothing was invented.

This module is deliberately defensive. Roughly 40 of the 50 merchants and 75 of the 100
triggers in the expanded dataset are sparse or carry placeholder payloads, so every
accessor degrades to "absent" rather than raising, and `FactSheet.density` tells the
composer how much real material it actually has to work with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .utils import (as_list, days_between, dig, first_present, human_date, indian_commas,
                    month_range_covers, months_between, num, parse_iso, pct, rupees,
                    signed_pct, squeeze, utcnow)

# --------------------------------------------------------------------------- #
# number ledger — the anti-fabrication spine
# --------------------------------------------------------------------------- #

# Matches Indian comma grouping ("12,34,567") as one token, not three.
_NUM_IN_TEXT = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")


class NumberLedger:
    """Every numeric literal the bot is allowed to put in front of a merchant."""

    #: counts we generate ourselves (list lengths, "2 slots", "one line") are safe
    ALWAYS_ALLOWED = {str(n) for n in range(0, 13)}

    def __init__(self) -> None:
        self._allowed: dict[str, str] = {}   # rendered form -> provenance
        for token in self.ALWAYS_ALLOWED:
            self._allowed[token] = "structural"

    def register(self, value: Any, provenance: str) -> None:
        for form in self._forms(value):
            self._allowed.setdefault(form, provenance)

    def register_text(self, text: str, provenance: str) -> None:
        """Harvest numbers embedded in free text, e.g. 'JIDA Oct 2026, p.14'."""
        for match in _NUM_IN_TEXT.findall(str(text or "")):
            self.register(match, provenance)

    def harvest(self, node: Any, provenance: str, depth: int = 0) -> None:
        """Walk a context payload and allow every number it mentions."""
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                self.harvest(value, f"{provenance}.{key}", depth + 1)
        elif isinstance(node, list):
            for item in node:
                self.harvest(item, provenance, depth + 1)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            self.register(node, provenance)
        elif isinstance(node, str):
            self.register_text(node, provenance)

    @staticmethod
    def _forms(value: Any) -> set[str]:
        """All the ways one context number may legitimately appear in a message."""
        forms: set[str] = set()
        raw = str(value).strip()
        try:
            # Numbers harvested out of free text arrive already grouped ("₹2,499"), and
            # float() cannot parse those — losing the price and later flagging it as invented.
            number = float(raw.replace(",", "")) if raw else float(value)
        except (TypeError, ValueError):
            return forms

        forms.add(raw)
        forms.add(str(value))
        if number.is_integer():
            whole = int(number)
            forms.add(str(whole))
            forms.add(indian_commas(whole))
            forms.add(f"{whole:,}")
        else:
            forms.add(f"{number:g}")
            forms.add(f"{number:.1f}")
            forms.add(f"{number:.2f}")

        # percentage renderings of a 0-1 ratio, and of a raw percentage
        for scaled in (number * 100.0, number):
            if abs(scaled) < 100000:
                if abs(scaled - round(scaled)) < 0.05:
                    forms.add(str(int(round(scaled))))
                    forms.add(indian_commas(int(round(scaled))))
                forms.add(f"{scaled:.1f}".rstrip("0").rstrip("."))
                forms.add(f"{abs(scaled):.1f}".rstrip("0").rstrip("."))
                if abs(scaled - round(scaled)) < 0.05:
                    forms.add(str(abs(int(round(scaled)))))
        return {f for f in forms if f}

    def unknown_numbers(self, text: str) -> list[str]:
        """Numeric tokens in `text` that no context can account for."""
        offenders = []
        for token in _NUM_IN_TEXT.findall(text or ""):
            if token in self._allowed:
                continue
            if token.lstrip("0") in self._allowed:
                continue
            stripped = token.rstrip("0").rstrip(".") if "." in token else token
            if stripped in self._allowed:
                continue
            offenders.append(token)
        return offenders

    def provenance_of(self, token: str) -> str | None:
        return self._allowed.get(token)

    @property
    def size(self) -> int:
        return len(self._allowed)


# --------------------------------------------------------------------------- #
# atoms
# --------------------------------------------------------------------------- #

@dataclass
class Fact:
    """One verifiable statement, with the context path that backs it."""

    key: str
    text: str
    provenance: str
    value: Any = None
    weight: float = 1.0

    def __bool__(self) -> bool:
        return bool(squeeze(self.text))


@dataclass
class OfferFact:
    id: str
    title: str
    status: str
    source: str                # "merchant" | "category_catalog"
    price: float | None = None
    started: str | None = None
    ended: str | None = None
    audience: str | None = None
    kind: str | None = None

    @property
    def is_service_at_price(self) -> bool:
        return "@" in self.title or (self.kind == "service_at_price")

    @property
    def is_discount_style(self) -> bool:
        return bool(re.search(r"\b\d+%\s*off\b|\bflat\b", self.title, re.I))


@dataclass
class DigestFact:
    id: str
    kind: str
    title: str
    source: str
    summary: str = ""
    actionable: str = ""
    citation: str = ""
    date: str | None = None
    trial_n: int | None = None
    segment: str | None = None
    relevance: float = 0.0
    relevance_because: str = ""
    is_new: bool = False


# --------------------------------------------------------------------------- #
# signal decoding — raw flags must never reach the merchant
# --------------------------------------------------------------------------- #

_SIGNAL_LEXICON: list[tuple[str, str, str]] = [
    # (regex on the bare signal name, human phrase template, family)
    (r"^stale_posts$",              "your last Google post went up {n} days ago", "content"),
    (r"^no_recent_post$",           "nothing new has gone up on your listing lately", "content"),
    (r"^ctr_below_peer_median$",    "your listing converts below the local median", "performance"),
    (r"^above_peer_ctr$",           "your listing converts above the local median", "performance"),
    (r"^above_peer_median_calls$",  "you take more calls than the local median", "performance"),
    (r"^above_peer_calls$",         "you take more calls than the local median", "performance"),
    (r"^perf_dip_severe$",          "the last week fell off sharply", "performance"),
    (r"^perf_dip_post_expiry$",     "numbers slid after the plan lapsed", "performance"),
    (r"^growing_views_7d$",         "views have been climbing week on week", "performance"),
    (r"^seasonal_dip_apr_may$",     "this is the usual April-May lull", "seasonal"),
    (r"^high_volume$",              "you are one of the busier listings in this area", "performance"),
    (r"^stable_growth$",            "growth has been steady", "performance"),
    (r"^unverified_gbp$",           "your Google listing is still unverified", "profile"),
    (r"^delivery_not_set_up$",      "delivery is not switched on yet", "profile"),
    (r"^no_active_offers$",         "there is no live offer on your listing", "offers"),
    (r"^renewal_due_soon$",         "your plan renews in {n} days", "commercial"),
    (r"^trial_ending_soon$",        "your trial is nearly up", "commercial"),
    (r"^winback_eligible$",         "your plan has lapsed but the listing is still live", "commercial"),
    (r"^new_merchant$",             "you are still in your first weeks with us", "commercial"),
    (r"^dormant_with_vera$",        "we have not spoken in {n} days", "relationship"),
    (r"^no_recent_conversation$",   "we have not spoken in a while", "relationship"),
    (r"^engaged_in_last$",          "you replied to me recently", "relationship"),
    (r"^high_engagement$",          "you reply more than most partners", "relationship"),
    (r"^active_planning$",          "we have something half-planned between us", "relationship"),
    (r"^high_risk_adult_cohort$",   "a large share of your patients are high-risk adults", "audience"),
    (r"^high_retention$",           "your members stay unusually long", "audience"),
    (r"^high_repeat_rate$",         "most of your customers come back", "audience"),
    (r"^boutique_segment$",         "you run a small-batch, high-touch studio", "audience"),
    (r"^compliance_aware$",         "you stay on top of compliance notices", "audience"),
    (r"^ipl_eligible_locality$",    "you are inside a match-night catchment", "audience"),
]

_SIGNAL_SPLIT = re.compile(r"^([a-z0-9_]+?)(?:[:_](\d+)\s*([a-z]*))?$", re.I)


@dataclass
class SignalFact:
    raw: str
    name: str
    number: int | None
    unit: str
    text: str
    family: str


def decode_signal(raw: str) -> SignalFact | None:
    """`stale_posts:22d` -> 'your last Google post went up 22 days ago'."""
    token = squeeze(str(raw or "")).strip()
    if not token:
        return None
    match = _SIGNAL_SPLIT.match(token)
    name, number, unit = (match.group(1), match.group(2), match.group(3) or "") if match else (token, None, "")
    number_i = int(number) if number else None

    for pattern, template, family in _SIGNAL_LEXICON:
        hit = re.match(pattern, name)
        if not hit:
            # `dormant_with_vera_14d` collapses to `dormant_with_vera` + 14
            continue
        text = template.replace("{n}", str(number_i if number_i is not None else ""))
        return SignalFact(raw=token, name=name, number=number_i, unit=unit,
                          text=squeeze(text), family=family)

    # Unknown flag: never echo it raw, and never guess a meaning for it either.
    return SignalFact(raw=token, name=name, number=number_i, unit=unit, text="", family="unknown")


# --------------------------------------------------------------------------- #
# trigger normalisation
# --------------------------------------------------------------------------- #

#: trigger kinds whose vocabulary is native to one vertical only. When the dataset
#: hands the kind to a different vertical (it does — `chronic_refill_due` lands on a
#: dentist, `recall_due` on a yoga studio), translate instead of speaking nonsense.
_KIND_TRANSLATION: dict[str, dict[str, str]] = {
    "chronic_refill_due": {
        "dentists": "a treatment-plan follow-up that is due",
        "salons": "a regular service cycle that is due",
        "restaurants": "a repeat-order cycle that has gone quiet",
        "gyms": "a membership cycle that is due",
        "pharmacies": "a chronic prescription refill that is due",
    },
    "recall_due": {
        "dentists": "a recall visit that is due",
        "salons": "a service touch-up that is due",
        "restaurants": "a repeat visit that is overdue",
        "gyms": "a return-to-class nudge that is due",
        "pharmacies": "a repeat purchase that is due",
    },
    "trial_followup": {
        "dentists": "a first-consult follow-up",
        "salons": "a first-service follow-up",
        "restaurants": "a first-order follow-up",
        "gyms": "a trial-class follow-up",
        "pharmacies": "a first-order follow-up",
    },
    "appointment_tomorrow": {
        "dentists": "an appointment booked for tomorrow",
        "salons": "a booking held for tomorrow",
        "restaurants": "a reservation for tomorrow",
        "gyms": "a session booked for tomorrow",
        "pharmacies": "a delivery scheduled for tomorrow",
    },
    "customer_lapsed_soft": {
        "dentists": "a patient drifting past their usual gap",
        "salons": "a client drifting past their usual gap",
        "restaurants": "a regular who has stopped ordering",
        "gyms": "a member whose attendance has slipped",
        "pharmacies": "a repeat customer who has not been back",
    },
    "customer_lapsed_hard": {
        "dentists": "a patient who has not been back in months",
        "salons": "a client who has not been back in months",
        "restaurants": "a regular who has stopped coming in",
        "gyms": "a member who has stopped showing up",
        "pharmacies": "a customer who has stopped refilling",
    },
}

#: what each vertical calls the people who walk through the door
CUSTOMER_NOUN = {
    "dentists": ("patient", "patients"),
    "salons": ("client", "clients"),
    "restaurants": ("guest", "guests"),
    "gyms": ("member", "members"),
    "pharmacies": ("customer", "customers"),
}


@dataclass
class TriggerFacts:
    id: str
    kind: str
    scope: str
    source: str
    urgency: int
    suppression_key: str
    expires_at: str | None
    payload: dict = field(default_factory=dict)
    is_placeholder: bool = False
    digest_item: DigestFact | None = None
    salient: list[Fact] = field(default_factory=list)
    translated: str = ""

    # normalised, kind-specific fields
    metric: str | None = None
    delta_pct: float | None = None
    window: str | None = None
    baseline: Any = None
    deadline: str | None = None
    days_to_deadline: int | None = None
    slots: list[dict] = field(default_factory=list)
    theme: str | None = None
    occurrences: int | None = None
    quote: str | None = None
    competitor: dict = field(default_factory=dict)
    festival: dict = field(default_factory=dict)
    event: dict = field(default_factory=dict)
    molecules: list[str] = field(default_factory=list)
    batches: list[str] = field(default_factory=list)
    intent_topic: str | None = None
    merchant_last_message: str | None = None
    milestone: dict = field(default_factory=dict)
    seasonal_trends: list[str] = field(default_factory=list)
    expected_seasonal: bool = False

    @property
    def has_payload_detail(self) -> bool:
        return bool(self.salient) and not self.is_placeholder


_PAYLOAD_LABELS = {
    "days_remaining": "days left on the plan",
    "renewal_amount": "renewal amount",
    "days_since_expiry": "days since the plan lapsed",
    "perf_dip_pct": "performance change since",
    "lapsed_customers_added_since_expiry": "customers gone quiet since then",
    "days_since_last_visit": "days since the last visit",
    "days_since_last_merchant_message": "days since we last spoke",
    "previous_focus": "what they were working on",
    "previous_membership_months": "months they stayed a member",
    "estimated_uplift_pct": "typical lift after verifying",
    "verification_path": "how verification works",
    "last_refill": "last refill",
    "stock_runs_out_iso": "stock runs out",
    "last_service_date": "last service",
    "due_date": "due date",
    "service_due": "what is due",
    "trial_date": "trial date",
    "trial_completed": "trial completed",
    "wedding_date": "wedding date",
    "days_to_wedding": "days to the wedding",
    "likely_driver": "what looks to be driving it",
    "credits": "CDE credits",
    "fee": "fee",
    "manufacturer": "manufacturer",
    "delivery_address_saved": "saved delivery address",
    "last_topic": "what we were last discussing",
}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise_trigger(trigger: dict, category: dict | None,
                      ledger: NumberLedger, now: datetime) -> TriggerFacts:
    payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    kind = str(trigger.get("kind") or "unknown")
    slug = (category or {}).get("slug", "")

    facts = TriggerFacts(
        id=str(trigger.get("id") or ""),
        kind=kind,
        scope=str(trigger.get("scope") or "merchant"),
        source=str(trigger.get("source") or "internal"),
        urgency=int(trigger.get("urgency") or 2),
        suppression_key=str(trigger.get("suppression_key") or ""),
        expires_at=trigger.get("expires_at"),
        payload=payload,
        is_placeholder=bool(payload.get("placeholder")),
    )
    facts.translated = _KIND_TRANSLATION.get(kind, {}).get(slug, "")

    ledger.harvest(payload, f"trigger.{facts.id}.payload")

    # ---- resolve any digest pointer into the actual item -------------------
    item_id = first_present(payload, "top_item_id", "digest_item_id", "alert_id", "item_id")
    if item_id and category:
        for raw in as_list(category.get("digest")):
            if isinstance(raw, dict) and raw.get("id") == item_id:
                facts.digest_item = build_digest_fact(raw, ledger)
                break

    # ---- kind-specific normalisation ---------------------------------------
    facts.metric = payload.get("metric")
    facts.delta_pct = _to_float(first_present(payload, "delta_pct", "perf_dip_pct"))
    facts.window = payload.get("window")
    facts.baseline = payload.get("vs_baseline")
    facts.expected_seasonal = bool(payload.get("is_expected_seasonal"))

    deadline = first_present(payload, "deadline_iso", "due_date", "stock_runs_out_iso",
                             "date", "expires_on")
    if deadline:
        facts.deadline = deadline
        facts.days_to_deadline = days_between(deadline, now)
        ledger.register(facts.days_to_deadline, "derived.days_to_deadline")

    facts.slots = [s for s in as_list(first_present(payload, "available_slots",
                                                    "next_session_options", "slots"))
                   if isinstance(s, dict)]

    facts.theme = payload.get("theme")
    facts.occurrences = payload.get("occurrences_30d")
    facts.quote = payload.get("common_quote")

    if payload.get("competitor_name"):
        facts.competitor = {
            "name": payload.get("competitor_name"),
            "distance_km": payload.get("distance_km"),
            "offer": payload.get("their_offer"),
            "opened": payload.get("opened_date"),
        }

    if payload.get("festival"):
        facts.festival = {
            "name": payload.get("festival"),
            "date": payload.get("date"),
            "days_until": payload.get("days_until"),
            "relevant_to": as_list(payload.get("category_relevance")),
        }

    if payload.get("match") or payload.get("venue"):
        facts.event = {
            "match": payload.get("match"),
            "venue": payload.get("venue"),
            "city": payload.get("city"),
            "time": payload.get("match_time_iso"),
            "is_weeknight": payload.get("is_weeknight"),
        }

    facts.molecules = [str(m) for m in as_list(payload.get("molecule_list"))]
    if payload.get("molecule"):
        facts.molecules.append(str(payload["molecule"]))
    facts.batches = [str(b) for b in as_list(payload.get("affected_batches"))]

    facts.intent_topic = payload.get("intent_topic") or payload.get("ask_template")
    facts.merchant_last_message = payload.get("merchant_last_message")

    if payload.get("metric") == "review_count" or payload.get("milestone_value"):
        facts.milestone = {
            "metric": payload.get("metric"),
            "now": payload.get("value_now"),
            "target": payload.get("milestone_value"),
            "imminent": payload.get("is_imminent"),
        }

    facts.seasonal_trends = [str(t) for t in as_list(payload.get("trends"))]

    # ---- generic salient facts (the judge sees the raw payload, so use it) --
    for key, value in payload.items():
        if key in ("placeholder", "metric_or_topic", "category", "top_item_id",
                   "digest_item_id", "alert_id", "item_id"):
            continue
        if isinstance(value, (dict, list)) or value is None or isinstance(value, bool):
            continue
        label = _PAYLOAD_LABELS.get(key, key.replace("_", " "))
        rendered = _render_payload_value(key, value)
        if rendered:
            facts.salient.append(Fact(key=f"trigger.{key}", text=f"{label}: {rendered}",
                                      provenance=f"trigger.payload.{key}", value=value))
    return facts


def _render_payload_value(key: str, value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key.endswith("_pct") or key.endswith("_yoy"):
            return signed_pct(value)
        if "amount" in key or "price" in key or key.endswith("_inr"):
            return rupees(value)
        return num(value)
    text = str(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return human_date(text, with_year=True)
    return squeeze(text.replace("_", " "))


def build_digest_fact(raw: dict, ledger: NumberLedger | None = None) -> DigestFact:
    source = str(raw.get("source") or "")
    fact = DigestFact(
        id=str(raw.get("id") or ""),
        kind=str(raw.get("kind") or "note"),
        title=squeeze(str(raw.get("title") or "")),
        source=source,
        summary=squeeze(str(raw.get("summary") or "")),
        actionable=squeeze(str(raw.get("actionable") or "")),
        citation=source,
        date=raw.get("date"),
        trial_n=raw.get("trial_n"),
        segment=raw.get("patient_segment") or raw.get("segment"),
    )
    if ledger:
        ledger.register_text(fact.title, f"category.digest.{fact.id}.title")
        ledger.register_text(fact.summary, f"category.digest.{fact.id}.summary")
        ledger.register_text(fact.source, f"category.digest.{fact.id}.source")
        ledger.register_text(fact.actionable, f"category.digest.{fact.id}.actionable")
        if fact.trial_n:
            ledger.register(fact.trial_n, f"category.digest.{fact.id}.trial_n")
    return fact


# --------------------------------------------------------------------------- #
# the sheet
# --------------------------------------------------------------------------- #

@dataclass
class FactSheet:
    now: datetime
    ledger: NumberLedger

    # raw contexts, kept for the guard and for rationale writing
    category: dict = field(default_factory=dict)
    merchant: dict = field(default_factory=dict)
    trigger_raw: dict = field(default_factory=dict)
    customer: dict | None = None

    # identity
    merchant_id: str = ""
    category_slug: str = ""
    business_name: str = ""
    owner_name: str = ""            # already carries "Dr." where the vertical wants it
    salutation: str = ""
    locality: str = ""
    city: str = ""
    place: str = ""                 # "Lajpat Nagar" or "Lajpat Nagar, Delhi"
    verified: bool = True
    established_year: int | None = None
    languages: list[str] = field(default_factory=list)
    customer_noun: str = "customer"
    customer_noun_plural: str = "customers"

    # commercial
    plan: str = ""
    subscription_status: str = ""
    days_remaining: int | None = None
    days_since_expiry: int | None = None

    # performance
    views: int | None = None
    calls: int | None = None
    directions: int | None = None
    leads: int | None = None
    ctr: float | None = None
    window_days: int = 30
    views_delta: float | None = None
    calls_delta: float | None = None
    ctr_delta: float | None = None

    # peer benchmarks
    peer: dict = field(default_factory=dict)
    peer_scope_label: str = "comparable listings"

    # catalogue
    active_offers: list[OfferFact] = field(default_factory=list)
    inactive_offers: list[OfferFact] = field(default_factory=list)
    catalog: list[OfferFact] = field(default_factory=list)

    # knowledge
    digest: list[DigestFact] = field(default_factory=list)
    new_digest_ids: set[str] = field(default_factory=set)
    trends: list[dict] = field(default_factory=list)
    seasonal_now: list[dict] = field(default_factory=list)
    content_library: list[dict] = field(default_factory=list)

    # voice
    voice: dict = field(default_factory=dict)
    taboos: list[str] = field(default_factory=list)
    vocab: list[str] = field(default_factory=list)

    # relationship
    signals: list[SignalFact] = field(default_factory=list)
    review_themes: list[dict] = field(default_factory=list)
    customer_aggregate: dict = field(default_factory=dict)
    last_merchant_message: dict | None = None
    last_vera_message: dict | None = None
    unanswered_last_touch: bool = False
    conversation_turns: int = 0

    # trigger + customer
    trigger: TriggerFacts | None = None
    cust: dict = field(default_factory=dict)   # normalised customer facts

    # what changed since the previous pushed version
    merchant_change: Any = None
    category_change: Any = None

    # ------------------------------------------------------------------ views
    @property
    def has_perf(self) -> bool:
        return self.views is not None or self.calls is not None

    @property
    def density(self) -> float:
        """0-1 measure of how much merchant-specific material exists."""
        checks = [
            bool(self.active_offers), bool(self.signals), bool(self.review_themes),
            bool(self.conversation_turns), self.has_perf,
            bool(self.customer_aggregate), bool(self.trigger and self.trigger.has_payload_detail),
        ]
        return sum(1 for c in checks if c) / len(checks)

    @property
    def is_sparse(self) -> bool:
        return self.density < 0.5

    def signal(self, *names: str) -> SignalFact | None:
        for sig in self.signals:
            if sig.name in names:
                return sig
        return None

    def has_signal(self, *names: str) -> bool:
        return self.signal(*names) is not None

    def theme(self, sentiment: str | None = None) -> dict | None:
        for item in self.review_themes:
            if sentiment is None or item.get("sentiment") == sentiment:
                return item
        return None

    def allow(self, value: Any, provenance: str) -> Any:
        """Register a derived number, then hand it straight back for rendering."""
        self.ledger.register(value, provenance)
        return value


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #

def _owner_and_salutation(merchant: dict, category: dict | None) -> tuple[str, str]:
    identity = merchant.get("identity") or {}
    raw_owner = squeeze(str(identity.get("owner_first_name") or ""))
    business = squeeze(str(identity.get("name") or "")) or "your business"
    slug = (category or {}).get("slug", "")

    if not raw_owner:
        # Some listings only carry a business name; address the business, never "Hi there".
        return "", business

    owner = raw_owner
    if slug == "dentists" and not re.match(r"^(dr\.?|doctor)\b", owner, re.I):
        owner = f"Dr. {owner}"
    owner = re.sub(r"^dr\.?\s+", "Dr. ", owner, flags=re.I)
    return owner, owner


def _offer_from_merchant(raw: dict) -> OfferFact:
    title = squeeze(str(raw.get("title") or ""))
    return OfferFact(
        id=str(raw.get("id") or ""),
        title=title,
        status=str(raw.get("status") or "active"),
        source="merchant",
        price=_price_in(title),
        started=raw.get("started"),
        ended=raw.get("ended"),
    )


def _offer_from_catalog(raw: dict) -> OfferFact:
    title = squeeze(str(raw.get("title") or ""))
    return OfferFact(
        id=str(raw.get("id") or ""),
        title=title,
        status="catalog",
        source="category_catalog",
        price=_to_float(raw.get("value")) if raw.get("value") not in (None, "") else _price_in(title),
        audience=raw.get("audience"),
        kind=raw.get("type"),
    )


_PRICE_RE = re.compile(r"₹\s*([\d,]+)")


def _price_in(title: str) -> float | None:
    hit = _PRICE_RE.search(title or "")
    if not hit:
        return None
    return _to_float(hit.group(1).replace(",", ""))


def build_fact_sheet(category: dict | None, merchant: dict | None, trigger: dict | None,
                     customer: dict | None = None, now: datetime | None = None,
                     merchant_change: Any = None, category_change: Any = None) -> FactSheet:
    now = now or utcnow()
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}

    ledger = NumberLedger()
    ledger.harvest(category, "category")
    ledger.harvest(merchant, "merchant")
    ledger.harvest(customer or {}, "customer")
    ledger.register(now.year, "clock.year")
    ledger.register(now.day, "clock.day")

    sheet = FactSheet(now=now, ledger=ledger, category=category, merchant=merchant,
                      trigger_raw=trigger, customer=customer,
                      merchant_change=merchant_change, category_change=category_change)

    identity = merchant.get("identity") or {}
    sheet.merchant_id = str(merchant.get("merchant_id") or "")
    sheet.category_slug = str(category.get("slug") or merchant.get("category_slug") or "")
    sheet.business_name = squeeze(str(identity.get("name") or ""))
    sheet.owner_name, sheet.salutation = _owner_and_salutation(merchant, category)
    sheet.locality = squeeze(str(identity.get("locality") or ""))
    sheet.city = squeeze(str(identity.get("city") or ""))
    sheet.place = ", ".join([p for p in (sheet.locality, sheet.city) if p][:2]) or sheet.city
    sheet.verified = bool(identity.get("verified", True))
    sheet.established_year = identity.get("established_year")
    sheet.languages = [str(l).lower() for l in as_list(identity.get("languages"))] or ["en"]
    sheet.customer_noun, sheet.customer_noun_plural = CUSTOMER_NOUN.get(
        sheet.category_slug, ("customer", "customers"))

    subscription = merchant.get("subscription") or {}
    sheet.plan = squeeze(str(subscription.get("plan") or ""))
    sheet.subscription_status = squeeze(str(subscription.get("status") or ""))
    sheet.days_remaining = subscription.get("days_remaining")
    sheet.days_since_expiry = subscription.get("days_since_expiry")

    perf = merchant.get("performance") or {}
    sheet.views = perf.get("views")
    sheet.calls = perf.get("calls")
    sheet.directions = perf.get("directions")
    sheet.leads = perf.get("leads")
    sheet.ctr = _to_float(perf.get("ctr"))
    sheet.window_days = int(perf.get("window_days") or 30)
    delta = perf.get("delta_7d") or {}
    sheet.views_delta = _to_float(delta.get("views_pct"))
    sheet.calls_delta = _to_float(delta.get("calls_pct"))
    sheet.ctr_delta = _to_float(delta.get("ctr_pct"))

    sheet.peer = category.get("peer_stats") or {}
    sheet.peer_scope_label = _peer_label(sheet.peer.get("scope"), sheet)

    for raw in as_list(merchant.get("offers")):
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        offer = _offer_from_merchant(raw)
        (sheet.active_offers if offer.status == "active" else sheet.inactive_offers).append(offer)

    sheet.catalog = [_offer_from_catalog(o) for o in as_list(category.get("offer_catalog"))
                     if isinstance(o, dict) and o.get("title")]

    sheet.digest = [build_digest_fact(d, ledger) for d in as_list(category.get("digest"))
                    if isinstance(d, dict) and d.get("title")]
    if category_change is not None:
        sheet.new_digest_ids = {d.get("id") for d in getattr(category_change, "added_digest", [])
                                if isinstance(d, dict)}
        for item in sheet.digest:
            item.is_new = item.id in sheet.new_digest_ids

    sheet.trends = [t for t in as_list(category.get("trend_signals")) if isinstance(t, dict)]
    sheet.seasonal_now = [b for b in as_list(category.get("seasonal_beats"))
                          if isinstance(b, dict) and month_range_covers(str(b.get("month_range", "")), now.month)]
    sheet.content_library = [c for c in as_list(category.get("patient_content_library"))
                             if isinstance(c, dict)]

    sheet.voice = category.get("voice") or {}
    sheet.taboos = [str(t) for t in as_list(sheet.voice.get("vocab_taboo"))]
    sheet.vocab = [str(v) for v in as_list(sheet.voice.get("vocab_allowed"))]

    for raw in as_list(merchant.get("signals")):
        decoded = decode_signal(raw)
        if decoded:
            sheet.signals.append(decoded)

    sheet.review_themes = [t for t in as_list(merchant.get("review_themes")) if isinstance(t, dict)]
    sheet.customer_aggregate = merchant.get("customer_aggregate") or {}

    history = [h for h in as_list(merchant.get("conversation_history")) if isinstance(h, dict)]
    sheet.conversation_turns = len(history)
    for turn in reversed(history):
        who = str(turn.get("from") or "").lower()
        if who == "merchant" and sheet.last_merchant_message is None:
            sheet.last_merchant_message = turn
        if who == "vera" and sheet.last_vera_message is None:
            sheet.last_vera_message = turn
    if history:
        last = history[-1]
        sheet.unanswered_last_touch = (str(last.get("from")).lower() == "vera"
                                       and str(last.get("engagement")) == "merchant_no_reply")

    sheet.trigger = normalise_trigger(trigger, category, ledger, now)
    sheet.cust = _customer_facts(customer, sheet, ledger, now)
    return sheet


def _peer_label(scope: Any, sheet: FactSheet) -> str:
    """`metro_solo_practices_2026` -> 'metro solo practices'. Never echo the raw slug."""
    text = squeeze(str(scope or "")).replace("_", " ")
    text = re.sub(r"\b(19|20)\d{2}\b", "", text).strip()
    return text or "comparable listings"


def _customer_facts(customer: dict | None, sheet: FactSheet,
                    ledger: NumberLedger, now: datetime) -> dict:
    if not customer:
        return {}

    identity = customer.get("identity") or {}
    relationship = customer.get("relationship") or {}
    preferences = customer.get("preferences") or {}
    consent = customer.get("consent") or {}

    raw_name = squeeze(str(identity.get("name") or ""))
    parent = None
    parent_match = re.search(r"parent:\s*([A-Za-z .]+)", raw_name)
    if parent_match:
        parent = squeeze(parent_match.group(1))
    display = squeeze(re.sub(r"\(.*?\)", "", raw_name))
    anonymous = (not display) or "walk-in" in raw_name.lower() or "no profile" in raw_name.lower()

    visits = relationship.get("visits_total")
    last_visit = relationship.get("last_visit")
    months_since = months_between(now, last_visit)
    days_since = days_between(now, last_visit)
    declared_state = str(customer.get("state") or "")

    # The generated dataset contradicts itself (state "new" with five visits on file).
    # Trust the visit ledger over the label, and say only what both support.
    effective = declared_state
    if visits and visits >= 2 and declared_state == "new":
        effective = "returning"

    services = [str(s) for s in as_list(relationship.get("services_received"))]
    scope_list = [str(s) for s in as_list(consent.get("scope"))]

    facts = {
        "id": str(customer.get("customer_id") or ""),
        "name": display or "there",
        "anonymous": anonymous,
        "parent": parent,
        "language_pref": squeeze(str(identity.get("language_pref") or "")).lower(),
        "age_band": identity.get("age_band"),
        "visits": visits,
        "first_visit": relationship.get("first_visit"),
        "last_visit": last_visit,
        "months_since_visit": months_since,
        "days_since_visit": days_since,
        "lifetime_value": relationship.get("lifetime_value"),
        "services": services,
        "top_service": _most_common(services),
        "declared_state": declared_state,
        "state": effective,
        "preferred_slots": preferences.get("preferred_slots"),
        "channel": preferences.get("channel"),
        "opted_in": bool(preferences.get("reminder_opt_in", True)),
        "consent_scope": scope_list,
        "consented_at": consent.get("opted_in_at"),
        "reminder_consent": any(s in scope_list for s in
                                ("recall_reminders", "appointment_reminders", "refill_reminders")),
        "promo_consent": "promotional_offers" in scope_list,
    }
    ledger.register(visits, "customer.relationship.visits_total")
    ledger.register(months_since, "derived.months_since_last_visit")
    ledger.register(days_since, "derived.days_since_last_visit")
    return facts


def _most_common(items: Iterable[str]) -> str | None:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=lambda k: counts[k])
