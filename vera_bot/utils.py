"""Small deterministic helpers shared across the bot.

Everything here is pure and side-effect free so that composition stays reproducible:
the same contexts must always render the same message.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, TypeVar

T = TypeVar("T")

_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s,.;:—-]+$")


# --------------------------------------------------------------------------- #
# hashing / deterministic choice
# --------------------------------------------------------------------------- #

def stable_hash(*parts: Any) -> int:
    """A stable integer hash. Python's hash() is salted per process; this is not."""
    joined = "␟".join("" if p is None else str(p) for p in parts)
    return int(hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12], 16)


def pick(options: Sequence[T], *seed_parts: Any, offset: int = 0) -> T:
    """Deterministically choose one of `options` from a seed.

    Used to vary phrasing between merchants so that a rule-driven composer does not
    read like one template applied 50 times, while staying reproducible per merchant.
    """
    if not options:
        raise ValueError("pick() needs at least one option")
    return options[(stable_hash(*seed_parts) + offset) % len(options)]


def short_id(value: str, keep: int = 3) -> str:
    """`m_001_drmeera_dentist_delhi` -> `drmeera`; used for readable conversation ids."""
    if not value:
        return "unknown"
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", value) if p]
    # drop the leading type letter + numeric index (m / 001)
    meaningful = [p for p in parts if not p.isdigit() and len(p) > 1]
    if not meaningful:
        meaningful = parts
    return "_".join(meaningful[:keep]).lower()[:40] or "unknown"


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #

def indian_commas(value: int | float) -> str:
    """1234567 -> '12,34,567' (Indian digit grouping)."""
    neg = value < 0
    whole = str(int(abs(value)))
    if len(whole) <= 3:
        out = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        out = ",".join(groups) + "," + tail
    return ("-" if neg else "") + out


def num(value: Any) -> str:
    """Render a count the way a person writes it in a WhatsApp message."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return indian_commas(value)
    return str(value)


def rupees(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount.is_integer():
        return "₹" + indian_commas(int(amount))
    return "₹" + f"{amount:,.2f}"


def pct(value: Any, decimals: int = 0) -> str:
    """0.021 -> '2.1%'  (pass decimals=1 for one place)."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return str(value)
    scaled = ratio * 100.0
    if decimals == 0 and abs(scaled - round(scaled)) > 0.04:
        decimals = 1
    return f"{scaled:.{decimals}f}%"


def signed_pct(value: Any) -> str:
    """-0.5 -> 'down 50%' / 0.18 -> 'up 18%'."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{'up' if ratio >= 0 else 'down'} {pct(abs(ratio))}"


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #

def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(text[:10], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def human_date(value: Any, with_year: bool = False) -> str:
    """'2026-12-15' -> '15 Dec' (or '15 Dec 2026')."""
    dt = parse_iso(value)
    if not dt:
        return str(value or "")
    return dt.strftime("%-d %b %Y") if with_year else dt.strftime("%-d %b")


def days_between(later: Any, earlier: Any) -> int | None:
    a, b = parse_iso(later), parse_iso(earlier)
    if not a or not b:
        return None
    return (a - b).days


def months_between(later: Any, earlier: Any) -> int | None:
    delta = days_between(later, earlier)
    return None if delta is None else int(round(delta / 30.44))


def month_index(dt: datetime) -> int:
    return dt.month


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_range_covers(month_range: str, month: int) -> bool:
    """Does a seasonal beat like 'Nov-Feb' / 'Apr-Jun' / 'Feb 14' cover this month?"""
    if not month_range:
        return False
    tokens = [t for t in re.split(r"[^A-Za-z]+", month_range) if t]
    idxs = [MONTH_ABBR.index(t[:3].title()) + 1 for t in tokens if t[:3].title() in MONTH_ABBR]
    if not idxs:
        return False
    if len(idxs) == 1:
        return idxs[0] == month
    start, end = idxs[0], idxs[-1]
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end  # wraps the year, e.g. Nov-Feb


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

def squeeze(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()


_EMPTY_TOKENS = {"", "none", "null", "nan", "n/a", "{}", "[]", "placeholder", "undefined"}


def text(value: Any, default: str = "", *, humanise: bool = True) -> str:
    """Render a possibly-absent context value, never producing the literal word "None".

    Missing data is extremely common in this dataset, and a message that says
    "we can hold a None slot" is worse than one that omits the slot entirely — so every
    optional field goes through here on its way into prose.
    """
    if value is None or isinstance(value, bool):
        return default
    rendered = _WS.sub(" ", str(value)).strip()
    if rendered.lower() in _EMPTY_TOKENS:
        return default
    if humanise:
        rendered = rendered.replace("_", " ")
        # "saturday"/"sunday brunch" arrive lowercase from preference fields
        rendered = _WEEKDAY.sub(lambda m: m.group(0).title(), rendered)
    return rendered


_WEEKDAY = re.compile(r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)


#: Splitting on "." naively cuts "Dr. Meera" in half and loses the salutation, so the
#: split refuses to fire directly after a known abbreviation.
ABBREVS = ("dr", "mr", "mrs", "ms", "st", "no", "vs", "approx", "p", "pp", "rs",
           "eg", "ie", "etc", "jr", "sr", "col", "capt")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?!(?:" + "|".join(ABBREVS) + r")\.)", re.I)
_ABBREV_TAIL = re.compile(r"\b(?:" + "|".join(ABBREVS) + r")\.\s*$", re.I)


def split_sentences(text: str) -> list[str]:
    """Sentence split that keeps 'Dr. Meera' and 'p. 14' in one piece."""
    merged: list[str] = []
    for piece in _SENTENCE_SPLIT.split(squeeze(text)):
        if merged and _ABBREV_TAIL.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return [m for m in merged if m]


def join_sentences(parts: Iterable[str]) -> str:
    """Glue sentence fragments, dropping blanks and fixing spacing/punctuation."""
    out: list[str] = []
    for raw in parts:
        piece = squeeze(raw)
        if not piece:
            continue
        if not piece.endswith((".", "?", "!", ":", "—", "\n")):
            piece += "."
        out.append(piece)
    text = " ".join(out)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    text = re.sub(r"([.?!])\1+", r"\1", text)
    return squeeze(text)


def trim_trailing_punct(text: str) -> str:
    return _TRAILING_PUNCT.sub("", text or "")


def sentence_case(text: str) -> str:
    text = squeeze(text)
    return text[:1].upper() + text[1:] if text else text


def oxford(items: Sequence[str], conjunction: str = "and") -> str:
    items = [squeeze(i) for i in items if squeeze(i)]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return ", ".join(items[:-1]) + f" {conjunction} {items[-1]}"


def normalise_for_compare(text: str) -> str:
    """Lowercase, strip punctuation/digits — for anti-repetition + similarity checks."""
    lowered = (text or "").lower()
    lowered = re.sub(r"[^a-zऀ-ॿ\s]", " ", lowered)
    return _WS.sub(" ", lowered).strip()


def token_set(text: str) -> set[str]:
    return {t for t in normalise_for_compare(text).split() if len(t) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------- #
# dict access
# --------------------------------------------------------------------------- #

def dig(source: Any, *path: str, default: Any = None) -> Any:
    """Safe nested lookup: dig(merchant, 'identity', 'name')."""
    node = source
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return default if node is None else node


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def first_present(source: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = source.get(key) if isinstance(source, dict) else None
        if value not in (None, "", [], {}):
            return value
    return default
