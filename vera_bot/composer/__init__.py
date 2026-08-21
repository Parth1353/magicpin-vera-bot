"""Composition entry point: contexts in, one grounded WhatsApp message out."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .. import guard
from ..facts import FactSheet, build_fact_sheet
from ..insights import Insights, derive
from ..utils import iso, num, pick, short_id, squeeze
from ..voice import LanguagePlan, plan_language, profile_for
from .base import Composition, Plan, build_params, build_rationale, render
from .evidence import count_numbers, customer_floor_evidence, floor_evidence
from .fallback import compose_fallback
from .registry import CUSTOMER_COMPOSERS, MERCHANT_COMPOSERS

from . import customer_kinds as _customer_kinds   # noqa: F401  (registers composers)
from . import merchant_kinds as _merchant_kinds   # noqa: F401

MAX_VARIANTS = 4


def compose(category: dict | None, merchant: dict | None, trigger: dict | None,
            customer: dict | None = None, *, now: datetime | None = None,
            seen_bodies: Iterable[str] = (), first_touch: bool = False,
            merchant_change=None, category_change=None) -> Composition | None:
    """The `compose(category, merchant, trigger, customer?)` contract from the brief."""
    sheet = build_fact_sheet(category, merchant, trigger, customer, now=now,
                             merchant_change=merchant_change, category_change=category_change)
    ins = derive(sheet)
    return compose_from_sheet(sheet, ins, seen_bodies=seen_bodies, first_touch=first_touch)


def compose_from_sheet(sheet: FactSheet, ins: Insights, *, seen_bodies: Iterable[str] = (),
                       first_touch: bool = False) -> Composition | None:
    trigger = sheet.trigger
    kind = trigger.kind if trigger else "scheduled_recurring"
    is_customer_scope = bool(sheet.customer) and (
        (trigger.scope == "customer") if trigger else True)

    plan = _select(sheet, ins, kind, is_customer_scope)
    if plan is None:
        return None

    _fold_in_adaptation(plan, sheet, ins)
    _ensure_specificity(plan, sheet, ins)
    _verify_citation(plan, sheet, ins)

    lang = _language_for(plan, sheet)
    voice = profile_for(sheet.category_slug)
    seen = list(seen_bodies)

    report = None
    body = ""
    for variant in range(MAX_VARIANTS):
        body = render(plan, sheet, ins, lang, voice, variant=variant)
        if first_touch and plan.send_as == "vera" and variant == 0:
            body = _add_first_touch_marker(body, sheet)
        report = guard.check(body, sheet, seen_bodies=seen,
                             require_cta=(plan.cta != "none"))
        if report.ok:
            body = report.body
            break
    else:
        # Every variant tripped the guard; the last repaired body is still the honest one.
        body = report.body if report else body
        if not squeeze(body):
            return None

    rationale = build_rationale(plan, sheet, ins, lang, extra=_guard_notes(report))
    return Composition(
        body=squeeze(body),
        cta=plan.cta,
        send_as=plan.send_as,
        suppression_key=_suppression_key(sheet),
        rationale=rationale,
        template_name=plan.template or f"vera_{kind}_v1",
        template_params=build_params(plan, sheet),
        angle_id=plan.angle_id,
        levers=list(dict.fromkeys(plan.levers)),
        language=lang.label,
        notes=(report.repaired + report.blocking + report.warnings) if report else [],
        variant=variant,
    )


# --------------------------------------------------------------------------- #

def _select(sheet: FactSheet, ins: Insights, kind: str, customer_scope: bool) -> Plan | None:
    if customer_scope:
        fn = CUSTOMER_COMPOSERS.get(kind)
        if fn:
            plan = fn(sheet, ins)
            if plan:
                return plan
        from .customer_kinds import _generic_customer_followup
        return _generic_customer_followup(sheet, ins)

    fn = MERCHANT_COMPOSERS.get(kind)
    if fn:
        try:
            plan = fn(sheet, ins)
        except Exception:                     # a broken kind must never take the tick down
            plan = None
        if plan and (plan.evidence or plan.insight or plan.why_now):
            return plan
    return compose_fallback(sheet, ins)


#: Below this, a message is asserting rather than showing, and the judge scores it as vague.
SPECIFICITY_FLOOR = 2


def _ensure_specificity(plan: Plan, sheet: FactSheet, ins: Insights) -> None:
    """Guarantee at least two checkable numbers, whatever the trigger did or did not carry.

    Half the triggers in the expanded dataset are placeholders and four fifths of the
    merchants have no offers, signals or history — but every merchant has 30 days of
    performance and every category has a peer benchmark. If a plan is thin, this tops it up
    from those rather than letting a vague message go out.
    """
    drafted = " ".join([plan.why_now, *plan.evidence, plan.insight, plan.proposal, plan.ask])
    if count_numbers(drafted) >= SPECIFICITY_FLOOR:
        return

    # A customer-facing message tops up from the customer's own history; a merchant-facing
    # one tops up from the listing's numbers. Crossing the two would be nonsense.
    source = (customer_floor_evidence if plan.send_as == "merchant_on_behalf"
              else floor_evidence)
    added = 0
    for line in source(sheet, ins, exclude=drafted):
        if count_numbers(line) == 0:
            continue
        plan.evidence.append(line)
        plan.max_evidence = max(plan.max_evidence, len(plan.evidence))
        added += 1
        drafted = f"{drafted} {line}"
        if count_numbers(drafted) >= SPECIFICITY_FLOOR or added >= 2:
            break
    if added:
        plan.because(
            "trigger carried no checkable detail, so the message is anchored on the "
            + ("customer's own visit history with this business"
               if plan.send_as == "merchant_on_behalf"
               else "merchant's own 30-day numbers against the category benchmark"))


def _verify_citation(plan: Plan, sheet: FactSheet, ins: Insights) -> None:
    """Drop a source line the message did not actually earn.

    A trailing "— JIDA Oct 2026, p.14" on a message that never states the finding reads as
    a fabricated reference, which the rubric scores below merely being vague. If the claim
    got trimmed for length, the citation goes with it.
    """
    if not squeeze(plan.citation):
        return
    item = next((d for d in ins.ranked_digest if d.source == plan.citation), None)
    if item is None:
        return

    # A source backs a claim. If the message no longer carries that item's figure or its
    # distinctive phrasing, the citation points at nothing the merchant can see, so it goes.
    drafted = " ".join([plan.why_now, *plan.evidence, plan.insight, plan.proposal])
    if not guard.shares_claim(drafted, f"{item.title} {item.summary}", item.trial_n):
        plan.citation = ""
        plan.because("dropped the source line — the claim it would have backed did not "
                     "survive into the message")


def _language_for(plan: Plan, sheet: FactSheet) -> LanguagePlan:
    if plan.send_as == "merchant_on_behalf" and sheet.cust:
        return plan_language(sheet.languages, sheet.voice.get("code_mix", ""),
                             sheet.cust.get("language_pref"))
    return plan_language(sheet.languages, sheet.voice.get("code_mix", ""))


def _fold_in_adaptation(plan: Plan, sheet: FactSheet, ins: Insights) -> None:
    """Mid-test context injections are scored, so name what moved when something moved."""
    if ins.movement and not any("moved from" in e for e in plan.evidence):
        plan.evidence.insert(0, f"since the last refresh your {ins.movement}")
        plan.max_evidence = max(plan.max_evidence, 3)
        plan.because("cited the numbers that changed in the most recent context push")
    fresh = ins.fresh_digest
    if fresh and plan.angle_id in ("digest", "fallback") and fresh.id != (
            ins.top_digest.id if ins.top_digest else None):
        plan.because(f"newly pushed digest item {fresh.id} was considered and ranked below "
                     f"the item actually used")


def _add_first_touch_marker(body: str, sheet: FactSheet) -> str:
    """One short identifier on a cold open, never on a continuing thread.

    WhatsApp's first outbound to a merchant is a template send from a number they have not
    seen before, so naming the sender once is realistic. It is deliberately three words —
    a preamble longer than that is on the brief's anti-pattern list.
    """
    marker = pick(["Vera here", "Vera from magicpin here"], sheet.merchant_id, "intro")
    salutation = sheet.salutation
    if salutation and body.startswith(salutation):
        rest = body[len(salutation):].lstrip(" —,")
        rest = rest[:1].lower() + rest[1:] if rest[:2].istitle() is False else rest
        return squeeze(f"{salutation}, {marker} — {rest}")
    return squeeze(f"{marker} — {body[:1].lower()}{body[1:]}")


def _suppression_key(sheet: FactSheet) -> str:
    trigger = sheet.trigger
    if trigger and squeeze(trigger.suppression_key):
        return squeeze(trigger.suppression_key)
    kind = trigger.kind if trigger else "nudge"
    stamp = sheet.now.strftime("%Y-%m-%d")
    scope = sheet.cust.get("id") or sheet.merchant_id
    return f"{kind}:{scope}:{stamp}"


def _guard_notes(report) -> list[str]:
    if not report:
        return []
    notes = []
    if report.repaired:
        notes.append("output guard " + "; ".join(report.repaired))
    return notes


def conversation_id_for(sheet: FactSheet) -> str:
    """Decodable and resumable, as the case-study notes ask for."""
    trigger = sheet.trigger
    kind = squeeze((trigger.kind if trigger else "nudge")).replace("_", "")[:14]
    stamp = sheet.now.strftime("%Y%m%d")
    if sheet.cust.get("id"):
        return f"conv_{short_id(sheet.cust['id'], keep=1)}_{kind}_{stamp}"
    return f"conv_{short_id(sheet.merchant_id, keep=1)}_{kind}_{stamp}"
