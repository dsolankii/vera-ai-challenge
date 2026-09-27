# Public test intelligence (not secret data)

This note separates official/public Magicpin information from third-party hypotheses.

## Official/publicly mirrored Magicpin testing details

Public copies of the Magicpin challenge/testing brief state that the production judge runs a 60-simulated-minute window in 5-minute ticks and interleaves adaptive context updates. Phase 3 injects:

- 5 new research/compliance digest items per category (as higher category-context versions)
- updated performance snapshots for 10 merchants (mix of spikes and dips)
- 15 new triggers across the test window
- 5 new customer contexts, each followed by a `recall_due` trigger 2 simulated minutes later

The same brief states that the top-10 replay contains exactly three five-turn deep dives:

1. Auto-reply hell: repeated WhatsApp Business canned reply four times; detect and exit gracefully.
2. Intent transition: after two qualifying turns the merchant says "ok let's do it"; switch to action immediately.
3. Hostile/off-topic: abuse followed by an unrelated GST-filing question; remain polite, on-mission, and know when to exit.

The main public brief names external trigger families including `weather_heatwave`, `local_news_event`, `category_research_digest_release`, `regulation_change`, `competitor_opened`, and `category_trend_movement`; internal examples include `perf_spike`, `perf_dip`, `milestone_reached`, `dormant_with_vera`, `customer_lapsed_soft`, `appointment_tomorrow`, `review_theme_emerged`, and `scheduled_recurring`.

## Important limitation

The exact values/text of the 5x-per-category digest items, the exact 10 merchants whose performance changes, the exact 15 injected trigger payloads, and the identities of the 5 surprise-customer merchants are not publicly disclosed in the official material found. Any repository claiming exact hidden payloads should be treated as a participant's simulation unless it provides a verifiable Magicpin result artifact.

## Third-party clue, not official

One public participant repository reports testing invented/novel Phase-3-style scenarios including weather, local events, and trend movements, and uses an LLM fallback for unknown trigger kinds. This is consistent with the official brief's named trigger families, but it is not evidence of the exact hidden payloads.

## Recommended sequence

Implementation status in v1.6:
1. Known benchmark was measured first (v1.5 exact/raw mean 42.43/50).
2. Official-but-unseen trigger families now have dedicated grounded handlers plus an unknown-trigger fact-first fallback.
3. Canonical `submission.jsonl` was regenerated and is byte-identical to v1.5, so known-case copy was not changed by the Phase-3 hardening.
