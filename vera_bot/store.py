"""Versioned, thread-safe context store.

The judge pushes context incrementally and expects three things:
  * idempotency on (scope, context_id, version)
  * atomic replacement when a higher version arrives
  * the bot to *notice* what changed, because mid-test injections are scored

The third point is why this is more than a dict: every accepted write records a
structured diff against the version it replaced, so the composer can say "your calls
moved from 18 to 31 since Monday" instead of silently re-composing stale copy.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from .utils import as_list, dig, iso, parse_iso, utcnow

Scope = Literal["category", "merchant", "customer", "trigger"]
SCOPES: tuple[str, ...] = ("category", "merchant", "customer", "trigger")

# Fields whose movement is worth mentioning in a message, and how to name them.
_TRACKED_METRICS = {
    ("performance", "views"): "profile views",
    ("performance", "calls"): "calls",
    ("performance", "directions"): "direction requests",
    ("performance", "leads"): "leads",
    ("performance", "ctr"): "listing conversion",
    ("subscription", "days_remaining"): "days left on the plan",
}


@dataclass
class ContextChange:
    """What moved between two versions of the same context."""

    context_id: str
    scope: str
    from_version: int
    to_version: int
    at: str
    metrics: dict[str, tuple[Any, Any]] = field(default_factory=dict)   # label -> (old, new)
    added_digest: list[dict] = field(default_factory=list)
    added_offers: list[dict] = field(default_factory=list)
    added_signals: list[str] = field(default_factory=list)
    state_changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    @property
    def is_material(self) -> bool:
        return bool(self.metrics or self.added_digest or self.added_offers
                    or self.added_signals or self.state_changes)


@dataclass
class Entry:
    scope: str
    context_id: str
    version: int
    payload: dict
    delivered_at: str
    stored_at: str
    change: ContextChange | None = None
    revisions: int = 1


def _diff(scope: str, context_id: str, old: dict, new: dict,
          from_version: int, to_version: int) -> ContextChange:
    change = ContextChange(context_id=context_id, scope=scope, from_version=from_version,
                           to_version=to_version, at=iso())

    for path, label in _TRACKED_METRICS.items():
        before, after = dig(old, *path), dig(new, *path)
        if before is None or after is None or before == after:
            continue
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            change.metrics[label] = (before, after)

    def _ids(items: Iterable, key: str = "id") -> set:
        return {i.get(key) for i in items if isinstance(i, dict) and i.get(key)}

    old_digest, new_digest = as_list(old.get("digest")), as_list(new.get("digest"))
    known = _ids(old_digest)
    change.added_digest = [d for d in new_digest
                           if isinstance(d, dict) and d.get("id") and d["id"] not in known]

    old_offers, new_offers = as_list(old.get("offers")), as_list(new.get("offers"))
    known_offers = _ids(old_offers)
    change.added_offers = [o for o in new_offers
                           if isinstance(o, dict) and o.get("id") and o["id"] not in known_offers]

    old_signals = {s for s in as_list(old.get("signals")) if isinstance(s, str)}
    change.added_signals = [s for s in as_list(new.get("signals"))
                            if isinstance(s, str) and s not in old_signals]

    for path in (("subscription", "status"), ("state",), ("identity", "verified")):
        before, after = dig(old, *path), dig(new, *path)
        if before is not None and after is not None and before != after:
            change.state_changes[".".join(path)] = (before, after)

    return change


class ContextStore:
    """All context the judge has pushed, plus what changed on each push."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], Entry] = {}
        self._recent_changes: list[ContextChange] = []
        self._pushes = 0

    # ---------------------------------------------------------------- writes
    def put(self, scope: str, context_id: str, version: int,
            payload: dict, delivered_at: str | None = None) -> tuple[bool, int, ContextChange | None]:
        """Returns (accepted, current_version, change)."""
        key = (scope, context_id)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and version <= existing.version:
                return False, existing.version, None

            change = None
            if existing is not None:
                change = _diff(scope, context_id, existing.payload, payload,
                               existing.version, version)
                if change.is_material:
                    self._recent_changes.append(change)
                    del self._recent_changes[:-200]

            self._entries[key] = Entry(
                scope=scope,
                context_id=context_id,
                version=version,
                payload=payload,
                delivered_at=delivered_at or iso(),
                stored_at=iso(),
                change=change,
                revisions=(existing.revisions + 1) if existing else 1,
            )
            self._pushes += 1
            return True, version, change

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._recent_changes.clear()

    # ---------------------------------------------------------------- reads
    def entry(self, scope: str, context_id: str | None) -> Entry | None:
        if not context_id:
            return None
        with self._lock:
            return self._entries.get((scope, context_id))

    def get(self, scope: str, context_id: str | None) -> dict | None:
        entry = self.entry(scope, context_id)
        return entry.payload if entry else None

    def version(self, scope: str, context_id: str | None) -> int:
        entry = self.entry(scope, context_id)
        return entry.version if entry else 0

    def change_for(self, scope: str, context_id: str | None) -> ContextChange | None:
        entry = self.entry(scope, context_id)
        if entry and entry.change and entry.change.is_material:
            return entry.change
        return None

    def all_of(self, scope: str) -> list[dict]:
        with self._lock:
            return [e.payload for e in self._entries.values() if e.scope == scope]

    def ids_of(self, scope: str) -> list[str]:
        with self._lock:
            return sorted(e.context_id for e in self._entries.values() if e.scope == scope)

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in SCOPES}
        with self._lock:
            for entry in self._entries.values():
                counts[entry.scope] = counts.get(entry.scope, 0) + 1
        return counts

    @property
    def total_pushes(self) -> int:
        return self._pushes

    # -------------------------------------------------------- domain lookups
    def merchant(self, merchant_id: str | None) -> dict | None:
        return self.get("merchant", merchant_id)

    def customer(self, customer_id: str | None) -> dict | None:
        return self.get("customer", customer_id)

    def trigger(self, trigger_id: str | None) -> dict | None:
        payload = self.get("trigger", trigger_id)
        if payload is None:
            return None
        # Some pushes nest the real trigger one level down under "payload".
        if "kind" not in payload and isinstance(payload.get("payload"), dict) \
                and "kind" in payload["payload"]:
            return payload["payload"]
        return payload

    def category(self, slug: str | None) -> dict | None:
        return self.get("category", slug)

    def category_for_merchant(self, merchant: dict | None) -> dict | None:
        if not merchant:
            return None
        slug = merchant.get("category_slug") or dig(merchant, "identity", "category")
        found = self.category(slug)
        if found is not None:
            return found
        # Fall back to inferring the vertical from the merchant id, which the
        # generated dataset encodes (`m_011_dr_sameer_dentist_bangalore`).
        mid = (merchant.get("merchant_id") or "").lower()
        for candidate in self.ids_of("category"):
            stem = candidate.rstrip("s")
            if stem and stem in mid:
                return self.category(candidate)
        return None

    def customers_of(self, merchant_id: str) -> list[dict]:
        return [c for c in self.all_of("customer") if c.get("merchant_id") == merchant_id]

    def triggers_for_merchant(self, merchant_id: str) -> list[dict]:
        out = []
        for tid in self.ids_of("trigger"):
            trg = self.trigger(tid)
            if trg and trg.get("merchant_id") == merchant_id:
                out.append(trg)
        return out

    def live_trigger_ids(self, now: Any = None) -> list[str]:
        """Trigger ids that have not expired as of `now`."""
        moment = parse_iso(now) or utcnow()
        live = []
        for tid in self.ids_of("trigger"):
            trg = self.trigger(tid)
            if not trg:
                continue
            expiry = parse_iso(trg.get("expires_at"))
            if expiry is None or expiry >= moment:
                live.append(tid)
        return live

    def recent_changes(self, limit: int = 20) -> list[ContextChange]:
        with self._lock:
            return list(self._recent_changes[-limit:])
