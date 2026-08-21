# Vera — merchant message engine

**Submission for the magicpin AI Challenge.** Parth Saini · parthsaini13@gmail.com

**Public base URL: `https://magicpin-vera-bot-9ckg.onrender.com`**

Endpoints: `POST /v1/context` · `POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` ·
`GET /v1/metadata` (plus `POST /v1/teardown`)

```bash
pip install -r requirements.txt && python run.py       # serves on :8080
python -m pytest tests/ -q                             # 111 tests
python tools/harness_sim.py --url https://magicpin-vera-bot-9ckg.onrender.com
LLM_API_KEY=sk-... python tools/run_judge.py --url https://magicpin-vera-bot-9ckg.onrender.com
```

Deployed on Render (Singapore, free tier, single instance — state is in-process).
`.github/workflows/keep-warm.yml` pings `/v1/healthz` every 10 minutes so the free instance
does not idle out mid-judging.

---

## Approach

The composer is **deterministic and grounded by construction**, with an optional LLM
editorial pass that is allowed to change wording and nothing else.

That is a deliberate choice, and it came from reading the harness rather than from a
preference for rules over models. `api-call-examples.md` budgets `/v1/tick` at 10s and
`judge_simulator.py` times out at 15s, while the full-evaluation path can ask for five
compositions in one tick. A per-message model call is the critical path there. More
importantly, the two things the rubric punishes hardest — fabricated data and unstable
output — are exactly the two things a generative path makes hard to guarantee and a
compiled path makes free.

The pipeline is five stages:

1. **FactSheet** (`facts.py`) — the four contexts become typed facts, each carrying the
   context path it came from. Every numeric literal the contexts mention is registered in a
   `NumberLedger` with its provenance.
2. **Insights** (`insights.py`) — derived judgment: conversion against the category median,
   the rupee value of the dormant list, seasonal alignment, digest ranking, and contrarian
   reads. Every derived number is arithmetic over two context numbers, registered alongside
   its inputs, and the message shows its working: *"1,200 views converting at 2.2% against a
   4.0% median — on that traffic it's the difference between 26 listing actions a month and 48."*
3. **Composer** (`composer/`) — dispatch by `trigger.kind` into 35 merchant-facing and 13
   customer-facing composers, each filling a `Plan` (why now / evidence / judgment / the work
   Vera will do / one ask). A renderer assembles it, places the language mix, and guarantees
   exactly one ask and a length a merchant will actually read.
4. **Guard** (`guard.py`) — the last gate. Blocks URLs, category taboos, internal jargon,
   unrendered placeholders, repeated bodies, and **any number the ledger cannot account for**.
   A source citation is dropped unless the message still carries a figure or a distinctive
   phrase from that source. Also runs a similarity check against the ten published case
   studies, so nothing reads as copied from them.
5. **Planner + conversation** (`planner.py`, `conversation.py`, `replyer/`) — suppression
   keys, opt-outs, one thread per merchant per tick, a deferral queue so a held-back trigger
   returns instead of being lost, and a deterministic intent classifier feeding per-intent
   reply handlers.

Result: composition is ~5ms, so a 5-action tick lands in ~30ms against a 10s budget, and the
same contexts always produce the same message.

## What I built for specifically

- **Sparse context is the real test.** 75 of the 100 expanded triggers carry
  `payload: {"placeholder": true}` and 40 of the 50 merchants have no offers, signals,
  history or review themes. Half the canonical pairs land there. Category context is always
  complete and every merchant has 30 days of performance, so a **specificity floor** tops any
  thin message up from those — customer-facing messages top up from the customer's own visit
  history instead, because listing analytics mean nothing to a patient.
- **Kind/category mismatches are built into the data.** `chronic_refill_due` is assigned to a
  dentist, `recall_due` to a yoga studio. Those get translated into the equivalent the
  business actually has rather than sending pharmacy copy to a dental clinic.
- **Restraint is a decision.** Diwali is 188 days out in the dataset, so the festival trigger
  produces a message that says so and redirects to the window that is actually open. An
  expected April–June gym dip argues against spending rather than for it.
- **Auto-reply detection is keyed on the merchant, not the conversation.** The harness sends
  four identical canned replies under four *different* `conversation_id`s; anything keyed on
  the thread sees four first-time auto-replies and never escalates. The ladder is
  send → wait 24h → end.
- **Contradictory records.** Generated customers can be `state: "new"` with five visits on
  file. The visit ledger wins, and the message only says what both support.

## Tradeoffs

- **Rules over generation for composition.** I give up the surprising turn of phrase a
  frontier model finds. I get zero fabrication, zero timeouts, reproducibility, and a message
  I can explain line by line. Variation comes from a deterministic hash per merchant, so the
  engine does not read like one template stamped fifty times.
- **The LLM is an editor, not an author.** Enabled with a key, it may rewrite for rhythm; the
  edit is rejected unless the number set is byte-identical and the guard still passes. So it
  can only make the message read better, never make it say more.
- **In-memory state.** Simplest thing that satisfies "persist until the test ends", at the
  cost of needing a single instance. The deploy configs pin that explicitly.
- **A hand-built proxy rubric** (`tools/offline_eval.py`) rather than paid judge runs. It
  catches what a rubric-driven judge certainly notices — missing citation, missing owner
  name, two asks, ungrounded figures — but it cannot score prose, so that stayed a manual read.

## What extra context would have helped most

1. **Merchant response history by message family.** `conversation_history` shows what was
   sent and whether they replied, but not which *kinds* of nudge this merchant answers.
   Knowing that Dr. Meera engages with clinical digests and ignores offer prompts would turn
   trigger ranking from a heuristic into a learned prior — the single biggest lift available.
2. **A slot/availability feed.** Customer-facing booking messages are the highest-intent
   sends and they are capped by not knowing what is actually free. Three trigger kinds
   currently offer to hold a slot without being able to name one.
3. **What the merchant sells, separately from what they discount.** `offers` is a promotions
   list; there is no service menu. For a merchant with no live offer I have to reach for the
   category catalogue, which is a category-typical price rather than theirs.
4. **Ground truth on CTR's denominator.** `ctr` is not `calls / views` in the dataset, so I
   phrase the peer gap in listing actions and show both totals. A defined metric would let
   the strongest number in the message be sharper still.

## Layout

```
vera_bot/        facts · insights · composer/ · guard · planner · conversation · replyer/ · app
tests/           111 tests: contract, invariants over all 100 triggers, replay scenarios
tools/           offline_eval.py (30 pairs → submission.jsonl) · harness_sim.py (full lifecycle)
deploy/          Render, Fly, Docker
SPEC-NOTES.md    every hard constraint extracted from the pack, with its source
```
