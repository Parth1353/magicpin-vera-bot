# Vera — merchant message engine

**magicpin AI Challenge submission** · Parth Saini · parthsaini13@gmail.com

**Bot URL: `https://magicpin-vera-bot-9ckg.onrender.com`**
`POST /v1/context` · `POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` · `GET /v1/metadata`

## Approach

`compose(category, merchant, trigger, customer?)` is **deterministic and grounded by
construction**. No model runs in the request path; an LLM editor is available behind a flag
but may only change wording, never facts.

That came from reading the harness. `/v1/tick` can be asked for five compositions inside a
10s budget, so a per-message model call is the critical path — and the two things the rubric
punishes hardest, fabrication and instability, are exactly what a compiled path makes free.
Composition runs in ~5ms and always returns the same message for the same inputs.

Five stages: **FactSheet** turns the four contexts into typed facts, registering every
number with the context path it came from → **Insights** derives judgment (conversion against
the category median, the rupee value of the dormant list, seasonal alignment, digest
ranking, contrarian reads), showing its arithmetic so a derived number stays checkable →
**Composer** dispatches by `trigger.kind` into 35 merchant-facing and 13 customer-facing
composers → **Guard** blocks URLs, category taboos, internal jargon, unrendered
placeholders, repeats, unearned citations, and *any number the ledger cannot account for* →
**Planner** handles suppression keys, opt-outs, one thread per merchant per tick, and a
deferral queue so a held-back trigger returns rather than being lost.

## What I built for specifically

- **Sparse context is the real test.** 75 of 100 expanded triggers carry
  `payload: {"placeholder": true}`; 40 of 50 merchants have no offers, signals or history.
  A specificity floor tops thin messages up from the merchant's own 30-day numbers against
  the category benchmark — customer-facing messages top up from the customer's visit history
  instead, because listing analytics mean nothing to a patient.
- **Kind/category mismatches are in the data.** `chronic_refill_due` lands on a dentist,
  `recall_due` on a yoga studio. Those get translated, not sent as pharmacy copy.
- **Restraint is a decision.** Diwali is 188 days out, so the festival trigger says so and
  redirects to the window that is actually open. An expected April–June gym dip argues
  against spending.
- **Auto-reply detection is keyed on the merchant, not the conversation** — the harness
  sends four identical canned replies under four different `conversation_id`s.

## Tradeoffs

Rules over generation costs me the surprising turn of phrase; it buys zero fabrication, zero
timeouts, and a message I can explain line by line. Variation comes from a deterministic
hash per merchant, so it does not read like one template stamped fifty times. State is
in-memory, so the service runs as a single instance. No URLs are ever emitted: none exist in
the contexts, so any link would be invented.

## What extra context would have helped most

1. **Response history by message family** — which *kinds* of nudge this merchant answers.
   That turns trigger ranking from a heuristic into a learned prior.
2. **A slot/availability feed** — three trigger kinds offer to hold a slot without being
   able to name one.
3. **A service menu separate from the promotions list**, so a merchant with no live offer
   does not force a fall back to a category-typical price.

## Verify it

```bash
pip install -r requirements.txt && python -m pytest tests/ -q          # 126 tests
python tools/verify_submission.py --url https://magicpin-vera-bot-9ckg.onrender.com
python tools/harness_sim.py     --url https://magicpin-vera-bot-9ckg.onrender.com
LLM_API_KEY=sk-... python tools/run_judge.py --url https://magicpin-vera-bot-9ckg.onrender.com
```

`verify_submission.py` replays every example in `examples/api-call-examples.md` verbatim
(58 checks, all passing). `harness_sim.py` runs the full judge lifecycle including mid-test
context injection. Deeper reasoning is in **[DESIGN.md](DESIGN.md)**; every constraint I extracted from the
pack, with its source, is in **[SPEC-NOTES.md](SPEC-NOTES.md)**; the pre-submission
requirement-by-requirement audit is in **[AUDIT.md](AUDIT.md)**.

`bot.py` exposes `compose(...)` per brief §7.1 and runs as `uvicorn bot:app`.
`conversation_handlers.py` exposes `respond(state, merchant_message)` per §7.4.
`submission.jsonl` holds the 30 canonical pairs.
