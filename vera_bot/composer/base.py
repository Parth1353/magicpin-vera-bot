"""The message plan and the renderer that turns it into WhatsApp prose.

A per-kind composer never writes a finished message. It fills a `Plan` — why now, the
evidence, the judgment, the work Vera is offering to do, and the single ask — and the
renderer assembles those slots into sentences, places the language mix, enforces one ask
per message, and trims to a length a merchant will actually read on a phone.

Two invariants the renderer guarantees so that nothing downstream has to check them:
  * exactly one question mark, and it belongs to the ask
  * the body stays inside the length budget, dropping the least evidential sentence first
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from ..facts import FactSheet
from ..insights import Insights
from ..utils import pick, split_sentences, squeeze, trim_trailing_punct
from ..voice import LanguagePlan, VoiceProfile, cta_line, hindi

CTA_KINDS = ("binary_yes_no", "binary_confirm_cancel", "open_ended", "multi_choice_slot", "none")

#: A merchant reads this on a phone between customers. The case studies that score 50/50
#: sit between 250 and 400 characters; past ~520 the ask stops landing.
LENGTH_BUDGET = 470
CUSTOMER_LENGTH_BUDGET = 420

#: Hindi clauses that are statements, safe to place mid-message without adding a second ask
_STATEMENT_SLOTS = ("two_minutes", "i_drafted", "i_will_handle", "worth_a_look",
                    "no_pressure", "for_you", "ready", "one_line", "understood", "tell_me")
_QUESTION_SLOTS = ("shall_i_send", "shall_i_do", "works_for_you")

_NUMBER = re.compile(r"\d")


@dataclass
class Plan:
    """What to say, before it is language."""

    kind: str
    angle_id: str = ""
    why_now: str = ""                                  # the trigger-anchored opening clause
    evidence: list[str] = field(default_factory=list)  # verifiable, number-bearing
    insight: str = ""                                  # the judgment worth paying for
    proposal: str = ""                                 # the artefact Vera will produce
    ask: str = ""                                      # the single next step
    cta: str = "binary_yes_no"
    citation: str = ""
    send_as: str = "vera"
    levers: list[str] = field(default_factory=list)
    template: str = ""
    params: list[str] = field(default_factory=list)
    rationale_bits: list[str] = field(default_factory=list)
    hindi_slot: str = "shall_i_send"
    lead_with_name: bool = True
    plain_insight: bool = False        # emit the insight without an "My read:" lead-in
    evidence_priority: int | None = None   # when the evidence is the deliverable itself
    emoji: str = ""
    max_evidence: int = 2

    def because(self, *bits: str) -> "Plan":
        self.rationale_bits.extend(b for b in bits if b)
        return self


@dataclass
class Composition:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: list[str]
    angle_id: str = ""
    levers: list[str] = field(default_factory=list)
    language: str = "English"
    notes: list[str] = field(default_factory=list)
    variant: int = 0


# --------------------------------------------------------------------------- #
# connective tissue
# --------------------------------------------------------------------------- #

_INSIGHT_LEADS = [
    "My read: {insight}",
    "The part that matters: {insight}",
    "Which means {insight}",
    "Worth knowing: {insight}",
]

_NUMERIC_INSIGHT_LEADS = [
    "Against your numbers: {insight}",
    "My read: {insight}",
    "Which puts it in context: {insight}",
    "The part that matters: {insight}",
]

#: Prefix-only by design. A lead with a trailing word ("I can have {x} ready") reads fine
#: for a two-word artefact and falls apart for a clause, and the artefact is often a clause.
_PROPOSAL_LEADS = [
    "I'll draft {proposal}",
    "I can put together {proposal}",
    "Happy to write {proposal}",
]

_LONG_PROPOSAL_LEADS = [
    "I can take on {proposal}",
    "I'll handle {proposal}",
    "Happy to put together {proposal}",
]


def _as_statement(text: str) -> str:
    """Strip any question so that only the ask carries a '?'."""
    cleaned = squeeze(text).replace("?", ".")
    return squeeze(re.sub(r"\.{2,}", ".", cleaned))


def _decapitalise(text: str, protect: FactSheet | None = None) -> str:
    """Lower an ordinary leading capital so it reads mid-sentence after a lead-in.

    Acronyms (ORS, IDA, RVG) and names lifted from the contexts keep their casing —
    "which means ors demand is up" would be worse than the problem it fixes.
    """
    if not text:
        return text
    word = text.split(" ", 1)[0].strip(",:")
    if word.isupper() or not word[:1].isupper():
        return text
    if word == "I" or word.startswith("I'"):
        return text
    if protect is not None:
        names = " ".join(filter(None, [protect.business_name, protect.city, protect.locality,
                                       protect.owner_name, " ".join(protect.vocab)]))
        if word.lower() in names.lower():
            return text
    return text[:1].lower() + text[1:]


def _fix_case(text: str) -> str:
    """Capitalise the first letter of every sentence inside a fragment."""
    out = squeeze(text)
    if not out:
        return out
    out = out[:1].upper() + out[1:]
    return re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)


def _first_sentence(text: str, limit: int = 200) -> str:
    """Quoted source summaries run long; keep the claim, drop the appendix."""
    body = squeeze(text)
    if not body:
        return ""
    sentences = split_sentences(body)
    kept = sentences[0]
    if len(kept) < 90 and len(sentences) > 1 and len(kept) + len(sentences[1]) <= limit:
        kept = f"{kept} {sentences[1]}"
    if len(kept) > limit:
        kept = kept[:limit].rsplit(" ", 1)[0] + "…"
    return squeeze(kept)


def _hindi_clause(plan: Plan, lang: LanguagePlan, seed: str, ask_is_question: bool) -> str:
    """One natural Hindi clause, placed as a statement so it never becomes a second ask."""
    if not lang.uses_hindi or not plan.hindi_slot:
        return ""
    slot = plan.hindi_slot
    if slot in _QUESTION_SLOTS and (ask_is_question or lang.mix == "light"):
        # a "shall I?" tag next to a question would be a second ask, so use a statement
        slot = "standalone"
    return squeeze(hindi(slot, seed, plan.kind))


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

#: What survives when the message runs long. The reason to send and the ask are never
#: dropped; the first piece of evidence outranks the second; connective niceties go first.
PRIORITY = {"opening": 100, "ask": 100, "evidence_key": 90, "insight": 80, "evidence0": 70,
            "proposal": 60, "evidence1": 50, "evidence2": 40, "hindi": 30}


def render(plan: Plan, sheet: FactSheet, ins: Insights, lang: LanguagePlan,
           voice: VoiceProfile, variant: int = 0) -> str:
    seed = f"{sheet.merchant_id}:{plan.kind}:{variant}"
    parts: list[tuple[str, str]] = []          # (slot, text)

    # --- opening: name + why now ------------------------------------------
    opening = _as_statement(plan.why_now)
    if plan.lead_with_name and sheet.salutation and opening:
        if opening[0].islower():
            opening = f"{sheet.salutation}, {opening}"
        else:
            lead = pick(["{name} —", "{name},"], seed, "lead", offset=variant)
            opening = f"{lead.format(name=sheet.salutation)} {opening}"
    elif plan.lead_with_name and sheet.salutation:
        opening = sheet.salutation
    if opening:
        parts.append(("opening", opening))

    # --- evidence: one sentence each, never welded together with connectives
    for index, item in enumerate(plan.evidence[: plan.max_evidence]):
        cleaned = _first_sentence(_as_statement(item))
        if cleaned:
            slot = "evidence_key" if plan.evidence_priority else f"evidence{min(index, 2)}"
            parts.append((slot, cleaned))

    # --- judgment ----------------------------------------------------------
    insight = _first_sentence(_as_statement(plan.insight), limit=240)
    if insight:
        if plan.plain_insight or insight.rstrip().endswith(":"):
            parts.append(("insight", insight))
        else:
            leads = _NUMERIC_INSIGHT_LEADS if _NUMBER.search(insight) else _INSIGHT_LEADS
            first = insight.split(" ", 1)[0].lower().strip(",:")
            # "Worth knowing: worth separating..." — pick a lead that does not echo it
            usable = [l for l in leads if not l.lower().startswith(first)] or leads
            lead = pick(usable, seed, "insight", offset=variant)
            text = trim_trailing_punct(insight)
            if not lead.split("{")[0].rstrip().endswith(":"):
                text = _decapitalise(text, protect=sheet)
            parts.append(("insight", lead.format(insight=text)))

    # --- the work Vera does ------------------------------------------------
    proposal = _as_statement(plan.proposal)
    if proposal:
        leads = _LONG_PROPOSAL_LEADS if len(proposal) > 45 else _PROPOSAL_LEADS
        lead = pick(leads, seed, "proposal", offset=variant)
        parts.append(("proposal", lead.format(proposal=trim_trailing_punct(proposal))))

    # --- language + the single ask -----------------------------------------
    ask = squeeze(plan.ask) or cta_line(plan.cta, proposal or "the draft", seed, offset=variant)
    # "Reply YES and we'll hold it" is as much an ask as a question mark, so a Hindi
    # question tag next to it would be a second ask.
    ask_is_question = bool(re.search(r"\?|\b(?:reply|say|tell|send|correct)\b", ask, re.I))
    clause = _hindi_clause(plan, lang, seed, ask_is_question)
    if clause:
        parts.append(("hindi", clause))
    if plan.cta != "none" and ask:
        parts.append(("ask", ask))

    budget = CUSTOMER_LENGTH_BUDGET if plan.send_as == "merchant_on_behalf" else LENGTH_BUDGET
    if plan.citation:
        budget -= len(plan.citation) + 3
    body = _assemble(parts, budget)

    if plan.emoji:
        body = f"{body} {plan.emoji}"
    if squeeze(plan.citation):
        body = f"{squeeze(body)} — {squeeze(plan.citation)}"
    return squeeze(body)


def _assemble(parts: Sequence[tuple[str, str]], budget: int) -> str:
    """Emit every part, dropping the lowest-priority ones until the body fits."""
    kept = list(parts)
    while True:
        body = _stitch(text for _, text in kept)
        if len(body) <= budget or len(kept) <= 2:
            return body
        droppable = [i for i, (slot, _) in enumerate(kept) if PRIORITY.get(slot, 50) < 100]
        if not droppable:
            return body
        worst = min(droppable, key=lambda i: (PRIORITY.get(kept[i][0], 50),
                                              len(_NUMBER.findall(kept[i][1]))))
        kept.pop(worst)


def _stitch(parts) -> str:
    out: list[str] = []
    for raw in parts:
        piece = _fix_case(squeeze(raw))
        if not piece:
            continue
        if not piece.endswith((".", "?", "!", ":")):
            piece += "."
        out.append(piece)
    text = " ".join(out)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    text = re.sub(r"([.?!])\1+", r"\1", text)
    text = re.sub(r"\s*—\s*—\s*", " — ", text)
    return squeeze(text)


def tighten(body: str, budget: int, protect_last: int = 2) -> str:
    """Drop the least evidential middle sentence until the body fits.

    The opening carries the reason to send and the last sentences carry the ask, so both
    ends are protected; what goes is whatever middle sentence contains the fewest facts.
    """
    if len(body) <= budget:
        return body
    sentences = split_sentences(body)
    while len(body) > budget and len(sentences) > protect_last + 1:
        candidates = range(1, len(sentences) - protect_last)
        if not candidates:
            break
        worst = min(candidates, key=lambda i: (len(_NUMBER.findall(sentences[i])),
                                               -len(sentences[i])))
        sentences.pop(worst)
        body = squeeze(" ".join(sentences))
    return body


def build_params(plan: Plan, sheet: FactSheet) -> list[str]:
    """Parameters for the pre-approved template used on a first outbound."""
    if plan.params:
        return [squeeze(p) for p in plan.params if squeeze(p)]
    headline = _first_sentence(_as_statement(plan.evidence[0])) if plan.evidence \
        else _as_statement(plan.why_now)
    ask = squeeze(plan.ask) or "Reply YES and I'll get started."
    params = [sheet.salutation or sheet.business_name, squeeze(headline), ask]
    if plan.citation:
        params.append(squeeze(plan.citation))
    return [p for p in params if p]


def build_rationale(plan: Plan, sheet: FactSheet, ins: Insights,
                    lang: LanguagePlan, extra: Sequence[str] = ()) -> str:
    """The judge cross-checks this against the message, so it states the actual reasoning."""
    trigger = sheet.trigger
    bits: list[str] = []
    if trigger:
        bits.append(f"{trigger.kind.replace('_', ' ')} (urgency {trigger.urgency}, "
                    f"{trigger.source}) is why this goes now")
    bits.extend(plan.rationale_bits)
    bits.extend(extra)
    if plan.levers:
        bits.append("levers: " + ", ".join(dict.fromkeys(plan.levers)))
    if lang.mix != "none":
        bits.append(f"written in {lang.label} to match the declared language preference")
    if plan.citation:
        bits.append(f"source cited inline ({squeeze(plan.citation)})")
    return squeeze("; ".join(b for b in bits if squeeze(b))) + "."
