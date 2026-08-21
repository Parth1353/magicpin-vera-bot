#!/usr/bin/env python3
"""Run every canonical test pair through the composer, score it, and write submission.jsonl.

The scoring here is a proxy, not the judge. It exists to catch the things a rubric-driven
LLM will certainly notice — no citation on a research claim, no owner name, a number with
no provenance, two asks, a URL, a message that reads like the published case studies — so
those never reach the real harness. Quality of prose is still a human call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera_bot import guard                                    # noqa: E402
from vera_bot.composer import compose_from_sheet, conversation_id_for   # noqa: E402
from vera_bot.facts import build_fact_sheet                   # noqa: E402
from vera_bot.insights import derive                          # noqa: E402
from vera_bot.utils import squeeze                            # noqa: E402

DEFAULT_DATA = ROOT.parent / "expanded"
SEED_DATA = ROOT.parent / "dataset"
NOW = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)

_NUM = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")


# --------------------------------------------------------------------------- #

def load(expanded: Path, seeds: Path) -> dict:
    data = {"categories": {}, "merchants": {}, "customers": {}, "triggers": {}, "pairs": []}

    for path in (seeds / "categories").glob("*.json"):
        payload = json.loads(path.read_text())
        data["categories"][payload.get("slug", path.stem)] = payload

    if expanded.exists():
        for kind, key in (("merchants", "merchant_id"), ("customers", "customer_id"),
                          ("triggers", "id")):
            folder = expanded / kind
            if folder.exists():
                for path in folder.glob("*.json"):
                    payload = json.loads(path.read_text())
                    data[kind][payload[key]] = payload
        pairs_path = expanded / "test_pairs.json"
        if pairs_path.exists():
            data["pairs"] = json.loads(pairs_path.read_text())["pairs"]

    # fall back to seeds for anything the expansion did not produce
    for name, kind, key in (("merchants_seed.json", "merchants", "merchant_id"),
                            ("customers_seed.json", "customers", "customer_id"),
                            ("triggers_seed.json", "triggers", "id")):
        path = seeds / name
        if path.exists():
            for item in json.loads(path.read_text())[kind]:
                data[kind].setdefault(item[key], item)
    return data


def category_for(data: dict, merchant: dict) -> dict:
    slug = merchant.get("category_slug")
    if slug in data["categories"]:
        return data["categories"][slug]
    mid = (merchant.get("merchant_id") or "").lower()
    for candidate, payload in data["categories"].items():
        if candidate.rstrip("s") in mid:
            return payload
    return {}


# --------------------------------------------------------------------------- #
# proxy rubric
# --------------------------------------------------------------------------- #

def score(body: str, sheet, ins, composition) -> dict:
    marks: dict[str, float] = {}
    flags: list[str] = []
    lowered = body.lower()

    # --- specificity -------------------------------------------------------
    numbers = set(_NUM.findall(body))
    spec = min(6.0, len(numbers) * 1.6)
    if guard.has_citation(body):
        spec += 2.0
    elif guard.needs_citation(body):
        spec -= 2.0
        flags.append("claim without a source")
    if re.search(r"₹\s?[\d,]+", body):
        spec += 1.0
    if re.search(r"\b\d+%|\b\d+ (?:days?|weeks?|months?)\b", body):
        spec += 1.0
    if re.search(r"\b(?:some|many|several|a lot|lots|significant|various)\b", lowered):
        spec -= 1.5
        flags.append("vague quantifier")
    marks["specificity"] = _clamp(spec)

    # --- category fit ------------------------------------------------------
    # Mirrors the judge's own criteria: dentists clinical + "Dr.", salons warm, restaurants
    # operator-to-operator, gyms coaching, pharmacies precise. Literal vocabulary matches
    # are worth something but register is worth more.
    fit = 4.5
    vocab_hits = [v for v in sheet.vocab if v.lower() in lowered]
    fit += min(1.5, len(vocab_hits) * 0.75)
    register = {
        "dentists": r"\b(recall|clinical|chart|patients?|caries|fluoride|scaling|dose|iopa)\b",
        "salons": r"\b(chair|bookings?|stylist|salon|clients?|spa|colour|color|look)\b",
        "restaurants": r"\b(covers?|footfall|kitchen|delivery|menu|table|orders?|guests?)\b",
        "gyms": r"\b(members?|coach|class(es)?|trial|session|floor|retention|training)\b",
        "pharmacies": r"\b(counter|batch|molecule|prescription|refill|stock|pack|dose)\b",
    }.get(sheet.category_slug)
    if register and re.search(register, lowered):
        fit += 2.0
    if guard.find_taboos(body, sheet.taboos):
        fit -= 5.0
        flags.append("category taboo used")
    if re.search(r"!!+|AMAZING|HUGE DEAL|BEST EVER", body):
        fit -= 2.0
        flags.append("promotional shouting")
    if sheet.category_slug == "dentists":
        if "dr." in lowered:
            fit += 1.5
        else:
            fit -= 1.0
            flags.append("dentist addressed without Dr.")
    if composition.send_as == "merchant_on_behalf" and re.search(
            r"[🦷✨💪🍽️💊]|no pressure|no judgement|no judgment|whenever suits", body):
        fit += 1.0
    if re.search(r"\bflat \d+% off\b", lowered):
        fit -= 1.5
        flags.append("discount framing where service-at-price exists")
    marks["category_fit"] = _clamp(fit)

    # --- merchant fit ------------------------------------------------------
    merchant_fit = 3.5
    names = [n for n in (sheet.salutation, sheet.business_name, sheet.owner_name) if n]
    if any(n.lower() in lowered for n in names):
        merchant_fit += 2.5
    else:
        flags.append("owner/business name missing")
    if sheet.locality and sheet.locality.lower() in lowered:
        merchant_fit += 1.0
    if sheet.business_name and sheet.business_name.lower() in lowered:
        merchant_fit += 0.5
    own_numbers = {str(sheet.views), str(sheet.calls), str(sheet.directions)}
    if any(n and n in body.replace(",", "") for n in own_numbers if n != "None"):
        merchant_fit += 1.5
    for offer in sheet.active_offers:
        if offer.title.lower() in lowered:
            merchant_fit += 1.0
            break
    if "hi" in sheet.languages and composition.language != "English":
        merchant_fit += 0.5
    marks["merchant_fit"] = _clamp(merchant_fit)

    # --- decision quality --------------------------------------------------
    decision = 4.0
    trigger = sheet.trigger
    if trigger:
        payload_terms = [squeeze(str(v)) for v in (trigger.payload or {}).values()
                         if isinstance(v, (str, int, float)) and len(str(v)) > 2]
        if any(str(t).lower() in lowered for t in payload_terms):
            decision += 2.0
        if trigger.digest_item and trigger.digest_item.source.split(",")[0].lower() in lowered:
            decision += 1.5
    if ins.contrarian and any(w in lowered for w in ("rather than", "not the", "before you",
                                                     "instead", "sit out", "hold the")):
        decision += 2.0
    if ins.conversion_gap_actions and str(ins.conversion_gap_actions) in body:
        decision += 1.0
    if composition.angle_id and composition.angle_id != "fallback":
        decision += 0.5
    # naming the road not taken is the clearest evidence of an actual decision
    if re.search(r"\b(rather than|not the|before you|instead of|this is not a|"
                 r"i'?d rather|sit out|hold the|the losing move|not chasing)\b", lowered):
        decision += 1.5
    if ins.movement and "moved from" in lowered:
        decision += 1.0
    if re.search(r"\b(improve your profile|increase your sales|grow your business)\b", lowered):
        decision -= 2.5
        flags.append("generic growth framing")
    marks["decision_quality"] = _clamp(decision)

    # --- engagement --------------------------------------------------------
    engagement = 3.5
    asks = guard.count_asks(body)
    if asks == 1:
        engagement += 2.5
    elif asks == 0:
        engagement -= 1.5
        flags.append("no explicit next step")
    else:
        engagement -= 1.0
        flags.append(f"{asks} separate asks")
    engagement += min(2.0, len(composition.levers) * 0.7)
    if len(body) <= 480:
        engagement += 1.0
    if len(body) > 620:
        engagement -= 1.5
        flags.append("too long for WhatsApp")
    if re.search(r"\bI (?:can|will|'ll) (?:draft|put|have|write|take)\b", body):
        engagement += 1.0
    marks["engagement_compulsion"] = _clamp(engagement)

    # --- hard penalties ----------------------------------------------------
    penalties = 0.0
    if re.search(r"https?://|www\.", body):
        penalties += 3.0
        flags.append("URL in body")
    jargon = guard.find_jargon(body, sheet.vocab)
    if jargon:
        penalties += 1.0
        flags.append(f"jargon: {jargon[:3]}")
    unknown = sheet.ledger.unknown_numbers(body)
    if unknown:
        penalties += 2.0
        flags.append(f"ungrounded numbers: {unknown[:3]}")
    similarity = guard.plagiarism_score(body, guard.context_vocabulary(sheet))
    if similarity >= guard.PLAGIARISM_THRESHOLD:
        penalties += 3.0
        flags.append(f"case-study similarity {similarity:.2f}")

    total = max(0.0, sum(marks.values()) - penalties)
    return {"marks": marks, "penalties": penalties, "total": total, "flags": flags,
            "similarity": round(similarity, 2), "length": len(body), "asks": asks}


def _clamp(value: float) -> float:
    return round(max(0.0, min(10.0, value)), 1)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expanded", default=str(DEFAULT_DATA))
    parser.add_argument("--seeds", default=str(SEED_DATA))
    parser.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    parser.add_argument("--report", default=str(ROOT / "eval_report.md"))
    parser.add_argument("--all-triggers", action="store_true",
                        help="score every trigger, not just the 30 canonical pairs")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    data = load(Path(args.expanded), Path(args.seeds))
    if not data["pairs"]:
        print("No test_pairs.json found — run dataset/generate_dataset.py first.")
        return 1

    pairs = data["pairs"]
    if args.all_triggers:
        pairs = [{"test_id": f"X{i:03d}", "trigger_id": tid,
                  "merchant_id": t.get("merchant_id"), "customer_id": t.get("customer_id")}
                 for i, (tid, t) in enumerate(sorted(data["triggers"].items()), 1)]

    rows, scored, failures = [], [], []
    for pair in pairs:
        merchant = data["merchants"].get(pair["merchant_id"])
        trigger = data["triggers"].get(pair["trigger_id"])
        if not merchant or not trigger:
            failures.append((pair["test_id"], "missing merchant or trigger"))
            continue
        customer = data["customers"].get(pair.get("customer_id")) if pair.get("customer_id") else None
        category = category_for(data, merchant)

        sheet = build_fact_sheet(category, merchant, trigger, customer, now=NOW)
        ins = derive(sheet)
        composition = compose_from_sheet(sheet, ins)
        if composition is None:
            failures.append((pair["test_id"], "composer declined to send"))
            continue

        result = score(composition.body, sheet, ins, composition)
        scored.append((pair, composition, result, sheet))
        rows.append({
            "test_id": pair["test_id"],
            "merchant_id": pair["merchant_id"],
            "trigger_id": pair["trigger_id"],
            "customer_id": pair.get("customer_id"),
            "conversation_id": conversation_id_for(sheet),
            "send_as": composition.send_as,
            "template_name": composition.template_name,
            "template_params": composition.template_params,
            "body": composition.body,
            "cta": composition.cta,
            "suppression_key": composition.suppression_key,
            "rationale": composition.rationale,
        })

    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    totals = [r["total"] for _, _, r, _ in scored]
    dims = ["specificity", "category_fit", "merchant_fit", "decision_quality",
            "engagement_compulsion"]
    averages = {d: round(sum(r["marks"][d] for _, _, r, _ in scored) / max(1, len(scored)), 2)
                for d in dims}
    flag_counts = Counter(f.split(":")[0] for _, _, r, _ in scored for f in r["flags"])

    report = ["# Offline evaluation", "",
              f"- pairs scored: **{len(scored)}** of {len(pairs)}",
              f"- average proxy total: **{sum(totals) / max(1, len(totals)):.1f} / 50**",
              f"- lowest: {min(totals):.1f} · highest: {max(totals):.1f}",
              f"- average length: {sum(r['length'] for _, _, r, _ in scored) // max(1, len(scored))} chars",
              ""]
    report.append("| dimension | avg |")
    report.append("|---|---:|")
    for dim, value in averages.items():
        report.append(f"| {dim} | {value} |")
    report.append("")
    if flag_counts:
        report.append("## Flags")
        for flag, count in flag_counts.most_common():
            report.append(f"- {flag} — {count}")
        report.append("")
    if failures:
        report.append("## Not composed")
        for test_id, reason in failures:
            report.append(f"- {test_id}: {reason}")
        report.append("")

    report.append("## Messages")
    for pair, composition, result, sheet in sorted(scored, key=lambda x: x[2]["total"]):
        report.append(f"\n### {pair['test_id']} · {result['total']:.1f}/50 · "
                      f"{sheet.category_slug} · {sheet.trigger.kind if sheet.trigger else '?'}")
        report.append(f"*{sheet.business_name} — {sheet.place}* · "
                      f"`{composition.cta}` · `{composition.send_as}` · "
                      f"{result['length']} chars · {composition.language}")
        report.append("")
        report.append(f"> {composition.body}")
        report.append("")
        report.append("  " + " · ".join(f"{k[:4]} {v}" for k, v in result["marks"].items()))
        if result["flags"]:
            report.append("  " + " ".join(f"`{f}`" for f in result["flags"]))
    Path(args.report).write_text("\n".join(report) + "\n")

    if not args.quiet:
        print(f"scored {len(scored)}/{len(pairs)} pairs · "
              f"avg {sum(totals) / max(1, len(totals)):.1f}/50 · "
              f"min {min(totals):.1f} · max {max(totals):.1f}")
        for dim, value in averages.items():
            print(f"  {dim:24} {value:5.2f}")
        if flag_counts:
            print("  flags:", dict(flag_counts))
        print(f"wrote {args.out} and {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
