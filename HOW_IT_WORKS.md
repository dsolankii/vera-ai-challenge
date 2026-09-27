# How the final Vera bot works (v1.6)

## 30-second interview answer

I built Vera as a deterministic decision engine rather than an open-ended chatbot. It stores versioned category, merchant, customer and trigger context. On every tick it ranks available signals using urgency, trigger type, merchant performance, customer immediacy and recent conversation relevance, then sends at most one Vera message per merchant. Trigger-specific composers write from real context, while runtime guards check consent, recipient-scoped suppression, expiry, category taboo language and numeric provenance. On replies, the bot handles STOP, auto-replies, hostility, off-topic questions, YES, later/no and customer confirmations. When a merchant says yes, the useful draft is returned immediately; the bot never claims it published, booked or dispatched something it did not actually execute.

## Context semantics

- Same version: 200 idempotent no-op.
- Older version: 409 `stale_version`.
- Higher version: replaces the stored context.
- Re-posting context never wipes conversation state.

## Hidden-test safeguards

- New digest items: explicit item ID wins; otherwise relevance + recency chooses deterministically.
- Updated merchant metrics: higher context versions are used immediately.
- Surprise customers: no customer-facing message until matching customer context and consent exist.
- Suppression: `(recipient, suppression_key)`, not global.
- Trigger expiry: expired triggers are skipped.
- Category taboos: blocked at runtime.
- History: merchant intent/preferences affect ranking and relevant copy.
- Wait/re-engage: a WAIT can open one fresh follow-up conversation after the requested delay.
- Cadence: low-urgency proactive messages stop after three unanswered sends on separate wake-ups; real engagement resets the cadence.
- Auto-reply: first canned response waits; repeated canned response ends the thread and clears the scheduled follow-up.

## Why deterministic first?

The challenge requires repeatable behavior for the same input and heavily penalizes fabricated facts. A deterministic rules + retrieval layer makes selection, grounding and replay behavior inspectable. The optional Gemini wording pass is therefore OFF by default. If enabled for experiments, it sees only the already-grounded draft, uses temperature 0, and the result is discarded unless the same grounding/taboo validation still passes.

## What the validator really guarantees

It does **not** claim that arbitrary prose can never be wrong. It specifically checks numeric factual claims against pushed context or explicitly registered calculations/proposals, rejects category taboo language, and uses deterministic writers that only receive the supplied contexts. This is a more accurate claim than saying the system can never hallucinate anything.

## Current limitation to state honestly in interview

The bot does not call external Google/booking/WhatsApp publishing systems, because the challenge exposes only the message-engine API. It therefore produces and approves the final copy/request without pretending an external action happened. State is in memory, so a stable host should be used for the judge window.


## v1.4 scoring-quality pass

The score pass does not add a generic "marketing polish" template. It strengthens the reasoning chain that the official rubric scores:

- **Specificity:** current merchant numbers, offer prices, dates, distances, source/batch details and peer comparisons are surfaced when they actually explain the action.
- **Category fit:** messages use the category's operator vocabulary (for example covers/AOV, membership churn/trial footfall, IOPA, molecule/batch/pharmacist counsel) without forcing jargon.
- **Merchant fit:** the copy combines trigger data with the merchant's current performance, live offers, locality and relevant conversation continuity.
- **Decision quality:** composers explain the choice (for example verify discovery before discounting, retention over acquisition during a seasonal dip, trust repair before another restaurant discount).
- **Engagement:** one concrete artifact/next step ends the message; YES/CONFIRM transitions deliver the artifact immediately.

A dedicated `test_score_quality.py` protects these score-driving anchors so later refactors cannot silently turn the copy generic again.


## v1.5: surgical score tuning

After the clean v1.4 judge run, the engine keeps the high-scoring templates unchanged and only specializes three weak voice cases. Pharmacy GBP verification uses a neighbourhood-pharmacist register, yoga momentum uses a calm parent/program register, and dormant salon re-entry uses a warm no-pressure register. All facts still come from pushed context and the numeric/taboo validator still runs after composition.

## v1.6: adaptive hidden-context path

When a new trigger kind arrives after submission, Vera does not map it to a canned generic promotion. It first checks explicit category and geography, then extracts the strongest pushed fact (headline/title/digest item/metric/search trend), combines only relevant current merchant state, and chooses a category-native next step. If the event is clearly for another city/category, it sends nothing. If a customer-scoped trigger arrives before its customer context, it waits.

The public Phase-3 shape is regression-tested directly: 5 new digest items per category, 10 higher-version merchant snapshots, 15 new triggers, and 5 mid-test customers followed by recall triggers. Replay tests cover four identical canned auto-replies, the two-turn qualification-to-action transition, and hostile-to-GST scope handling.
