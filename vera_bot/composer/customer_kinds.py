"""Customer-facing composers — messages Vera drafts for the merchant to send.

Different rules apply here. `send_as` is `merchant_on_behalf`, the merchant has to be
identified in the first line because the customer has no relationship with Vera, the
customer's own `language_pref` overrides the merchant's languages, and consent scope
bounds what may be said at all.
"""

from __future__ import annotations

import re

from ..facts import FactSheet
from ..insights import Insights
from ..utils import human_date, num, oxford, pick, rupees, squeeze, text
from ..voice import REGIONAL_GREETING, plan_language
from .base import Plan
from .registry import customer_kind

_EMOJI = {"dentists": "🦷", "salons": "✨", "gyms": "💪", "restaurants": "🍽️", "pharmacies": "💊"}


def _greeting(sheet: FactSheet, warm: bool = True) -> str:
    """'Hi Priya, Dr. Meera's clinic here' — the customer must know who is writing."""
    cust = sheet.cust
    lang = plan_language(sheet.languages, "", cust.get("language_pref"))
    name = cust.get("name") or "there"

    if lang.regional and lang.regional in REGIONAL_GREETING:
        hello = REGIONAL_GREETING[lang.regional]
    elif lang.mix == "heavy":
        hello = "Namaste"
    else:
        hello = pick(["Hi", "Hello"], cust.get("id", ""), "hello")

    who = _merchant_label(sheet)
    if cust.get("anonymous"):
        return f"{hello} — {who} here"
    if cust.get("parent"):
        return f"{hello} {cust['parent']}, {who} here"
    return f"{hello} {name}, {who} here"


def _merchant_label(sheet: FactSheet) -> str:
    """How the business introduces itself to its own customer.

    A named person outperforms a brand on a WhatsApp message from a local business, so the
    owner's name leads wherever the vertical allows it.
    """
    owner = text(sheet.owner_name)
    name = text(sheet.business_name, "we")
    if sheet.category_slug == "dentists" and owner:
        return f"{owner}'s clinic"
    if owner and name != "we" and owner.lower() not in name.lower() \
            and len(f"{owner} {name}") < 40:
        return f"{owner} from {name}"
    if sheet.locality and len(name) < 30:
        return f"{name}, {sheet.locality}"
    return name


def _gap_phrase(sheet: FactSheet) -> str:
    cust = sheet.cust
    months, days = cust.get("months_since_visit"), cust.get("days_since_visit")
    if months and months >= 2:
        return f"it's been about {num(months)} months since your last visit"
    if days and days >= 14:
        return f"it's been {num(days)} days since we last saw you"
    return ""


def _history_line(sheet: FactSheet) -> str:
    """The relationship facts a customer can verify — visits, since when, what for.

    Most generated customer records carry nothing but these, so this is what keeps a
    sparse-context customer message specific instead of generic.
    """
    cust = sheet.cust
    visits, first = cust.get("visits"), cust.get("first_visit")
    service = text(cust.get("top_service"))
    if visits and visits >= 2 and first:
        line = f"you've been in {num(visits)} times since {human_date(first, with_year=True)}"
        return f"{line}, mostly for {service}" if service else line
    if visits and visits >= 2:
        line = f"you've been in {num(visits)} times so far"
        return f"{line}, mostly for {service}" if service else line
    if cust.get("last_visit"):
        return f"your last visit with us was {human_date(cust['last_visit'], with_year=True)}"
    return ""


def _price_line(sheet: FactSheet, ins: Insights) -> str:
    offer = ins.lead_offer or ins.suggested_offer
    if not offer:
        return ""
    if offer.source == "category_catalog":
        return ""       # never quote a price the merchant has not actually put live
    return offer.title


def _slot_line(sheet: FactSheet) -> tuple[str, str]:
    """Returns (sentence, cta_kind) from the slots the trigger actually supplied.

    Code-mixed inline where the customer's stated preference asks for it, which reads far
    more naturally than bolting a Hindi tag onto the end of an English sentence.
    """
    trigger = sheet.trigger
    slots = [squeeze(str(s.get("label") or s.get("iso") or ""))
             for s in (trigger.slots if trigger else [])]
    slots = [s for s in slots if s]
    preference = text(sheet.cust.get("preferred_slots"))
    lang = plan_language(sheet.languages, "", sheet.cust.get("language_pref"))
    mixed = lang.mix in ("natural", "heavy") and not lang.regional

    if len(slots) >= 2:
        matched = f", dono {preference} — jaisa aap usually lete hain" if preference and mixed \
            else (f", both {preference} slots, which is what you usually take" if preference else "")
        if mixed:
            return (f"{slots[0]} ya {slots[1]} khaali hai{matched}", "multi_choice_slot")
        return (f"two slots are open: {slots[0]} or {slots[1]}{matched}", "multi_choice_slot")
    if len(slots) == 1:
        if mixed:
            return (f"agla open slot {slots[0]} ka hai", "binary_yes_no")
        return (f"the next open slot is {slots[0]}", "binary_yes_no")
    if preference:
        if mixed:
            return (f"hum aapke liye ek {preference} slot rakh sakte hain", "binary_yes_no")
        return (f"we can hold a {preference} slot, which is what you usually take", "binary_yes_no")
    return ("", "binary_yes_no")


def _consent_note(sheet: FactSheet) -> str:
    cust = sheet.cust
    if cust.get("reminder_consent"):
        return "customer consented to reminders, so this is in scope"
    if cust.get("promo_consent"):
        return ("customer's consent covers offers rather than reminders, so this is framed as an "
                "offer and kept to one send")
    return "consent scope is thin, so the message stays factual and offers an easy opt-out"


def _base_plan(sheet: FactSheet, kind: str, template: str) -> Plan:
    plan = Plan(kind=kind, send_as="merchant_on_behalf", template=template,
                lead_with_name=False, plain_insight=True,
                emoji=_EMOJI.get(sheet.category_slug, ""))
    lang = plan_language(sheet.languages, "", sheet.cust.get("language_pref"))
    plan.hindi_slot = "works_for_you" if lang.mix in ("natural", "heavy") else "shall_i_send"
    return plan


# --------------------------------------------------------------------------- #

@customer_kind("recall_due", "customer_lapsed_soft", "appointment_due")
def recall_due(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    cust = sheet.cust

    service = text(payload.get("service_due"))
    due = payload.get("due_date")
    plan = _base_plan(sheet, "recall_due", "merchant_recall_reminder_v1")

    due_phrase = trigger.translated if trigger and trigger.translated else "your next visit is due"
    if service:
        due_phrase = f"your {service} is due"
    elif due:
        due_phrase = f"your next visit was due {human_date(due, with_year=True)}"

    gap = _gap_phrase(sheet)
    # The translated kind and the gap phrase often say the same thing; pick the sharper one.
    lead = due_phrase if (service or due) else (gap or due_phrase)
    if gap and lead is due_phrase and (service or due):
        lead = f"{gap} and {due_phrase}"
    plan.why_now = f"{_greeting(sheet)}. {lead}"

    evidence = []
    history = _history_line(sheet)
    if history:
        evidence.append(history)
    price = _price_line(sheet, ins)
    if price:
        evidence.append(f"we're running {price} at the moment")
    plan.evidence = evidence

    slot_sentence, cta_kind = _slot_line(sheet)
    plan.insight = slot_sentence
    plan.cta = cta_kind
    if any(word in slot_sentence for word in ("khaali", "agla", "hum aapke", "dono")):
        plan.hindi_slot = ""          # the mix is already inside the slot sentence
    plan.ask = ("Reply 1 or 2, or send a time that suits you better."
                if cta_kind == "multi_choice_slot" else
                "Reply YES and we'll hold it for you.")
    plan.levers = ["relationship_continuity", "specificity", "low_friction_commitment"]
    return plan.because("sent from the merchant's number on the merchant's behalf",
                        f"language matched to the customer's stated preference "
                        f"({cust.get('language_pref') or 'English'})",
                        _consent_note(sheet),
                        "slot options come from the trigger, not invented")


@customer_kind("customer_lapsed_hard", "winback_customer", "churn_risk")
def lapsed_hard(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    cust = sheet.cust

    days = payload.get("days_since_last_visit") or cust.get("days_since_visit")
    focus = text(payload.get("previous_focus"))
    months_member = payload.get("previous_membership_months")

    plan = _base_plan(sheet, "customer_lapsed_hard", "merchant_winback_v1")
    weeks = None
    if days:
        weeks = sheet.allow(int(round(int(days) / 7)), "derived.days_since_visit/7")

    gap = (f"it's been about {num(weeks)} weeks" if weeks else _gap_phrase(sheet)) or "it's been a while"
    plan.why_now = f"{_greeting(sheet)}. {gap} — no pressure either way"

    evidence = []
    if months_member:
        evidence.append(f"you were with us {num(months_member)} months before that")
    if focus:
        evidence.append(f"back then you were working on {focus}")
    if not evidence:
        history = _history_line(sheet)
        if history:
            evidence.append(history)
    plan.evidence = evidence

    price = _price_line(sheet, ins)
    if price:
        plan.insight = f"{price} is live right now, and it's the easiest way back in"
    else:
        plan.insight = _cadence_note(sheet) or "nothing has changed about how we run"

    plan.ask = "Reply YES and we'll keep a spot for you — no commitment, nothing charged."
    plan.cta = "binary_yes_no"
    plan.levers = ["no_shame_framing", "relationship_continuity", "low_friction_commitment"]
    return plan.because("win-back written without guilt, which is what the category voice asks for",
                        "referenced what they were actually working on rather than a generic pitch",
                        _consent_note(sheet))


@customer_kind("chronic_refill_due", "refill_due")
def chronic_refill(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    molecules = trigger.molecules if trigger else []
    runs_out = payload.get("stock_runs_out_iso")
    last = payload.get("last_refill")
    saved_address = payload.get("delivery_address_saved")

    # Outside pharmacy this kind does not mean medicine; the translation keeps it honest.
    if sheet.category_slug != "pharmacies":
        return _generic_customer_followup(sheet, ins)

    plan = _base_plan(sheet, "chronic_refill_due", "merchant_refill_reminder_v1")
    subject = oxford(molecules, "and") if molecules else "your monthly medicines"
    when = f" on {human_date(runs_out, with_year=True)}" if runs_out else ""
    plan.why_now = f"{_greeting(sheet)}. Your {subject} run out{when}"

    evidence = []
    if last:
        evidence.append(f"last refilled {human_date(last, with_year=True)}")
    evidence.append("same dose, same pack — nothing changes unless your doctor has changed it")
    plan.evidence = evidence

    offers = [o.title for o in sheet.active_offers]
    if offers:
        plan.insight = f"{oxford(offers[:2], 'and')} applies to this order"
    if saved_address:
        plan.insight = squeeze(f"{plan.insight}, and your saved address is on file".lstrip(", "))

    plan.ask = "Reply CONFIRM and we'll pack it, or tell us if the dosage has changed."
    plan.cta = "binary_confirm_cancel"
    plan.levers = ["precision", "relationship_continuity", "low_friction_commitment"]
    return plan.because("named the exact molecules and the run-out date from the trigger",
                        "left room for a dosage change instead of assuming a repeat",
                        _consent_note(sheet))


@customer_kind("trial_followup")
def trial_followup(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    trial_date = payload.get("trial_date") or payload.get("trial_completed")

    plan = _base_plan(sheet, "trial_followup", "merchant_trial_followup_v1")
    when = f" on {human_date(trial_date, with_year=True)}" if trial_date else ""
    plan.why_now = f"{_greeting(sheet)}. You came in for the trial{when}"

    slot_sentence, cta_kind = _slot_line(sheet)
    evidence = []
    if slot_sentence:
        evidence.append(slot_sentence)
    price = _price_line(sheet, ins)
    if price:
        evidence.append(f"{price} is what it costs to carry on")
    plan.evidence = evidence

    plan.insight = ("the second session is the one that decides it — most people who come back "
                    "once come back regularly")
    plan.cta = cta_kind
    plan.ask = ("Reply 1 or 2 and we'll hold it." if cta_kind == "multi_choice_slot"
                else "Reply YES and we'll keep the spot.")
    plan.levers = ["relationship_continuity", "specificity", "low_friction_commitment"]
    return plan.because("followed up on a real trial date rather than a generic re-engagement",
                        _consent_note(sheet))


@customer_kind("wedding_package_followup", "bridal_followup")
def bridal_followup(sheet: FactSheet, ins: Insights) -> Plan | None:
    trigger = sheet.trigger
    payload = (trigger.payload if trigger else {}) or {}
    wedding = payload.get("wedding_date")
    days = payload.get("days_to_wedding")
    trial_done = payload.get("trial_completed")
    # "skin_prep_program_30day" -> "30-day skin-prep programme"
    window = text(payload.get("next_step_window_open"))
    duration = re.search(r"(\d+)\s*day", window)
    if duration:
        window = squeeze(re.sub(r"\b\d+\s*day\b", "", window))
        window = f"{duration.group(1)}-day {window}".strip()

    plan = _base_plan(sheet, "wedding_package_followup", "merchant_bridal_followup_v1")
    plan.emoji = "💍"
    when = f" on {human_date(wedding, with_year=True)}" if wedding else ""
    plan.why_now = (f"{_greeting(sheet)}. {num(days)} days to the wedding{when}"
                    if days else f"{_greeting(sheet)}. Checking in on the wedding plan")

    evidence = []
    if trial_done:
        evidence.append(f"your trial was {human_date(trial_done, with_year=True)}")
    if window:
        evidence.append(f"the {window} window opens now, before the bookings tighten up")
    elif days:
        evidence.append("this is the stretch where the prep actually has time to work")
    plan.evidence = evidence

    # A ₹99 haircut is not a bridal package; quoting it here would read as a bot that has
    # not understood its own message.
    relevant = next((o.title for o in sheet.active_offers
                     if {"bridal", "package", "trial", "membership", "spa", "facial"}
                     & set(o.title.lower().split())), "")
    plan.insight = (f"{relevant} is what we have live against it" if relevant else
                    "we'd rather start the prep early than compress it near the date")
    preference = text(sheet.cust.get("preferred_slots"))
    plan.ask = (f"Reply YES and we'll block your usual {preference} slot for the first session."
                if preference else "Reply YES and we'll block the first session for you.")
    plan.levers = ["relationship_continuity", "urgency", "low_friction_commitment"]
    return plan.because("anchored on the real wedding date and the completed trial",
                        _consent_note(sheet))


@customer_kind("appointment_tomorrow", "booking_reminder")
def appointment_tomorrow(sheet: FactSheet, ins: Insights) -> Plan | None:
    plan = _base_plan(sheet, "appointment_tomorrow", "merchant_appointment_reminder_v1")
    slot_sentence, _ = _slot_line(sheet)
    plan.why_now = f"{_greeting(sheet)}. Quick reminder about tomorrow"
    plan.evidence = [slot_sentence] if slot_sentence else []
    history = _history_line(sheet)
    if history and not slot_sentence:
        plan.evidence.append(history)
    if not slot_sentence:
        cadence = _cadence_note(sheet)
        if cadence:
            plan.evidence.append(cadence)
    top_service = text(sheet.cust.get("top_service"))
    if top_service:
        plan.evidence.append(f"booked in for {top_service}, same as last time")
    plan.insight = "if the timing has moved, easier to know now than tomorrow morning"
    if not plan.evidence:
        plan.insight = f"{plan.insight}, and {_cadence_note(sheet)}" if _cadence_note(sheet) \
            else plan.insight
    plan.ask = "Reply YES to confirm, or send a better time."
    plan.cta = "binary_confirm_cancel"
    plan.levers = ["low_friction_commitment", "relationship_continuity"]
    return plan.because("confirmation reminder keeps the slot usable if they cannot make it",
                        _consent_note(sheet))


#: How each vertical lowers the friction of coming back, in its own register.
_EASY_RETURN = {
    "dentists": "nothing needs booking ahead — send a day and we'll fit you in",
    "salons": "send a day and we'll keep the chair free",
    "gyms": "no rejoining fee and no paperwork — just walk in",
    "pharmacies": "same brand, same pack, kept ready at the counter",
    "restaurants": "send a day and we'll keep a table",
}


def _cadence_note(sheet: FactSheet) -> str:
    """A category-true reason the visit matters, drawn from the peer benchmark."""
    peer = sheet.peer or {}
    for key, phrasing in (
            ("retention_6mo_pct", "most {noun} in this area come back within six months"),
            ("retention_3mo_pct", "most {noun} in this area come back within three months"),
            ("repeat_customer_pct", "most {noun} here are regulars rather than one-offs")):
        if peer.get(key):
            return phrasing.format(noun=sheet.customer_noun_plural)
    return _EASY_RETURN.get(sheet.category_slug, "")


def _generic_customer_followup(sheet: FactSheet, ins: Insights) -> Plan | None:
    """When a trigger kind does not belong to this vertical, say the honest version of it."""
    trigger = sheet.trigger
    translated = (trigger.translated if trigger else "") or "a follow-up that is due"
    plan = _base_plan(sheet, "customer_followup", "merchant_followup_v1")

    gap = _gap_phrase(sheet)
    plan.why_now = f"{_greeting(sheet)}. {gap or f'you have {translated}'}"
    evidence = []
    history = _history_line(sheet)
    if history:
        evidence.append(history)
    price = _price_line(sheet, ins)
    if price:
        evidence.append(f"{price} is live at the moment")
    elif ins.lead_offer or ins.suggested_offer:
        pass
    plan.evidence = evidence
    plan.insight = _cadence_note(sheet) or (translated if not gap else "")
    plan.ask = "Reply YES and we'll hold a slot for you."
    plan.cta = "binary_yes_no"
    plan.levers = ["relationship_continuity", "low_friction_commitment"]
    return plan.because(
        f"trigger kind '{trigger.kind if trigger else 'unknown'}' is not native to "
        f"{sheet.category_slug}, so it was translated into the equivalent this business "
        f"actually has rather than sending category-wrong copy",
        _consent_note(sheet))
