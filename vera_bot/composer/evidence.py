"""Shared evidence fragments, and the specificity floor.

The single biggest scoring lever in this challenge is whether a message carries facts the
merchant can go and check. Every merchant context in the dataset — including the sparse
generated ones — has `identity`, `subscription` and a 30-day `performance` block, and every
category context has a full `peer_stats` benchmark. That is always enough for two verifiable
numbers, so no message is allowed to go out with fewer.
"""

from __future__ import annotations

import re

from ..facts import FactSheet
from ..insights import Insights
from ..utils import human_date, num, pct, signed_pct, squeeze, text

_NUMBER = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")

#: A short, true clause that lands a business term from the category's own vocabulary.
#: Dentists are absent on purpose — their allowed vocabulary is clinical, and forcing
#: clinical words into a listing-performance sentence is exactly the wrong-vocabulary
#: mistake the case studies penalise.
_VOCAB_TAIL = {
    "restaurants": "which is the top of the funnel before it becomes covers",
    "gyms": "which is the footfall that turns into trials",
    "pharmacies": "which is what walks up to the counter",
    "salons": "which is what turns into bookings",
}


def perf_line(sheet: FactSheet, with_vocab: bool = False) -> str:
    """The merchant's own numbers, phrased so they can be checked against the dashboard."""
    if sheet.views is None:
        return ""
    line = f"{num(sheet.views)} views on your listing in the last {sheet.window_days} days"
    if sheet.calls is not None:
        line += f" and {num(sheet.calls)} calls off them"
    tail = _VOCAB_TAIL.get(sheet.category_slug) if with_vocab else None
    return f"{line} — {tail}" if tail else line


def peer_line(sheet: FactSheet, ins: Insights) -> str:
    comparison = ins.comparisons.get("ctr")
    if not comparison:
        return ""
    direction = "ahead of" if comparison.ahead else "against"
    return (f"that's {comparison.render_mine()} of views turning into action, {direction} the "
            f"{comparison.render_peer()} median for {sheet.peer_scope_label}")


def momentum_line(sheet: FactSheet, ins: Insights) -> str:
    if ins.momentum:
        return f"week on week your {ins.momentum.replace(' week on week', '')}"
    return ""


def volume_line(sheet: FactSheet) -> str:
    aggregate = sheet.customer_aggregate or {}
    for key, phrasing in (("total_active_members", "you have {n} active {noun} on the books"),
                          ("chronic_rx_count", "you have {n} chronic-prescription {noun} on file"),
                          ("total_unique_ytd", "{n} different {noun} have come through this year")):
        if aggregate.get(key):
            return phrasing.format(n=num(aggregate[key]), noun=sheet.customer_noun_plural)
    return ""


def subscription_line(sheet: FactSheet) -> str:
    if sheet.subscription_status == "expired" and sheet.days_since_expiry:
        return (f"your plan lapsed {num(sheet.days_since_expiry)} days ago and the listing has "
                f"been running unattended since")
    if sheet.subscription_status == "trial" and sheet.days_remaining is not None:
        return f"you have {num(sheet.days_remaining)} days left on the trial"
    if sheet.subscription_status == "active" and sheet.days_remaining is not None \
            and sheet.days_remaining <= 30:
        return f"your {text(sheet.plan, 'plan')} renews in {num(sheet.days_remaining)} days"
    return ""


def trend_line(sheet: FactSheet, ins: Insights) -> str:
    trend = ins.top_trend
    if not trend:
        return ""
    band = text(trend.get("segment_age"))
    suffix = f", concentrated in the {band} band" if band and band != "all" else ""
    return (f"searches for \"{trend.get('query')}\" are {signed_pct(trend.get('delta_yoy'))} "
            f"year on year{suffix}")


def customer_floor_evidence(sheet: FactSheet, ins: Insights, exclude: str = "") -> list[str]:
    """The checkable facts a *customer* can verify.

    Listing analytics are meaningless to the person on the other end of a recall reminder —
    "2,410 views" is the merchant's number, not theirs. What a customer can check is their
    own history with the business and the price of what is being offered.
    """
    cust = sheet.cust or {}
    lowered = (exclude or "").lower()
    candidates: list[str] = []

    visits, first = cust.get("visits"), cust.get("first_visit")
    service = text(cust.get("top_service"))
    if visits and visits >= 2:
        line = f"you've been in {num(visits)} times"
        if first:
            line += f" since {human_date(first, with_year=True)}"
        if service:
            line += f", mostly for {service}"
        candidates.append(line)
    if cust.get("last_visit"):
        candidates.append(f"your last visit with us was "
                          f"{human_date(cust['last_visit'], with_year=True)}")

    live = [o for o in sheet.active_offers if o.title]
    if live:
        candidates.append(f"{live[0].title} is what we have running at the moment")

    months = cust.get("months_since_visit")
    if months and months >= 2:
        candidates.append(f"that's about {num(months)} months")

    out = []
    for candidate in candidates:
        clean = squeeze(candidate)
        if not clean or clean.split(" ")[0].lower() in lowered:
            continue
        if clean.lower()[:24] in lowered:
            continue
        out.append(clean)
    return out


def count_numbers(*chunks: str) -> int:
    """How many distinct numeric facts a draft is carrying."""
    found: set[str] = set()
    for chunk in chunks:
        found.update(_NUMBER.findall(chunk or ""))
    return len(found)


def floor_evidence(sheet: FactSheet, ins: Insights, exclude: str = "") -> list[str]:
    """Ordered candidates for topping a message up to the specificity floor."""
    lowered = (exclude or "").lower()
    candidates = [
        perf_line(sheet, with_vocab=True),
        peer_line(sheet, ins),
        volume_line(sheet),
        subscription_line(sheet),
        trend_line(sheet, ins),
        momentum_line(sheet, ins),
    ]
    out = []
    for candidate in candidates:
        clean = squeeze(candidate)
        if not clean:
            continue
        head = clean.split(" ")[0].lower()
        if head and head in lowered:
            continue
        out.append(clean)
    return out
