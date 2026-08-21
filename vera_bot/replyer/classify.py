"""Inbound-message classification.

The brief names two production failures this has to beat. 40-70% of "merchant replies" are
the merchant's own WhatsApp Business auto-reply, and production Vera burns two or three
turns on each. And when a merchant says "let's do it", production Vera asks another
qualifying question instead of starting the work.

Both are classification problems before they are generation problems, so this runs first
and deterministically. Precedence is explicit: an opt-out beats hostility, hostility beats
an auto-reply, and a commitment beats the question mark it happens to end with
("Ok lets do it. Whats next?" is a commitment, not a question).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..utils import normalise_for_compare, squeeze

# --------------------------------------------------------------------------- #
# lexicons
# --------------------------------------------------------------------------- #

OPT_OUT = (
    r"stop (?:messag|send|text|contact|call)", r"\bstop\b(?!\s*(?:by|in|loss))",
    r"do ?n[o']?t (?:message|send|contact|call|text)", r"unsubscribe", r"remove me",
    r"opt ?out", r"leave me alone", r"never (?:message|contact|call)",
    r"not interested", r"no longer interested",
    r"band kar", r"mat bhej", r"mat karo", r"nahi chahiye", r"bas karo",
)

HOSTILE = (
    r"\buseless\b", r"\bspam\b", r"\bbother(?:ing|ed)?\b", r"\brubbish\b", r"\bnonsense\b",
    r"waste of (?:my )?time", r"\bstupid\b", r"\bidiot\b", r"shut up", r"\bannoying\b",
    r"\bfed up\b", r"\bbakwas\b", r"\bbekaar\b", r"\bfaltu\b", r"pareshan",
)

AUTO_REPLY_STRONG = (
    r"thank(?:s| you) for (?:contacting|reaching|your message|writing)",
    r"we (?:have )?received your (?:message|query|enquiry)",
    r"(?:our|the) team will (?:respond|revert|get back|contact)",
    r"will (?:get back|revert|respond) to you (?:shortly|soon|asap)",
    r"this is an? (?:automated|auto)[- ]?(?:reply|response|message|assistant)",
    r"\bauto[- ]?reply\b", r"i am an? automated", r"currently (?:away|unavailable|closed)",
    r"outside (?:our )?(?:business|working|office) hours",
    r"we are closed (?:right )?now", r"away from (?:my|the) (?:desk|phone)",
    r"aapki (?:jaankari|baat|sujhaav)", r"hamari team (?:tak|se)",
    r"dhanyavaad.{0,40}team", r"shukriya.{0,40}team",
    r"for (?:more information|any query).{0,30}(?:visit|call)",
)
AUTO_REPLY_WEAK = (
    r"^thank(?:s| you)[.! ]", r"we appreciate your", r"your (?:query|request) is important",
    r"business hours", r"working hours", r"kindly wait",
)

COMMIT = (
    r"let'?s do it", r"lets do it", r"\bgo ahead\b", r"\bdo it\b", r"\bplease do\b",
    r"yes,? (?:please|do|send|go|sure)", r"^yes\b", r"^yeah\b", r"^yep\b", r"^ok(?:ay)?\b",
    r"sounds good", r"sounds great", r"\bi'?m in\b", r"count me in", r"\bproceed\b",
    r"\bconfirm(?:ed)?\b", r"go for it", r"make it happen", r"send it", r"send me",
    r"share it", r"draft it", r"set it up", r"start it", r"\bwhat'?s next\b", r"\bwhats next\b",
    r"\bhaan\b", r"\bhaanji\b", r"\bkar do\b", r"\bkar dijiye\b", r"\bchalo\b",
    r"theek hai", r"\bbhej do\b", r"\bbhejiye\b", r"\bkariye\b", r"\bthik hai\b",
)

#: "Mujhe magicpin judrna hai" — the brief's Pattern D failure. An explicit join/sign-up
#: intent must route straight to action, never back to another qualifying question.
JOIN_INTENT = (
    r"want to (?:join|sign ?up|register|list|partner)", r"\bjoin (?:magicpin|you|karna)\b",
    r"\bsign me up\b", r"\bregister (?:me|my)\b", r"list my (?:business|shop|store)",
    r"\bonboard\b", r"\bjud\s?(?:na|rna|ana|ne)\b", r"\bjur\s?(?:na|rna)\b",
    r"\bshuru kar\b", r"how (?:do|can) i (?:join|sign ?up|register)",
)

OFF_TOPIC = (
    r"\bgst\b", r"income tax", r"\bitr\b", r"\btds\b", r"\bpan card\b", r"\baadhaar\b",
    r"\bloan\b", r"\binsurance\b", r"bank account", r"\bvisa\b", r"\bpassport\b",
    r"electricity bill", r"\brecharge\b", r"rent agreement", r"legal notice",
    r"\btrademark\b", r"\bhiring\b", r"\brecruit", r"\bsalary\b", r"labour licen",
    r"shop (?:act|licen)", r"\bfssai\b", r"\bmsme\b", r"\budyam\b",
)

OBJECTION_PRICE = (
    r"too (?:expensive|costly|much)", r"\bexpensive\b", r"\bcostly\b", r"no budget",
    r"can'?t afford", r"\bmehenga\b", r"\bmehnga\b", r"paisa nahi", r"budget nahi",
    r"what(?:'?s| is) the (?:cost|price|charge)", r"how much (?:does|will) (?:it|this) cost",
)
OBJECTION_VALUE = (
    r"does(?:n'?t| not) work", r"did(?:n'?t| not) work", r"no results", r"tried (?:this|that) before",
    r"waste", r"not useful", r"no use", r"kaam nahi",
)

DEFER = (
    r"\blater\b", r"next week", r"next month", r"\btomorrow\b", r"\bkal\b", r"\bbaad mein\b",
    r"\babhi nahi\b", r"\bbusy\b", r"no time", r"not (?:right )?now", r"call me",
    r"give me (?:a )?(?:day|week|time)", r"remind me",
)

HUMAN = (
    r"talk to (?:a |some)?(?:person|human|someone|manager|executive)",
    r"speak to (?:a |some)?(?:person|human|someone|manager)",
    r"\breal person\b", r"customer care", r"\bhelpline\b", r"connect me to",
)

ACK = (r"^(?:thanks|thank you|ok|okay|noted|sure|got it|good|great|nice|fine)\b",
       r"^(?:theek hai|thik hai|acha|accha|ji|ji haan|shukriya|dhanyavaad)\b",
       r"^[👍🙏😊🙂✅]+$", r"\b(?:that works|works for me|perfect|sounds fine)\b")

NEGATIVE = (r"^no\b", r"\bnot now\b", r"\bmaybe later\b", r"\bnahi\b", r"don'?t need",
            r"\bno need\b", r"we'?re good", r"already (?:have|doing)")

QUESTION_LEAD = (r"^(?:what|how|why|when|where|who|which|can|could|will|would|is|are|do|does|"
                 r"kya|kaise|kab|kahan|kaun|kitna|kitne)\b",)

#: Latin-script Hindi. Split by how much each word proves on its own: "kitna" appears in
#: no English sentence, while "se" and "ke" turn up inside ordinary English words often
#: enough that one alone means nothing.
HINGLISH_STRONG = frozenset({
    "kitna", "kitne", "kaise", "kaisa", "kyun", "kyu", "mujhe", "aapko", "aapka", "aapke",
    "chahiye", "karo", "kariye", "kijiye", "bhej", "bhejiye", "batao", "bataiye", "haan",
    "nahi", "nahin", "theek", "thik", "accha", "acha", "shukriya", "dhanyavaad", "namaste",
    "lagega", "hoga", "milega", "sakta", "sakte", "hamein", "abhi", "baad", "jaldi",
    "zyada", "kam", "paisa", "mehenga", "sasta", "dukaan", "grahak",
})
HINGLISH_WEAK = frozenset({
    "hai", "hain", "kya", "aap", "kar", "karna", "mein", "kab", "koi", "bata", "dekh",
    "lekin", "hum", "mera", "meri", "yeh", "woh", "bhi", "se", "ko", "ka", "ki", "ke",
})
HINGLISH_MARKERS = tuple(HINGLISH_STRONG | HINGLISH_WEAK)


@dataclass
class Classification:
    intent: str
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    hinglish: bool = False
    is_question: bool = False
    matched_terms: list[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.intent in ("opt_out",)


def _hits(text: str, patterns: tuple[str, ...]) -> list[str]:
    found = []
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            found.append(squeeze(match.group(0)))
    return found


def detect_hinglish(text: str) -> bool:
    """One unmistakable Hindi word is enough; ambiguous ones need corroboration."""
    words = set(normalise_for_compare(text).split())
    if words & HINGLISH_STRONG:
        return True
    return len(words & HINGLISH_WEAK) >= 2


def classify(message: str, *, previous_inbound: list[str] = (),
             merchant_auto_reply_texts: list[str] = ()) -> Classification:
    raw = squeeze(message or "")
    text = raw.lower()
    if not text:
        return Classification("empty", 1.0, ["no content"])

    hinglish = detect_hinglish(raw)
    is_question = text.rstrip().endswith("?") or bool(_hits(text, QUESTION_LEAD))
    normalised = normalise_for_compare(raw)

    # 1. explicit opt-out beats everything, including hostility
    opt = _hits(text, OPT_OUT)
    if opt:
        return Classification("opt_out", 0.97, [f"opt-out phrase: {opt[0]}"],
                              hinglish, is_question, opt)

    # 2. hostility without an opt-out: de-escalate rather than close
    hostile = _hits(text, HOSTILE)
    if hostile:
        return Classification("hostile", 0.9, [f"hostile phrase: {hostile[0]}"],
                              hinglish, is_question, hostile)

    # 3. auto-reply: canned phrasing, or the same text this merchant already sent
    strong = _hits(text, AUTO_REPLY_STRONG)
    weak = _hits(text, AUTO_REPLY_WEAK)
    repeated = normalised in [normalise_for_compare(t) for t in previous_inbound] \
        or normalised in [normalise_for_compare(t) for t in merchant_auto_reply_texts]
    if strong or (weak and repeated) or (repeated and len(normalised.split()) > 4):
        evidence = [f"canned phrasing: {strong[0]}"] if strong else []
        if repeated:
            evidence.append("identical text already received from this merchant")
        return Classification("auto_reply", 0.93 if strong else 0.75, evidence,
                              hinglish, is_question, strong + weak)

    # 4. commitment — checked before questions, because "ok let's do it, what's next?"
    #    is a green light with a question mark attached
    commit = _hits(text, COMMIT)
    if commit:
        return Classification("commit", 0.92, [f"commitment phrase: {commit[0]}"],
                              hinglish, is_question, commit)

    # 5. explicit sign-up intent — route to action, never back to qualification
    join = _hits(text, JOIN_INTENT)
    if join:
        return Classification("join_intent", 0.9, [f"explicit sign-up intent: {join[0]}"],
                              hinglish, is_question, join)

    # 6. wants a human
    human = _hits(text, HUMAN)
    if human:
        return Classification("human_handoff", 0.85, [f"asked for a person: {human[0]}"],
                              hinglish, is_question, human)

    # 7. out of scope
    off = _hits(text, OFF_TOPIC)
    if off:
        return Classification("off_topic", 0.85, [f"out-of-scope topic: {off[0]}"],
                              hinglish, is_question, off)

    # 8. price: a question about cost is not the same as pushing back on it
    price = _hits(text, OBJECTION_PRICE)
    if price:
        intent = "question_price" if is_question else "objection_price"
        return Classification(intent, 0.8, [f"price raised: {price[0]}"],
                              hinglish, is_question, price)
    value = _hits(text, OBJECTION_VALUE)
    if value:
        return Classification("objection_value", 0.8, [f"value objection: {value[0]}"],
                              hinglish, is_question, value)

    # 9. deferral
    defer = _hits(text, DEFER)
    if defer:
        return Classification("defer", 0.75, [f"asked for time: {defer[0]}"],
                              hinglish, is_question, defer)

    # 10. plain no
    negative = _hits(text, NEGATIVE)
    if negative:
        return Classification("negative", 0.75, [f"declined: {negative[0]}"],
                              hinglish, is_question, negative)

    # 11. acknowledgement — short, positive, and asking for nothing
    if _hits(text, ACK) and len(text.split()) <= 8 and not is_question:
        return Classification("ack", 0.7, ["short acknowledgement"], hinglish, is_question)

    # 12. a real question
    if is_question:
        return Classification("question", 0.7, ["reads as a question"], hinglish, True)

    return Classification("unknown", 0.4, ["no decisive signal"], hinglish, is_question)
