#!/usr/bin/env python3
"""Submission conformance audit.

Replays every request/response example documented in `examples/api-call-examples.md`
verbatim against a running bot, plus the stated limits from the challenge site
(500 KB payload cap, 10 req/s, 20 actions/tick, 30 s timeout) and the failure modes in
that file's F.1-F.5.

This is a contract audit, not a quality score: it answers "does the bot do what the spec
says", one documented example at a time, so any gap is attributable to a line in the pack.

    python tools/verify_submission.py --url https://your-bot.example
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT.parent
SEEDS = PACK / "dataset"
EXPANDED = PACK / "expanded"

G, R, Y, B, DIM, X = "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[2m", "\033[0m"

RESULTS: list[tuple[str, str, str, str]] = []      # (section, ref, verdict, detail)


def record(section: str, ref: str, ok: bool | None, detail: str = "") -> bool:
    verdict = "PASS" if ok else ("WARN" if ok is None else "FAIL")
    RESULTS.append((section, ref, verdict, detail))
    colour = G if ok else (Y if ok is None else R)
    print(f"  {colour}{verdict:<4}{X} {ref:<46} {DIM}{detail[:78]}{X}")
    return bool(ok)


def head(title: str) -> None:
    print(f"\n{B}{'─' * 96}\n  {title}\n{'─' * 96}{X}")


class Client:
    def __init__(self, base: str) -> None:
        parsed = urllib.parse.urlparse(base.rstrip("/"))
        self.host, self.https = parsed.netloc, parsed.scheme == "https"
        self.prefix = parsed.path.rstrip("/")
        self._conn = None
        self.latency: dict[str, list[float]] = defaultdict(list)

    def _new(self, timeout):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, timeout=timeout)

    def call(self, method, path, payload=None, timeout=35.0, fresh=False):
        data = json.dumps(payload).encode() if payload is not None else None
        start = time.time()
        conn = self._new(timeout) if fresh else None
        for _ in range(2):
            try:
                target = conn or self._conn or self._new(timeout)
                if not conn:
                    self._conn = target
                target.request(method, f"{self.prefix}{path}", body=data,
                               headers={"Content-Type": "application/json",
                                        "Connection": "close" if fresh else "keep-alive"})
                response = target.getresponse()
                raw = response.read()
                status = response.status
                if conn:
                    conn.close()
                break
            except Exception as exc:                                # noqa: BLE001
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                if self._conn:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                last = exc
        else:
            return None, 0, time.time() - start, str(last)
        elapsed = time.time() - start
        self.latency[path].append(elapsed)
        try:
            return json.loads(raw.decode()), status, elapsed, None
        except Exception:
            return None, status, elapsed, "non-JSON response"


def load_pack():
    data = {"categories": {}, "merchants": {}, "customers": {}, "triggers": {}}
    for path in (SEEDS / "categories").glob("*.json"):
        payload = json.loads(path.read_text())
        data["categories"][payload["slug"]] = payload
    for name, key in (("merchants_seed.json", "merchant_id"),
                      ("customers_seed.json", "customer_id"),
                      ("triggers_seed.json", "id")):
        container = name.split("_")[0]
        for item in json.loads((SEEDS / name).read_text())[container]:
            data[container][item[key]] = item
    return data


ISO = lambda dt: dt.isoformat().replace("+00:00", "Z")                       # noqa: E731
NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

REQUIRED_ACTION_FIELDS = ("conversation_id", "merchant_id", "customer_id", "send_as",
                          "trigger_id", "template_name", "template_params", "body", "cta",
                          "suppression_key", "rationale")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    bot = Client(args.url)
    pack = load_pack()

    bot.call("POST", "/v1/teardown", {})

    # ══ 1. Warmup examples 1.1-1.7 ════════════════════════════════════════
    head("api-call-examples.md §1 — Warmup")

    body, status, ms, err = bot.call("GET", "/v1/healthz")
    record("warmup", "1.1 GET /v1/healthz → 200", status == 200 and body and body.get("status") == "ok",
           f"{ms*1000:.0f}ms")
    record("warmup", "1.1 healthz has uptime_seconds:int",
           isinstance((body or {}).get("uptime_seconds"), int))
    record("warmup", "1.1 contexts_loaded has all 4 scopes",
           set((body or {}).get("contexts_loaded", {})) == {"category", "merchant", "customer", "trigger"},
           str((body or {}).get("contexts_loaded")))
    record("warmup", "1.1 counts are zero before any push",
           all(v == 0 for v in (body or {}).get("contexts_loaded", {}).values()))

    body, status, ms, err = bot.call("GET", "/v1/metadata")
    for field in ("team_name", "team_members", "model", "approach", "contact_email",
                  "version", "submitted_at"):
        record("warmup", f"1.2 metadata.{field}", bool((body or {}).get(field)),
               str((body or {}).get(field))[:60])

    category = pack["categories"]["dentists"]
    body, status, ms, err = bot.call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 1,
        "payload": category, "delivered_at": ISO(NOW)})
    record("warmup", "1.3 POST /v1/context category → accepted",
           status == 200 and body and body.get("accepted") is True)
    record("warmup", "1.3 response carries ack_id + stored_at",
           bool((body or {}).get("ack_id")) and bool((body or {}).get("stored_at")),
           f"{(body or {}).get('ack_id')}")

    merchant = pack["merchants"]["m_001_drmeera_dentist_delhi"]
    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "merchant", "context_id": merchant["merchant_id"], "version": 1,
        "payload": merchant, "delivered_at": ISO(NOW)})
    record("warmup", "1.4 POST /v1/context merchant → accepted",
           status == 200 and body and body.get("accepted") is True)

    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "merchant", "context_id": merchant["merchant_id"], "version": 1,
        "payload": merchant})
    record("warmup", "1.5 same version re-push → 409 stale_version",
           status == 409 and body and body.get("reason") == "stale_version",
           f"HTTP {status} {body}")
    record("warmup", "1.5 409 body reports current_version",
           (body or {}).get("current_version") == 1)

    bumped = json.loads(json.dumps(merchant))
    bumped["performance"]["views"] = 2580
    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "merchant", "context_id": merchant["merchant_id"], "version": 2,
        "payload": bumped})
    record("warmup", "1.6 version bump → accepted", status == 200 and body.get("accepted") is True)

    for slug, payload in pack["categories"].items():
        if slug != "dentists":
            bot.call("POST", "/v1/context", {"scope": "category", "context_id": slug,
                                             "version": 1, "payload": payload})
    for mid, payload in pack["merchants"].items():
        if mid != merchant["merchant_id"]:
            bot.call("POST", "/v1/context", {"scope": "merchant", "context_id": mid,
                                             "version": 1, "payload": payload})
    for cid, payload in pack["customers"].items():
        bot.call("POST", "/v1/context", {"scope": "customer", "context_id": cid,
                                         "version": 1, "payload": payload})
    body, _, _, _ = bot.call("GET", "/v1/healthz")
    counts = body.get("contexts_loaded", {})
    record("warmup", "1.7 healthz reflects everything pushed",
           counts.get("category") == 5 and counts.get("merchant") == 10
           and counts.get("customer") == 15, str(counts))

    # ══ 2. Test-window examples 2.1-2.9 ═══════════════════════════════════
    head("api-call-examples.md §2 — Test window")

    trigger = pack["triggers"]["trg_001_research_digest_dentists"]
    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "trigger", "context_id": trigger["id"], "version": 1, "payload": trigger,
        "delivered_at": ISO(NOW)})
    record("tick", "2.1 trigger push → accepted", status == 200 and body.get("accepted") is True)

    body, status, ms, _ = bot.call("POST", "/v1/tick", {
        "now": ISO(NOW + timedelta(minutes=35)), "available_triggers": [trigger["id"]]})
    actions = (body or {}).get("actions", [])
    record("tick", "2.2 tick returns an action", len(actions) == 1, f"{ms*1000:.0f}ms")
    action = actions[0] if actions else {}
    missing = [f for f in REQUIRED_ACTION_FIELDS if f not in action]
    record("tick", "2.2 / F.2 all required action fields", not missing, f"missing={missing}")
    record("tick", "2.2 trigger_id echoes the trigger", action.get("trigger_id") == trigger["id"])
    record("tick", "2.2 merchant_id echoes the merchant",
           action.get("merchant_id") == merchant["merchant_id"])
    record("tick", "2.2 suppression_key echoes the trigger's",
           action.get("suppression_key") == trigger["suppression_key"],
           str(action.get("suppression_key")))
    record("tick", "2.2 send_as == 'vera' (merchant-facing)", action.get("send_as") == "vera")
    record("tick", "2.2 cta is a documented value",
           action.get("cta") in ("open_ended", "binary_yes_no", "binary_confirm_cancel",
                                 "multi_choice_slot", "none"), str(action.get("cta")))
    record("tick", "2.2 template_params is a non-empty list",
           isinstance(action.get("template_params"), list) and action["template_params"])
    record("tick", "F.4 no URL in body",
           "http" not in action.get("body", "").lower() and "www." not in action.get("body", ""))
    record("tick", "F.2 body non-empty", bool(action.get("body", "").strip()),
           f"{len(action.get('body',''))} chars")

    body, status, _, _ = bot.call("POST", "/v1/tick", {
        "now": ISO(NOW + timedelta(minutes=40)), "available_triggers": ["trg_does_not_exist"]})
    record("tick", "2.3 unknown trigger → {'actions': []}", body == {"actions": []}, str(body))

    conversation_id = action.get("conversation_id", "conv_001")
    body, status, ms, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": conversation_id, "merchant_id": merchant["merchant_id"],
        "customer_id": None, "from_role": "merchant",
        "message": "Yes please send the abstract. Also draft the patient WhatsApp.",
        "received_at": ISO(NOW + timedelta(minutes=42)), "turn_number": 2})
    record("reply", "2.4 engaged merchant → send",
           body and body.get("action") == "send" and bool(body.get("body")),
           f"{ms*1000:.0f}ms · {str((body or {}).get('body'))[:48]}")

    body, _, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "conv_autoreply_probe", "merchant_id": merchant["merchant_id"],
        "from_role": "merchant",
        "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.",
        "received_at": ISO(NOW), "turn_number": 2})
    record("reply", "2.5 auto-reply detected (send-once or wait)",
           body and body.get("action") in ("send", "wait"),
           f"{body.get('action')} · {body.get('rationale','')[:52]}")

    body, _, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "conv_hardno", "merchant_id": "m_002_bharat_dentist_mumbai",
        "from_role": "merchant", "message": "Not interested. Stop messaging me.",
        "received_at": ISO(NOW), "turn_number": 2})
    record("reply", "2.6 hard no → end", body and body.get("action") == "end",
           str((body or {}).get("rationale", ""))[:56])
    record("reply", "2.6 end response shape is {action, rationale}",
           set(body or {}) == {"action", "rationale"}, str(set(body or {})))

    body, _, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "conv_curveball", "merchant_id": "m_003_studio11_salon_hyderabad",
        "from_role": "merchant", "message": "Btw can you also help me with my GST filing this month?",
        "received_at": ISO(NOW), "turn_number": 2})
    text = (body or {}).get("body", "").lower()
    record("reply", "2.7 curveball → send, declines scope",
           body and body.get("action") == "send" and ("ca" in text or "outside" in text),
           str((body or {}).get("body", ""))[:70])
    record("reply", "2.7 curveball reply redirects on-mission",
           any(w in text for w in ("back to", "shall i", "want me")),
           "redirect present")

    injected = json.loads(json.dumps(category))
    injected["digest"] = [{
        "id": "d_injected_probe", "kind": "compliance",
        "title": "DCI revised radiograph dose limits effective 2026-12-15",
        "source": "DCI circular 2026-11-04",
        "summary": "Max dose drops 1.5 to 1.0 mSv per IOPA. E-speed film passes; D-speed does not.",
    }] + injected["digest"]
    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 2, "payload": injected})
    record("adapt", "2.8 mid-test category v2 accepted",
           status == 200 and body.get("accepted") is True)

    cust = pack["customers"]["c_001_priya_for_m001"]
    ctrg = pack["triggers"]["trg_003_recall_due_priya"]
    bot.call("POST", "/v1/context", {"scope": "customer", "context_id": cust["customer_id"],
                                     "version": 2, "payload": cust})
    bot.call("POST", "/v1/context", {"scope": "trigger", "context_id": ctrg["id"],
                                     "version": 1, "payload": ctrg})
    body, _, _, _ = bot.call("POST", "/v1/tick", {
        "now": ISO(NOW + timedelta(minutes=60)), "available_triggers": [ctrg["id"]]})
    cactions = (body or {}).get("actions", [])
    caction = cactions[0] if cactions else {}
    record("customer", "2.9 customer-scoped trigger produces an action", bool(cactions))
    record("customer", "2.9 send_as == merchant_on_behalf",
           caction.get("send_as") == "merchant_on_behalf", str(caction.get("send_as")))
    record("customer", "2.9 customer_id populated",
           caction.get("customer_id") == cust["customer_id"], str(caction.get("customer_id")))
    record("customer", "2.9 honours hi-en language preference",
           any(w in caction.get("body", "") for w in ("ya", "hai", "aapke", "jaisa", "khaali")),
           str(caction.get("body", ""))[:70])
    record("customer", "2.9 names real slots from the trigger payload",
           "5 Nov" in caction.get("body", "") or "6 Nov" in caction.get("body", ""))

    # ══ 4. Replay scenarios ═══════════════════════════════════════════════
    head("api-call-examples.md §4 — Replay scenarios")

    ladder = []
    for turn in range(2, 6):
        body, _, _, _ = bot.call("POST", "/v1/reply", {
            "conversation_id": f"conv_replay_auto_{turn}",
            "merchant_id": "m_004_glamour_salon_pune", "from_role": "merchant",
            "message": "Thank you for contacting us! Our team will respond shortly.",
            "received_at": ISO(NOW), "turn_number": turn})
        ladder.append((body or {}).get("action"))
    record("replay", "4.1 auto-reply ladder send→wait→end", ladder[:3] == ["send", "wait", "end"],
           " → ".join(str(a) for a in ladder))
    record("replay", "4.1 ladder keyed on merchant, not conversation_id",
           ladder[:3] == ["send", "wait", "end"], "four distinct conversation_ids used")

    body, _, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "conv_replay_intent", "merchant_id": "m_005_pizzajunction_restaurant_delhi",
        "from_role": "merchant", "message": "Ok, let's do it. What's next?",
        "received_at": ISO(NOW), "turn_number": 3})
    text = (body or {}).get("body", "").lower()
    actioning = [w for w in ("done", "sending", "draft", "here", "confirm", "proceed", "next")
                 if w in text]
    qualifying = [w for w in ("would you", "do you", "can you tell", "what if", "how about")
                  if w in text]
    record("replay", "4.2 intent transition → action mode",
           body.get("action") == "send" and actioning and not qualifying,
           f"actioning={actioning} qualifying={qualifying}")

    body, _, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "conv_replay_hostile", "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
        "from_role": "merchant", "message": "Why are you bothering me. This is useless. Stop sending these.",
        "received_at": ISO(NOW), "turn_number": 2})
    text = (body or {}).get("body", "").lower()
    record("replay", "4.3 hostile → end, or apology + exit",
           body.get("action") == "end" or any(w in text for w in ("sorry", "apolog", "won't")),
           f"{body.get('action')} · {body.get('rationale','')[:50]}")

    # ══ Stated limits ═════════════════════════════════════════════════════
    head("Challenge site — stated technical constraints")

    big = json.loads(json.dumps(pack["categories"]["restaurants"]))
    big["_padding"] = "x" * 400_000
    size_kb = len(json.dumps(big)) / 1024
    body, status, ms, err = bot.call("POST", "/v1/context", {
        "scope": "category", "context_id": "restaurants", "version": 9,
        "payload": big}, timeout=35)
    record("limits", f"500 KB payload cap ({size_kb:.0f} KB accepted)",
           status == 200 and body and body.get("accepted") is True,
           f"{ms*1000:.0f}ms {err or ''}")
    bot.call("POST", "/v1/context", {"scope": "category", "context_id": "restaurants",
                                     "version": 10, "payload": pack["categories"]["restaurants"]})

    def hit(i):
        client = Client(args.url)
        return client.call("GET", "/v1/healthz", timeout=30, fresh=True)

    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(hit, range(20)))
    burst = time.time() - start
    good = sum(1 for b, s, _, _ in outcomes if s == 200)
    record("limits", "10 req/s burst — 20 concurrent healthz", good == 20,
           f"{good}/20 ok in {burst:.1f}s")

    for tid, payload in pack["triggers"].items():
        bot.call("POST", "/v1/context", {"scope": "trigger", "context_id": tid,
                                         "version": 1, "payload": payload})
    body, _, ms, _ = bot.call("POST", "/v1/tick", {
        "now": ISO(NOW + timedelta(minutes=90)),
        "available_triggers": list(pack["triggers"])})
    n = len((body or {}).get("actions", []))
    record("limits", "20 actions/tick cap respected", n <= 20, f"{n} actions in {ms*1000:.0f}ms")
    record("limits", "30 s response timeout — tick well inside", ms < 10,
           f"{ms:.2f}s vs 30s ceiling")

    # ══ Robustness ════════════════════════════════════════════════════════
    head("Robustness — malformed and unexpected input")

    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "nonsense", "context_id": "x", "version": 1, "payload": {}})
    record("robust", "invalid scope → 400 invalid_scope",
           status == 400 and body.get("reason") == "invalid_scope", f"HTTP {status}")

    body, status, _, _ = bot.call("POST", "/v1/context", {"scope": "merchant"})
    record("robust", "missing required field → 4xx, not 500", 400 <= status < 500, f"HTTP {status}")

    body, status, _, _ = bot.call("POST", "/v1/tick", {})
    record("robust", "tick with empty body → 200 + actions list",
           status == 200 and isinstance((body or {}).get("actions"), list), f"HTTP {status}")

    body, status, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "never_opened_by_bot", "merchant_id": "m_999_unknown",
        "from_role": "merchant", "message": "hello?", "turn_number": 2})
    record("robust", "reply on unknown conversation + unknown merchant",
           status == 200 and (body or {}).get("action") in ("send", "wait", "end"),
           str((body or {}).get("action")))

    body, status, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "c_from_customer", "merchant_id": merchant["merchant_id"],
        "customer_id": "c_001_priya_for_m001", "from_role": "customer",
        "message": "Wed works for me", "turn_number": 2})
    record("robust", "from_role='customer' handled",
           status == 200 and (body or {}).get("action") in ("send", "wait", "end"),
           str((body or {}).get("action")))

    body, status, _, _ = bot.call("POST", "/v1/reply", {
        "conversation_id": "c_empty", "merchant_id": merchant["merchant_id"],
        "from_role": "merchant", "message": "", "turn_number": 2})
    record("robust", "empty message → valid action, no crash",
           status == 200 and (body or {}).get("action") in ("send", "wait", "end"),
           str((body or {}).get("action")))

    body, status, _, _ = bot.call("POST", "/v1/context", {
        "scope": "merchant", "context_id": "m_unicode", "version": 1,
        "payload": {"merchant_id": "m_unicode", "category_slug": "dentists",
                    "identity": {"name": "श्री डेंटल ₹ Clinic", "city": "Delhi",
                                 "locality": "Karol Bagh", "languages": ["hi", "en"],
                                 "owner_first_name": "Aarav"},
                    "performance": {"window_days": 30, "views": 1000, "calls": 20,
                                    "ctr": 0.02}}})
    record("robust", "unicode + ₹ in payload accepted", status == 200 and body.get("accepted"))

    # ══ Determinism ═══════════════════════════════════════════════════════
    head("Determinism — the site's headline requirement")

    bot.call("POST", "/v1/teardown", {})
    bodies = []
    for _ in range(2):
        bot.call("POST", "/v1/teardown", {})
        bot.call("POST", "/v1/context", {"scope": "category", "context_id": "dentists",
                                         "version": 1, "payload": category})
        bot.call("POST", "/v1/context", {"scope": "merchant",
                                         "context_id": merchant["merchant_id"],
                                         "version": 1, "payload": merchant})
        bot.call("POST", "/v1/context", {"scope": "trigger", "context_id": trigger["id"],
                                         "version": 1, "payload": trigger})
        out, _, _, _ = bot.call("POST", "/v1/tick", {
            "now": ISO(NOW + timedelta(minutes=35)), "available_triggers": [trigger["id"]]})
        bodies.append((out or {}).get("actions", [{}])[0].get("body", ""))
    record("determinism", "same contexts + same now → identical body",
           bodies[0] == bodies[1] and bool(bodies[0]),
           "identical" if bodies[0] == bodies[1] else "DIFFERENT")

    # ══ Summary ═══════════════════════════════════════════════════════════
    head("SUMMARY")
    fails = [r for r in RESULTS if r[2] == "FAIL"]
    warns = [r for r in RESULTS if r[2] == "WARN"]
    passes = [r for r in RESULTS if r[2] == "PASS"]
    print(f"  {G}{len(passes)} passed{X} · {Y}{len(warns)} warn{X} · {R}{len(fails)} failed{X}"
          f"   ({len(RESULTS)} checks)")
    for section, ref, verdict, detail in fails:
        print(f"  {R}FAIL{X} [{section}] {ref} — {detail}")
    for section, ref, verdict, detail in warns:
        print(f"  {Y}WARN{X} [{section}] {ref} — {detail}")

    print(f"\n{DIM}  latency by endpoint{X}")
    for path, values in sorted(bot.latency.items()):
        print(f"    {path:<16} n={len(values):<4} avg {sum(values)/len(values)*1000:6.0f}ms  "
              f"max {max(values)*1000:6.0f}ms")

    bot.call("POST", "/v1/teardown", {})
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
