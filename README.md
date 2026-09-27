# Vera Engine v1.6 — magicpin AI Challenge

**Approach:** a deterministic, stateful decision engine rather than a generic chatbot. Trigger scoring decides *whether/what* to send; category/merchant/customer context decides *how* to say it; runtime guards reject stale, ungrounded, duplicated, non-consensual, or off-category sends.

## Contract

- Public function: `compose(category, merchant, trigger, customer=None)`
- API: `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata`
- Context versions: same version = idempotent 200 no-op; lower = 409; higher replaces immediately.
- Max 20 actions/tick; one merchant-facing action per merchant/tick; customer sends require ownership + relevant consent.
- Numeric claims must come from pushed context (or an explicitly labelled proposal/calculation); category taboo phrases are blocked.
- Expired triggers, missing customer context, explicit opt-outs, wrong merchant/customer links, and clearly irrelevant local/category events are skipped.

## Composition + adaptation

Known trigger families have dedicated composers. v1.6 also implements official-brief Phase-3 families that are not all present in the public expanded set: `weather_heatwave`, `local_news_event`, `category_research_digest_release`, `category_trend_movement`, and `scheduled_recurring`.

Completely unseen trigger kinds use a **fact-first fallback**: extract the pushed headline/title/metric/trend, verify category/location relevance, combine it with current merchant state, and either create one grounded next step or send nothing. It never invents a hidden payload.

Fresh category versions are used immediately (including newly appended digest items). Updated merchant performance replaces stale values. Customer-scoped triggers wait for the customer context instead of guessing.

## Conversation flow

`/v1/reply` handles STOP, WhatsApp Business auto-replies, hostility, off-topic asks, customer confirmations, questions, delays, soft-no, and explicit commitment. `"Ok, let's do it. What's next?"` moves directly to a ready artifact; it does not restart qualification. Repeated canned auto-replies are detected and ended gracefully. Hostile/off-topic turns stay polite and on-mission. The engine never claims a post, booking, payment, or dispatch occurred when it did not.

## Tradeoffs

The submission-safe default uses **no runtime LLM**, giving deterministic output and sub-second latency. An optional Gemini wording-only pass exists but is OFF by default and is accepted only when grounding/taboo checks still pass. Runtime state is in memory, so stable non-sleeping hosting is preferred for the evaluation window.

## Validation

```bash
python3 test_local.py /path/to/expanded
python3 test_adaptive.py /path/to/expanded
python3 test_gap_fixes.py /path/to/expanded
python3 test_hardening.py /path/to/expanded
python3 test_score_quality.py /path/to/expanded
python3 test_phase3_replay.py /path/to/expanded
python3 generate_submission.py /path/to/expanded submission.jsonl
```

Local result: canonical 30/30 PASS; hardening 12/12 PASS; score-quality 13/13 PASS; Phase-3 adaptive injection PASS; Phase-4 replay PASS; 15-new-trigger stress PASS; HTTP run produced 88 unique actions with max 20/tick and 0 URLs. The 30-line `submission.jsonl` is byte-identical to v1.5, so the known-case message set is preserved while v1.6 adds hidden/adaptive coverage.

**Additional context that would help most:** exact schemas for post-submission trigger payloads and whether evaluator process restarts may occur during the 60-minute simulated window.
