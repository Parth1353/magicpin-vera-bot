# Pre-submission audit

Run against the live deployment on 2026-08-21, before submitting.
Every requirement traced to the line in the pack (or on the challenge site) that states it.

**Bot:** `https://magicpin-vera-bot-9ckg.onrender.com` · deployed commit `a53cf8f`

Reproduce:

```bash
python -m pytest tests/ -q
python tools/verify_submission.py --url https://magicpin-vera-bot-9ckg.onrender.com
python tools/harness_sim.py      --url https://magicpin-vera-bot-9ckg.onrender.com
LLM_API_KEY=sk-... python tools/run_judge.py --url https://magicpin-vera-bot-9ckg.onrender.com
```

## Totals

| Suite | Result |
|---|---|
| `tools/verify_submission.py` — every `api-call-examples.md` example, verbatim, on the live URL | **58 / 58** |
| `tools/harness_sim.py` — full judge lifecycle incl. mid-test injection | **17 / 17** |
| Official `judge_simulator.py` (`all`) against the live URL | warmup · auto_reply · intent · hostile — **all PASS** |
| `pytest` | **126 passed** |

## Endpoint contract — `challenge-testing-brief.md` §2, `api-call-examples.md` §1-2

| Requirement | Source | Status |
|---|---|---|
| `GET /v1/healthz` → `status`, `uptime_seconds`, `contexts_loaded` (4 scopes) | 2.4 / 1.1 | pass |
| `GET /v1/metadata` → 7 identity fields | 2.5 / 1.2 | pass |
| `POST /v1/context` → `accepted`, `ack_id`, `stored_at` | 2.1 / 1.3 | pass |
| Idempotent on `(context_id, version)`; equal-or-lower version → **409 `stale_version` + `current_version`** | 2.1 / 1.5 | pass |
| Higher version replaces atomically | 2.1 / 1.6 | pass |
| Invalid scope → **400 `invalid_scope`** | 2.1 | pass |
| `POST /v1/tick` → `actions[]`, may be empty | 2.2 / 2.3 | pass |
| Action carries all 11 fields (missing = −2) | F.2 | pass |
| `conversation_id` unique per tick action | 2.2 | pass |
| `POST /v1/reply` → exactly `send` \| `wait` \| `end` | 2.3 / 2.4-2.6 | pass |
| `end` response is `{action, rationale}` only | 2.6 | pass |
| Customer-scoped trigger → `send_as: merchant_on_behalf`, `customer_id` set | 2.9 | pass |
| `POST /v1/teardown` wipes state | §11 | pass |

## Stated limits — challenge site, "technical constraints"

| Limit | Measured |
|---|---|
| 30 s response timeout | tick **0.37 s**, reply **0.16 s** |
| 10 requests/sec from judge | 20 concurrent healthz, **20/20 OK in 1.4 s** |
| 500 KB context payload cap | **397 KB accepted in 658 ms** |
| 20 actions per tick | cap enforced; 25 triggers → 7 actions |

## Failure modes — `api-call-examples.md` F.1-F.5

| Mode | Status |
|---|---|
| F.2 malformed / missing action fields | none across 94 actions |
| F.2 empty body | none |
| F.4 URL in body (−3 each) | zero URLs ever emitted — no URL exists in any context, so one would be fabricated |
| F.5 repeated body in a conversation (−2 each) | none |
| Unhandled exception → 500 | impossible: middleware returns a schema-valid response on every path |

## Judged behaviour — `challenge-brief.md` §8, §11-12; `case-studies.md`

| Requirement | Status |
|---|---|
| Deterministic for the same inputs | identical body across repeated runs — verified live and in `compose()` |
| Adaptive injection: new digest items used, not ignored | injected "27%" fact appears in post-injection messages |
| Adaptive injection: shifted performance cited | "moved from X to Y" appears |
| Adaptive injection: no hallucination | a citation is dropped unless the message still carries that source's figure or phrasing |
| §11 buried CTA | 0/100 — the ask is the last sentence (a trailing source line after it matches the brief's own gold example) |
| §11 multiple CTAs | 0/100 — exactly one ask per message, enforced in the renderer |
| §11 promotional tone in clinical categories | 0/100 |
| §11 "Flat N% off" where service+price exists | 0/100 — the offer picker never selects a discount when a service-at-price is available |
| §11 long preamble | 0/100 |
| §11 hallucinated data | 0/100 — every number traces to a context path via `NumberLedger` |
| case-studies #3 owner/business named | 100/100 |
| case-studies #8 decodable `conversation_id` | 100/100 |
| case-studies #9 rationale matches the message | 100/100 — no rationale claims a citation, language or trigger the body does not carry |
| case-studies #11 near-duplicate of a published case study | 0/100 |
| §12.1 auto-reply detection | send → wait 24h → end, keyed on the **merchant** (harness uses 4 different conversation ids) |
| §12.2 intent transition | switches to action mode, zero qualifying markers |
| §12.4 per-turn language detection | Hindi/Hinglish reply gets a Hindi-register response |
| §12.5 knowing when to stop | opt-out, hostility, repeated objection and three unclear turns all wind down |

## Deliverables — `challenge-brief.md` §7

| Artifact | Status |
|---|---|
| §7.1 `bot.py` — `compose(category, merchant, trigger, customer)` → the five keys | present; also exposes `app` for `uvicorn bot:app` |
| §7.2 `submission.jsonl` — 30 rows, T01-T30 | present |
| §7.3 `README.md` — one page | 628 words; long version in `DESIGN.md` |
| §7.4 `conversation_handlers.py` — `respond(state, merchant_message)` (optional tiebreaker) | present |

## Known limitations, stated plainly

1. **Free Render instance.** Idles out after ~15 min; `.github/workflows/keep-warm.yml` pings
   `/v1/healthz` every 10 minutes to prevent it. Upgrading to Starter for the judging window
   removes the risk entirely.
2. **Auto-deploy is not wired.** Render's GitHub App has no webhook on this repo, so pushes
   do **not** redeploy. Deploys are manual. This is protective during judging — a stray push
   cannot restart the process and wipe in-memory context — but a real fix would need a manual
   deploy from the Render dashboard.
3. **Single instance by design.** Context lives in the process for the length of a test window,
   so the service must not be scaled horizontally.
4. **The 35.5/50 in `eval_report.md` is a self-built proxy rubric**, not the judge. It catches
   missing citations, missing names, double asks and ungrounded figures; it cannot score prose.
