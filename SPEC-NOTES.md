# Hard constraints extracted from the challenge pack

Sources: `challenge-brief.md`, `challenge-testing-brief.md`, `examples/api-call-examples.md`,
`examples/case-studies.md`, `judge_simulator.py`, `dataset/`.

## Latency (the binding constraint)
| Endpoint | Brief says | api-call-examples budget | judge_simulator timeout |
|---|---|---|---|
| GET /v1/healthz  | — | 2 s  | 5 s |
| GET /v1/metadata | — | 2 s  | 5 s |
| POST /v1/context | — | 5 s  | 10 s |
| POST /v1/tick    | 30 s | 10 s | **15 s** |
| POST /v1/reply   | 30 s | 10 s | **15 s** |

=> Design for **<10 s worst case on tick/reply**. `_full` scenario batches 5 triggers per
tick, so up to 5 compositions must finish inside that budget. A synchronous per-action LLM
call cannot be the critical path. Deterministic core; LLM strictly optional + budgeted.

## Response-shape requirements
- `/v1/context` re-push of an equal-or-lower version -> **HTTP 409**
  `{accepted:false, reason:"stale_version", current_version:N}` (api-call-examples 1.5,
  testing-brief 2.1). Higher version replaces atomically.
- `/v1/tick` action REQUIRED keys, missing any = -2 penalty (api-call-examples F.2):
  `conversation_id, merchant_id, customer_id, send_as, trigger_id, template_name,
   template_params, body, cta, suppression_key, rationale`
- `/v1/reply` -> exactly one of `send` | `wait` (+`wait_seconds`) | `end`.
- Reusing a `conversation_id` in `/v1/tick` is invalid — new conversations only.
- Max 20 actions/tick. One action per (merchant_id, conversation_id) per tick.

## Scoring (judge_simulator LLMScorer + case-studies.md)
Five dimensions x 0-10: specificity, category_fit, merchant_fit, decision_quality
(a.k.a. trigger_relevance), engagement_compulsion.
Explicit judge penalties: **fabricating data not in context -2**, **exposing internal
jargon to the merchant -1**.
The scorer prompt hands the judge: category slug + `voice.tone` + `voice.vocab_taboo`,
merchant name/`owner_first_name`/locality/languages, views/calls/ctr, `signals`,
active offer titles, trigger kind + FULL payload JSON + urgency, customer identity,
and the body **with its character count**.

### Case-study rules that cap scores
1. Research/compliance claim without a source citation -> capped at 7.
2. Numbers must trace to the contexts; unprovenanced numbers score as fabrication.
3. Missing owner/merchant first name -> -1 merchant fit.
4. One low-friction next step; multi-action asks dilute.
5. Customer-facing must honour `language_pref` + relationship state (-2 if not).
6. Must use `voice.vocab_allowed` domain vocabulary, correctly.
7. Judgment > templating (case 5: recommend *against* the obvious play).
8. Meaningful, decodable `conversation_id`.
9. `rationale` must match the message; mismatch = penalty.
10. Repetition or fabrication -> capped at 5/dimension.
11. **Near-duplicating a case-study body = plagiarism penalty** (similarity check).

## Hard bans
- **No URLs in body** — "hard fail for that action", -3 each (api-call-examples F.4).
  This overrides challenge-brief 5.4 which permits URLs; take the stricter rule.
- No `voice.vocab_taboo` terms.
- No raw internal jargon (`ctr_below_peer_median`, `stale_posts:22d`, suppression keys,
  ids, snake_case tokens).
- No repeated body inside a conversation (-2 each).
- No invented offers, competitors, citations, slots.

## Replay scenarios (judge_simulator + api-call-examples 4.x)
1. **Auto-reply hell.** Simulator sends the same canned line 4x but with a *different*
   `conversation_id` each turn (`conv_auto_1..4`) => repeat detection must be keyed on
   **merchant**, not conversation. Documented ideal ladder: `send` (flag for owner) ->
   `wait` -> `end`.
2. **Intent transition.** `"Ok lets do it. Whats next?"` on an unknown conversation.
   Simulator passes only if the body contains one of
   `done|sending|draft|here|confirm|proceed|next` and contains **none** of
   `would you|do you|can you tell|what if|how about`.
3. **Hostile.** `"Stop messaging me. This is useless spam."` -> `end` is the clean pass.
   Real replay adds an off-topic follow-up ("help me file my GST?") -> stay on-mission,
   decline scope politely, redirect.

## Dataset realities that decide the score
- The judge scores against the **expanded** dataset: 30 canonical pairs, and ~half of them
  point at generated contexts.
- 40 of 50 merchants have `offers: []`, `signals: []`, `conversation_history: []`,
  `review_themes: []` and only `customer_aggregate.total_unique_ytd`.
- 75 of 100 triggers carry `payload: {"placeholder": true, "metric_or_topic": <kind>}`.
- Generated triggers are assigned to random merchants, so **kind/category mismatches are
  built in**: T08 is `chronic_refill_due` on a *dentist*, T29 is `recall_due` on a *yoga
  studio*, T15 is `customer_lapsed_soft` on a *pharmacy*.
- Generated customers can contradict themselves (`state:"new"` with `visits_total:5`) and
  carry `consent.scope:["promotional_offers"]` only — no `recall_reminders`.

=> Excellence on **sparse + mismatched** context is where this challenge is actually won.
Category context is always fully populated; merchant `identity`/`performance`/
`subscription` are always present. That is enough to be specific every single time.

## Post-submission twist
Judges push new `digest` items, shifted `performance`, new triggers, and surprise customer
scopes mid-test as higher `version`s. Incorporating them scores higher; ignoring them
scores lower; inventing scores lowest. => diff old vs new payload and cite what moved.
