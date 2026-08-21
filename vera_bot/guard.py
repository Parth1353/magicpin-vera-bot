"""Output policy: everything that must be true before a message reaches a merchant.

The judge's explicit penalties are fabrication (-2) and internal jargon (-1), plus a hard
fail on URLs (-3 each) and anti-repetition (-2 each). The case studies add two more caps:
an uncited research or compliance claim is capped at 7, and near-duplicating a published
case-study body is scored as plagiarism.

So this module is the last thing every message passes through. Where a violation can be
repaired without gutting the message it repairs it; where it cannot, it reports and the
composer falls back to a different variant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .facts import FactSheet, NumberLedger
from .utils import (jaccard, normalise_for_compare, split_sentences, squeeze,
                    token_set)

# --------------------------------------------------------------------------- #
# patterns
# --------------------------------------------------------------------------- #

_URL = re.compile(
    r"\b(?:https?://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]*\.(?:com|in|org|net|co|io|app|me|gov|edu)\b(?:/\S*)?",
    re.I,
)
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_INTERNAL_ID = re.compile(r"\b(?:m|c|o|d|trg|pc|conv|ack)_[0-9a-z]", re.I)

#: terms that only exist inside our own system
_JARGON = (
    "suppression key", "suppression", "payload", "context push", "merchant id",
    "customer id", "trigger id", "trigger kind", "endpoint", "json", "api call",
    "prompt", "dataset", "fact sheet", "factsheet", "composer", "the trigger",
    "peer median flag", "signal flag", "urgency score", "context object",
)

#: shorthand that is fine internally but reads as jargon to a shop owner, unless the
#: category's own vocabulary explicitly allows it
_SOFT_JARGON = {
    "ctr": "how many views turn into calls",
    "cta": "the ask",
    "kpi": "the number that matters",
    "roi": "what it pays back",
    "crm": "your customer list",
    "gbp": "your Google listing",
    "sku": "item",
    "churn": "members dropping off",
}

#: Claims that must carry a source, per the case-study scoring rule ("no citation =
#: capped at 7"). Deliberately narrow: an offer called "3 FREE Trial Classes" is not a
#: research claim, so the bare word "trial" does not qualify — "trial of 2,100" does.
_CLAIM_PATTERNS = (
    r"\b(?:stud(?:y|ies)|research|meta-analysis|journal)\b",
    r"\btrials?\s+(?:of|in|shows?|showed|found)\b",
    r"\b\d[\d,]*[- ]?(?:patient|customer|member|client|guest)s?\s+trial\b",
    r"\b(?:circular|bulletin|guideline|regulation|directive|advisory)\b",
    r"\b(?:council|authority|regulator)\b",
    r"\bvoluntary recall\b|\brecall(?:ed)?\s+batch",
    r"\b(?:shows?|showed|found|reported)\b[^.?!]{0,40}\b\d+(?:\.\d+)?%",
)



# --------------------------------------------------------------------------- #
# published case-study bodies — near-duplicates are penalised as plagiarism
# --------------------------------------------------------------------------- #

CASE_STUDY_BODIES: tuple[str, ...] = (
    "Dr. Meera, JIDA's Oct issue landed. One item relevant to your high-risk adult patients "
    "2,100-patient trial showed 3-month fluoride recall cuts caries recurrence 38% better than "
    "6-month. Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp "
    "you can share? JIDA Oct 2026 p.14",
    "Hi Priya, Dr. Meera's clinic here It's been 5 months since your last visit your 6-month "
    "cleaning recall is due. Apke liye 2 slots ready hain: Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm. "
    "299 cleaning + complimentary fluoride. Reply 1 for Wed, 2 for Thu, or tell us a time that works.",
    "Hi Kavya Lakshmi from Studio11 Kapra here. 196 days to your wedding perfect window to start "
    "the 30-day skin-prep program before serious bridal bookings roll in. 2,499 covers 4 sessions "
    "+ a take-home kit. Want me to block your preferred Saturday 4pm slot for the first session next week?",
    "Hi Lakshmi! Quick check what service has been most asked-for this week at Studio11? I'll turn "
    "the answer into a Google post + a 4-line WhatsApp reply you can use when customers ask about "
    "pricing. Takes 5 min.",
    "Quick heads-up Suresh DC vs MI at Arun Jaitley tonight, 7:30pm. Important: Saturday IPL matches "
    "usually shift -12% restaurant covers (people watch at home). Skip the match-night promo today; "
    "instead push your BOGO pizza (already active) as a delivery-only Saturday special. Want me to "
    "draft the Swiggy banner + an Insta story? Live in 10 min.",
    "Suresh, here's a starter version you can edit: Mylari Corporate Thali for offices in Indiranagar "
    "10 thalis @ 125 each (25 off retail) + free delivery 25 thalis @ 115 each + 2 free filter coffees "
    "50+: 105 each + 1 free dosa platter WhatsApp the day-before by 5pm; we deliver between 12:30-1pm. "
    "3 offices in Indiranagar are in your delivery radius. Want me to draft a 3-line WhatsApp to send "
    "their facilities managers?",
    "Karthik, your views are down 30% this week but I want to flag this is the normal April-June "
    "acquisition lull (every metro gym sees -25 to -35% in this window). Action: skip ad spend now, "
    "save it for Sept-Oct when conversion is 2x. For now, focus retention on your 245 members. Want me "
    "to draft a summer attendance challenge to keep them through the dip?",
    "Hi Rashmi Karthik from PowerHouse here. It's been about 8 weeks happens to most members at some "
    "point, no judgment. We've added a Tue/Thu evening HIIT class that fits weight-loss goals well "
    "(45 min, 6:30pm). Want me to hold a free trial spot for you next Tue, 30 Apr? Reply YES no "
    "commitment, no auto-charge.",
    "Ramesh, urgent: voluntary recall on 2 atorvastatin batches (AT2024-1102, AT2024-1108) by Mfr Z "
    "sub-potency, no safety risk, but customers should be informed for replacement. Pulled your "
    "repeat-Rx list: 22 of your chronic-Rx customers were dispensed these batches in last 90 days. "
    "Want me to draft their WhatsApp note + the replacement-pickup workflow?",
    "Namaste Apollo Health Plus Malviya Nagar yahan. Sharma ji ki 3 monthly medicines (metformin, "
    "atorvastatin, telmisartan) 28 April ko khatam hongi. Same dose, same brand pack ready hai. "
    "Senior discount 15% applied total 1,420 (240 saved). Free home delivery to saved address by 5pm "
    "tomorrow. Reply CONFIRM to dispatch, or call if any change in dosage.",
)

#: Function words and contraction fragments. Two messages from the same business
#: necessarily share "clinic here it s been"; that is English, not copying.
_GLUE = frozenset({
    "s", "t", "ll", "ve", "re", "d", "m", "the", "and", "for", "you", "your", "with", "here",
    "been", "since", "from", "that", "this", "have", "has", "was", "are", "not", "but", "can",
    "will", "one", "out", "get", "got", "let", "all", "any", "our", "its", "it", "a", "an",
    "of", "to", "in", "on", "is", "be", "at", "or", "we", "us", "me", "my", "so", "if", "as",
    "hi", "hello", "reply", "want", "yes",
})


def _content_words(text: str) -> list[str]:
    return [w for w in normalise_for_compare(text).split()
            if len(w) > 2 and w not in _GLUE]


def _ngrams(text: str, n: int = 5) -> set[str]:
    """N-grams over content words only, so shared grammar cannot look like shared writing."""
    words = _content_words(text)
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


_CASE_TOKENS = [token_set(b) for b in CASE_STUDY_BODIES]
_CASE_NGRAMS = [_ngrams(b) for b in CASE_STUDY_BODIES]
PLAGIARISM_THRESHOLD = 0.60


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

@dataclass
class GuardReport:
    body: str
    ok: bool = True
    blocking: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def summary(self) -> str:
        bits = []
        if self.blocking:
            bits.append("blocked: " + "; ".join(self.blocking))
        if self.repaired:
            bits.append("repaired: " + "; ".join(self.repaired))
        return " | ".join(bits)


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #

def strip_urls(text: str) -> tuple[str, bool]:
    """Meta rejects link-bearing templates; the judge scores a URL as a hard fail."""
    cleaned = _URL.sub("", text or "")
    changed = cleaned != text
    return squeeze(re.sub(r"\s+([,.;:?!])", r"\1", cleaned)), changed


def find_taboos(text: str, taboos: Sequence[str]) -> list[str]:
    lowered = (text or "").lower()
    hits = []
    for raw in taboos:
        term = squeeze(str(raw)).lower()
        # entries like "FDA-approved (use only when actually applicable)" carry a caveat
        term = re.sub(r"\s*\(.*?\)\s*", "", term).strip()
        if not term:
            continue
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            hits.append(term)
    return hits


def find_jargon(text: str, allowed_vocab: Iterable[str] = ()) -> list[str]:
    lowered = (text or "").lower()
    allowed = {squeeze(v).lower() for v in allowed_vocab}
    hits = []

    for term in _JARGON:
        if term in lowered:
            hits.append(term)
    for term, _ in _SOFT_JARGON.items():
        if term in allowed:
            continue
        if re.search(rf"\b{term}\b", lowered):
            hits.append(term)

    for match in _SNAKE.findall(text or ""):
        if match.lower() in allowed:
            continue
        hits.append(match)
    if _INTERNAL_ID.search(text or ""):
        hits.append("internal id")
    return sorted(set(hits))


def soften_jargon(text: str, allowed_vocab: Iterable[str] = ()) -> str:
    allowed = {squeeze(v).lower() for v in allowed_vocab}
    out = text or ""
    for term, replacement in _SOFT_JARGON.items():
        if term in allowed:
            continue
        out = re.sub(rf"\b{term}\b", replacement, out, flags=re.I)
    out = _SNAKE.sub(lambda m: m.group(0).replace("_", " "), out)
    return squeeze(out)


def needs_citation(text: str) -> bool:
    lowered = (text or "").lower()
    return any(re.search(pattern, lowered) for pattern in _CLAIM_PATTERNS)


def has_citation(text: str) -> bool:
    """A source reads as '— JIDA Oct 2026 p.14' or 'DCI circular 2026-11-04'."""
    if re.search(r"[—–-]\s*[A-Z][\w&.'()/ ]{3,}\s*(?:,|\d{4}|p\.\s*\d+)", text or ""):
        return True
    return bool(re.search(
        r"\b(JIDA|DCI|IDA|ICMR|CDSCO|FDA|DGCI|GST Council|Google Trends|Practo|"
        r"circular|bulletin|partner update|launch note|per the .{3,30} (?:circular|alert|note))\b",
        text or "", re.I))


_IMPERATIVE_ASK = re.compile(
    r"\b(?:reply\s+(?:yes|no|confirm|stop|1|2)|say\s+(?:yes|the\s+word)|tell\s+me|send\s+me|"
    r"let\s+me\s+know|one\s+line\s+back|correct\s+me|in\s+one\s+line)\b", re.I)


def count_asks(text: str) -> int:
    """How many separate decisions the merchant is being asked to make.

    Counted per sentence, not per marker: "Correct me in one line" is one ask even though
    two phrases in it look like asks, while "Want A? Reply YES for B?" is two.
    """
    return sum(1 for sentence in split_sentences(text or "")
               if "?" in sentence or _IMPERATIVE_ASK.search(sentence))


def context_vocabulary(sheet: FactSheet | None) -> set[str]:
    """Every word the contexts themselves supplied.

    Two messages built from the same merchant and the same digest item necessarily share
    the facts — the offer title, the slot labels, the trial size, the source name. Reusing
    those is grounding, not copying. Only the wording *around* them can be plagiarised, so
    context words are removed from both sides before the comparison.
    """
    if sheet is None:
        return set()
    blob: list[str] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str):
            blob.append(node)

    for payload in (sheet.category, sheet.merchant, sheet.trigger_raw, sheet.customer or {}):
        walk(payload)
    return token_set(" ".join(blob))


_FIGURE = re.compile(r"\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?")


def shares_claim(body: str, claim_text: str, trial_n: int | None = None) -> bool:
    """Is the message actually making the claim it is about to cite?

    A source line backs a claim, and a claim is either a figure ("38% lower recurrence") or
    a distinctive phrase ("the lowest acquisition window of the year"). Advice derived from
    a finding is not the finding, so callers pass title + summary only — quoting an item's
    "actionable" while dropping its evidence does not earn its citation.
    """
    figures = set(_FIGURE.findall(claim_text or ""))
    if trial_n:
        figures |= {str(trial_n), f"{trial_n:,}"}
    if figures & set(_FIGURE.findall(body or "")):
        return True
    return bool(_ngrams(body, 3) & _ngrams(claim_text, 3))


def plagiarism_score(text: str, context_terms: set[str] | None = None) -> float:
    """High only when the message reuses a case study's *own wording*.

    Facts that both the case study and this message pulled from the same context are
    discounted first; what is left is the author's phrasing, and repeating that is copying.
    """
    context_terms = context_terms or set()
    residual = " ".join(w for w in normalise_for_compare(text).split()
                        if w not in context_terms)
    tokens = token_set(residual)
    if len(tokens) < 4:
        return 0.0
    grams = _ngrams(residual)
    best = 0.0
    for raw_case, case_grams_full in zip(CASE_STUDY_BODIES, _CASE_NGRAMS):
        case_residual = " ".join(w for w in normalise_for_compare(raw_case).split()
                                 if w not in context_terms)
        case_tokens = token_set(case_residual)
        case_grams = _ngrams(case_residual)
        if len(case_tokens) < 4:
            continue
        shared = tokens & case_tokens
        overlap = len(shared) / max(1, len(tokens))
        verbatim = bool(grams & case_grams)
        # A short message can share a lot of vocabulary by coincidence; require either a
        # literal repeated phrase or a large absolute overlap before calling it copying.
        if not verbatim and len(shared) < 12:
            overlap *= 0.5
        if verbatim:
            overlap = max(overlap, 0.75)
        best = max(best, overlap)
    return best


def drop_sentences_with(text: str, offenders: Sequence[str]) -> tuple[str, list[str]]:
    """Remove only the sentences that carry an unaccountable claim."""
    if not offenders:
        return text, []
    sentences = split_sentences(text or "")
    kept, dropped = [], []
    for sentence in sentences:
        if any(token in sentence for token in offenders):
            dropped.append(squeeze(sentence))
        else:
            kept.append(sentence)
    return squeeze(" ".join(kept)), dropped


# --------------------------------------------------------------------------- #
# the pass
# --------------------------------------------------------------------------- #

def check(body: str, sheet: FactSheet | None = None, *,
          ledger: NumberLedger | None = None,
          taboos: Sequence[str] = (), allowed_vocab: Sequence[str] = (),
          seen_bodies: Iterable[str] = (), require_cta: bool = True,
          strict_numbers: bool = True) -> GuardReport:
    """Repair what can be repaired, block what cannot."""
    ledger = ledger or (sheet.ledger if sheet else None)
    taboos = list(taboos) or (sheet.taboos if sheet else [])
    allowed_vocab = list(allowed_vocab) or (sheet.vocab if sheet else [])

    report = GuardReport(body=squeeze(body or ""))
    if not report.body:
        report.ok = False
        report.blocking.append("empty body")
        return report

    # 1. URLs — hard fail if left in, so strip them outright
    cleaned, had_url = strip_urls(report.body)
    if had_url:
        report.body = cleaned
        report.repaired.append("removed a URL")

    # 2. internal jargon
    jargon = find_jargon(report.body, allowed_vocab)
    if jargon:
        report.body = soften_jargon(report.body, allowed_vocab)
        still = find_jargon(report.body, allowed_vocab)
        report.repaired.append("softened jargon: " + ", ".join(jargon[:4]))
        if still:
            report.ok = False
            report.blocking.append("jargon survives: " + ", ".join(still[:4]))

    # 3. leaked placeholders — a message must never contain the word "None"
    if re.search(r"(?<![A-Za-z])(?:None|null|undefined|nan|placeholder)(?![A-Za-z])",
                 report.body):
        report.ok = False
        report.blocking.append("unrendered placeholder value in the body")

    # 4. category taboos
    taboo_hits = find_taboos(report.body, taboos)
    if taboo_hits:
        report.ok = False
        report.blocking.append("taboo language: " + ", ".join(taboo_hits))

    # 5. unaccountable numbers
    if ledger is not None and strict_numbers:
        offenders = ledger.unknown_numbers(report.body)
        if offenders:
            trimmed, dropped = drop_sentences_with(report.body, offenders)
            if trimmed and len(trimmed) > 60:
                report.body = trimmed
                report.repaired.append("dropped ungrounded claim(s): " + ", ".join(offenders[:4]))
            else:
                report.ok = False
                report.blocking.append("ungrounded numbers: " + ", ".join(offenders[:4]))

    # 6. citation discipline
    if needs_citation(report.body) and not has_citation(report.body):
        report.note("research/compliance claim without a visible source")

    # 7. one ask
    asks = count_asks(report.body)
    if asks > 1:
        report.note(f"{asks} separate asks in one message")
    if require_cta and asks == 0:
        report.note("no explicit next step")

    # 8. anti-repetition
    normalised = normalise_for_compare(report.body)
    for previous in seen_bodies:
        if not previous:
            continue
        if normalise_for_compare(previous) == normalised or jaccard(previous, report.body) > 0.82:
            report.ok = False
            report.blocking.append("repeats a message already sent in this thread")
            break

    # 9. plagiarism against the published case studies
    similarity = plagiarism_score(report.body, context_vocabulary(sheet))
    if similarity >= PLAGIARISM_THRESHOLD:
        report.ok = False
        report.blocking.append(f"too close to a published case study ({similarity:.2f})")
    elif similarity >= PLAGIARISM_THRESHOLD - 0.08:
        report.note(f"drifting towards a case-study phrasing ({similarity:.2f})")

    return report
