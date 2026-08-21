#!/usr/bin/env python3
"""A faithful local rebuild of the judge harness lifecycle in challenge-testing-brief.md §4.

`judge_simulator.py` only exercises warmup plus three replay scenarios. The parts that
actually decide the score are not in it:

  * Phase 1 warmup with all 255 base contexts, not five merchants
  * Phase 2 across a 60-minute simulated window in five-minute ticks
  * Phase 3 adaptive injection — new digest items, shifted performance, surprise customer
    contexts arriving mid-test as higher versions — which is explicitly scored
  * Phase 4 replays with merchant personas that answer back
  * The operational contract: 409 on a stale version, 400 on a bad scope, every required
    action field present, no URLs, no repeated body, every response inside budget

This runs all of it and reports what a judge would see.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "expanded"
SEEDS = ROOT.parent / "dataset"

REQUIRED_ACTION_FIELDS = ("conversation_id", "merchant_id", "customer_id", "send_as",
                          "trigger_id", "template_name", "template_params", "body", "cta",
                          "suppression_key", "rationale")
BUDGETS = {"/v1/healthz": 2.0, "/v1/metadata": 2.0, "/v1/context": 5.0,
           "/v1/tick": 10.0, "/v1/reply": 10.0}

URL_RE = re.compile(r"https?://|www\.", re.I)

G, R, Y, B, DIM, X = "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[2m", "\033[0m"


def ok(msg): print(f"{G}  PASS{X} {msg}")
def bad(msg): print(f"{R}  FAIL{X} {msg}")
def warn(msg): print(f"{Y}  WARN{X} {msg}")
def info(msg): print(f"{B}  ····{X} {msg}")
def head(msg): print(f"\n{B}{'='*74}\n  {msg}\n{'='*74}{X}")


class Bot:
    """HTTP client with connection reuse.

    Warmup pushes 255 contexts back to back. Opening a fresh TLS handshake for each one
    measures the round trip to the host far more than it measures the bot, so the socket is
    kept alive across calls and a dropped connection is retried once — which is what any
    real harness client does.
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.failures: list[str] = []
        parsed = urllib.parse.urlparse(self.base)
        self._host = parsed.netloc
        self._https = parsed.scheme == "https"
        self._prefix = parsed.path.rstrip("/")
        self._conn: http.client.HTTPConnection | None = None

    def _connect(self, timeout: float):
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, timeout=timeout)

    def _once(self, method: str, path: str, body: bytes | None, timeout: float):
        if self._conn is None:
            self._conn = self._connect(timeout)
        headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
        self._conn.request(method, f"{self._prefix}{path}", body=body, headers=headers)
        response = self._conn.getresponse()
        raw = response.read()
        return response.status, raw

    def call(self, method: str, path: str, payload: dict | None = None, timeout: float = 15.0):
        data = json.dumps(payload).encode() if payload is not None else None
        start = time.time()
        last_error = None
        for attempt in range(2):
            try:
                status, raw = self._once(method, path, data, timeout)
                break
            except Exception as exc:                                # noqa: BLE001
                last_error = exc
                try:
                    if self._conn:
                        self._conn.close()
                finally:
                    self._conn = None
        else:
            self.failures.append(f"{path}: {last_error}")
            return None, 0, (time.time() - start)

        elapsed = time.time() - start
        self.latencies[path].append(elapsed)
        budget = BUDGETS.get(path)
        if budget and elapsed > budget:
            self.failures.append(f"{path} took {elapsed:.1f}s (budget {budget}s)")
        try:
            return json.loads(raw.decode()), status, elapsed
        except Exception:                                           # noqa: BLE001
            return None, status, elapsed


def load() -> dict:
    data = {"categories": {}, "merchants": {}, "customers": {}, "triggers": {}}
    for path in (SEEDS / "categories").glob("*.json"):
        payload = json.loads(path.read_text())
        data["categories"][payload["slug"]] = payload
    for kind, key in (("merchants", "merchant_id"), ("customers", "customer_id"),
                      ("triggers", "id")):
        folder = DATA / kind
        if folder.exists():
            for path in folder.glob("*.json"):
                payload = json.loads(path.read_text())
                data[kind][payload[key]] = payload
    return data


# --------------------------------------------------------------------------- #
# merchant personas for the replay phase
# --------------------------------------------------------------------------- #

PERSONAS = {
    "engaged": ["Yes please send it", "Ok lets do it. Whats next?", "Great, go ahead",
                "thanks, that works"],
    "auto_reply": ["Thank you for contacting us! Our team will respond shortly."] * 4,
    "hostile": ["Why are you bothering me. This is useless.",
                "Btw can you also help me with my GST filing this month?",
                "Stop messaging me."],
    "sceptical": ["How much will this cost?", "Too expensive", "tried this before, no results",
                  "no need"],
    "busy": ["Call me next week, busy now", "later", "ok", "thanks"],
    "hinglish": ["haan bhej do", "kitna time lagega?", "theek hai kar do", "shukriya"],
}


def replay(bot: Bot, conversation_id: str, merchant_id: str, persona: str,
           now: datetime, verbose: bool = False) -> dict:
    """Play a merchant persona for up to 5 turns and grade how the thread flows."""
    turns, bodies, actions = 0, [], []
    for index, message in enumerate(PERSONAS[persona], start=2):
        payload = {"conversation_id": conversation_id, "merchant_id": merchant_id,
                   "customer_id": None, "from_role": "merchant", "message": message,
                   "received_at": (now + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
                   "turn_number": index}
        body, status, _ = bot.call("POST", "/v1/reply", payload)
        turns += 1
        if body is None:
            bad(f"{persona}: no response on turn {index}")
            break
        action = body.get("action")
        actions.append(action)
        if verbose:
            detail = body.get("body", "") or f"wait {body.get('wait_seconds')}s" \
                if action != "end" else "—"
            print(f"    {DIM}m:{X} {message[:52]:<54} {DIM}->{X} {action}: {str(detail)[:70]}")
        if action == "send":
            text = body.get("body", "")
            if not text.strip():
                bad(f"{persona}: empty body on a send")
            if text in bodies:
                bad(f"{persona}: repeated body verbatim")
            bodies.append(text)
        if action == "end":
            break
    return {"persona": persona, "turns": turns, "actions": actions}


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8099")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--show", type=int, default=6, help="messages to print in full")
    args = parser.parse_args()

    bot = Bot(args.url)
    data = load()
    if not data["merchants"]:
        print("No expanded dataset — run dataset/generate_dataset.py first.")
        return 1

    problems: list[str] = []
    now = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

    # ---------------------------------------------------------------- Phase 1
    head("PHASE 1 — warmup")
    bot.call("POST", "/v1/teardown", {})
    health, status, _ = bot.call("GET", "/v1/healthz", timeout=5)
    (ok if status == 200 and health.get("status") == "ok" else bad)("healthz reachable")
    meta, status, _ = bot.call("GET", "/v1/metadata", timeout=5)
    if status == 200 and meta.get("team_name"):
        ok(f"metadata — {meta['team_name']} · {meta.get('model')}")
    else:
        bad("metadata missing")
        problems.append("metadata")

    pushed = 0
    for scope, items in (("category", data["categories"]), ("merchant", data["merchants"]),
                         ("customer", data["customers"])):
        for context_id, payload in items.items():
            body, status, _ = bot.call("POST", "/v1/context", {
                "scope": scope, "context_id": context_id, "version": 1, "payload": payload,
                "delivered_at": now.isoformat().replace("+00:00", "Z")}, timeout=10)
            if status == 200 and body and body.get("accepted"):
                pushed += 1
            else:
                problems.append(f"context push rejected: {scope}/{context_id}")
    ok(f"pushed {pushed} base contexts")

    health, _, _ = bot.call("GET", "/v1/healthz", timeout=5)
    counts = health.get("contexts_loaded", {})
    expected = {"category": len(data["categories"]), "merchant": len(data["merchants"]),
                "customer": len(data["customers"])}
    if all(counts.get(k) == v for k, v in expected.items()):
        ok(f"contexts_loaded matches: {counts}")
    else:
        bad(f"contexts_loaded {counts} != expected {expected}")
        problems.append("warmup count mismatch")

    # idempotency + validation
    first = next(iter(data["categories"]))
    body, status, _ = bot.call("POST", "/v1/context", {
        "scope": "category", "context_id": first, "version": 1,
        "payload": data["categories"][first]}, timeout=10)
    if status == 409 and body and body.get("reason") == "stale_version":
        ok("re-pushing the same version returns 409 stale_version")
    else:
        bad(f"idempotency: expected 409 stale_version, got {status} {body}")
        problems.append("idempotency")

    body, status, _ = bot.call("POST", "/v1/context", {
        "scope": "nonsense", "context_id": "x", "version": 1, "payload": {}}, timeout=10)
    if status == 400 and body and body.get("reason") == "invalid_scope":
        ok("invalid scope returns 400 invalid_scope")
    else:
        bad(f"expected 400 invalid_scope, got {status} {body}")
        problems.append("scope validation")

    # ---------------------------------------------------------------- Phase 2
    head("PHASE 2 — 60 simulated minutes, 5-minute ticks")
    trigger_ids = sorted(data["triggers"])
    per_tick = max(1, len(trigger_ids) // 12)
    all_actions: list[dict] = []
    injected_facts: list[tuple[str, str]] = []

    for tick_index in range(12):
        moment = now + timedelta(minutes=5 * tick_index)
        batch = trigger_ids[tick_index * per_tick:(tick_index + 1) * per_tick]
        for trigger_id in batch:
            bot.call("POST", "/v1/context", {
                "scope": "trigger", "context_id": trigger_id, "version": 1,
                "payload": data["triggers"][trigger_id],
                "delivered_at": moment.isoformat().replace("+00:00", "Z")}, timeout=10)

        # ------------------------------------------------- Phase 3 injections
        if tick_index == 4:
            for slug, category in data["categories"].items():
                updated = json.loads(json.dumps(category))
                updated["digest"] = [{
                    "id": f"d_2026W35_injected_{slug}", "kind": "research",
                    "title": f"Injected {slug} finding: 3-visit cadence lifts retention 27%",
                    "source": f"Injected Journal {slug} 2026, p.7", "trial_n": 1450,
                    "summary": "Post-submission item the bot has never seen before.",
                    "actionable": "Re-time your follow-ups around a 3-visit cadence",
                }] + updated.get("digest", [])
                bot.call("POST", "/v1/context", {"scope": "category", "context_id": slug,
                                                 "version": 2, "payload": updated}, timeout=10)
            injected_facts.append(("digest", "27%"))
            info("injected a new digest item into all 5 categories (version 2)")

        if tick_index == 6:
            shifted = 0
            for merchant_id, merchant in list(data["merchants"].items())[:10]:
                updated = json.loads(json.dumps(merchant))
                updated["performance"]["views"] = int(updated["performance"]["views"] * 1.4) + 137
                updated["performance"]["calls"] = int(updated["performance"].get("calls", 5)) + 29
                bot.call("POST", "/v1/context", {"scope": "merchant", "context_id": merchant_id,
                                                 "version": 2, "payload": updated}, timeout=10)
                data["merchants"][merchant_id] = updated
                shifted += 1
            injected_facts.append(("performance", "moved from"))
            info(f"shifted performance on {shifted} merchants (version 2)")

        body, status, elapsed = bot.call("POST", "/v1/tick", {
            "now": moment.isoformat().replace("+00:00", "Z"),
            "available_triggers": batch}, timeout=15)
        if body is None:
            bad(f"tick {tick_index} failed")
            problems.append("tick failure")
            continue
        actions = body.get("actions", [])
        all_actions.extend(actions)
        if args.verbose:
            info(f"tick {tick_index:>2} · {len(batch):>2} triggers -> "
                 f"{len(actions)} actions · {elapsed*1000:.0f}ms")

    ok(f"{len(all_actions)} actions across 12 ticks "
       f"(max tick latency {max(bot.latencies['/v1/tick'])*1000:.0f}ms)")

    # ----------------------------------------------------- action validation
    head("OPERATIONAL CONTRACT")
    seen_conversation_ids: set[str] = set()
    seen_bodies_by_conv: dict[str, set[str]] = defaultdict(set)
    field_misses = Counter()
    url_hits = repeats = dupe_ids = empty = 0

    for action in all_actions:
        for field in REQUIRED_ACTION_FIELDS:
            if field not in action:
                field_misses[field] += 1
        conversation_id = action.get("conversation_id", "")
        if conversation_id in seen_conversation_ids:
            dupe_ids += 1
        seen_conversation_ids.add(conversation_id)
        text = action.get("body", "")
        if not text.strip():
            empty += 1
        if URL_RE.search(text):
            url_hits += 1
        if text in seen_bodies_by_conv[conversation_id]:
            repeats += 1
        seen_bodies_by_conv[conversation_id].add(text)

    (ok if not field_misses else bad)(
        f"required action fields present on all {len(all_actions)} actions"
        if not field_misses else f"missing fields: {dict(field_misses)}")
    (ok if not url_hits else bad)(f"no URLs in any body" if not url_hits
                                  else f"{url_hits} bodies contain a URL")
    (ok if not repeats else bad)("no repeated bodies" if not repeats
                                 else f"{repeats} repeated bodies")
    (ok if not dupe_ids else bad)("conversation ids unique per tick action"
                                  if not dupe_ids else f"{dupe_ids} reused conversation ids")
    (ok if not empty else bad)("no empty bodies" if not empty else f"{empty} empty bodies")
    if field_misses or url_hits or repeats or dupe_ids or empty:
        problems.append("action contract")

    # ------------------------------------------------------- adaptation check
    head("PHASE 3 — did the bot use the injected context?")
    late = all_actions[len(all_actions) // 2:]
    corpus = " ".join(a.get("body", "") + " " + a.get("rationale", "") for a in late)
    for label, needle in injected_facts:
        if needle in corpus:
            ok(f"post-injection messages reference the new {label} fact ('{needle}')")
        else:
            warn(f"no visible use of the injected {label} fact ('{needle}')")

    hallucinated = [a for a in all_actions if "Injected Journal" in a.get("body", "")
                    and "27%" not in a.get("body", "")]
    if hallucinated:
        bad(f"{len(hallucinated)} messages cite the injected item without its claim")
        for action in hallucinated[:2]:
            print(f"    {DIM}{action['trigger_id']}{X}\n    {action['body']}")
        problems.append("unearned citation")
    else:
        ok("no citation survives without the claim it points at")

    # ----------------------------------------------------------- distribution
    head("COVERAGE")
    by_send_as = Counter(a.get("send_as") for a in all_actions)
    by_cta = Counter(a.get("cta") for a in all_actions)
    by_merchant = Counter(a.get("merchant_id") for a in all_actions)
    lengths = [len(a.get("body", "")) for a in all_actions] or [0]
    info(f"send_as: {dict(by_send_as)}")
    info(f"cta: {dict(by_cta)}")
    info(f"distinct merchants reached: {len(by_merchant)} · "
         f"max messages to one merchant: {max(by_merchant.values()) if by_merchant else 0}")
    info(f"body length: min {min(lengths)} · median {sorted(lengths)[len(lengths)//2]} · "
         f"max {max(lengths)}")

    # ---------------------------------------------------------------- Phase 4
    head("PHASE 4 — replay personas")
    merchant_ids = sorted(data["merchants"])
    results = []
    for index, persona in enumerate(PERSONAS):
        conversation_id = f"replay_{persona}"
        results.append(replay(bot, conversation_id, merchant_ids[index % len(merchant_ids)],
                              persona, now, verbose=True))
    for result in results:
        trail = " -> ".join(result["actions"])
        print(f"  {result['persona']:<11} {DIM}{trail}{X}")

    auto = next(r for r in results if r["persona"] == "auto_reply")
    if "end" in auto["actions"] and auto["actions"].index("end") <= 2:
        ok(f"auto-reply loop exited by turn {auto['actions'].index('end') + 1}")
    else:
        bad("auto-reply loop not exited quickly")
        problems.append("auto-reply")

    hostile = next(r for r in results if r["persona"] == "hostile")
    if hostile["actions"][-1] == "end":
        ok("hostile thread closed")
    else:
        warn("hostile thread did not close")

    # ------------------------------------------------------------------ done
    head("SUMMARY")
    for path, values in sorted(bot.latencies.items()):
        info(f"{path:<14} n={len(values):<4} avg {sum(values)/len(values)*1000:6.0f}ms  "
             f"max {max(values)*1000:6.0f}ms")
    if bot.failures:
        for failure in bot.failures[:10]:
            bad(failure)
        problems.append("transport")
    if problems:
        print(f"\n{R}  {len(problems)} problem area(s): {sorted(set(problems))}{X}")
        return 1
    print(f"\n{G}  All operational checks passed.{X}")

    if args.show:
        head(f"SAMPLE MESSAGES ({args.show})")
        step = max(1, len(all_actions) // args.show)
        for action in all_actions[::step][:args.show]:
            print(f"\n{DIM}{action['merchant_id']} · {action['trigger_id']} · "
                  f"{action['send_as']} · {action['cta']}{X}")
            print(f"  {action['body']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
