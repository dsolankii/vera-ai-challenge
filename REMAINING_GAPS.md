# Remaining gaps after v1.6

These are operational/unknown-evaluator risks, not known contract failures.

1. **Exact secret Phase-3 payloads remain unavailable.** v1.6 implements every additional trigger family explicitly named in the official brief plus a grounded unknown-trigger fallback, but it does not pretend to know secret values.
2. **Cloud restart durability.** Judge/runtime state is in memory. Prefer stable non-sleeping hosting for the evaluation window; persistence would be needed only if the chosen host can restart the process mid-run.
3. **External execution is intentionally absent.** The challenge API does not provide Google-post, booking, payment, or WhatsApp-send connectors, so replies stop at ready/confirmed copy instead of claiming actions happened.
4. **Deployment metadata must be real.** Set `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, and `SUBMITTED_AT` before submission.
5. **Optional LLM rewrite stays OFF.** Deterministic v1.6 preserves the known 30 messages exactly. Enable wording polish only if repeated evaluation proves a reliable gain without quota/latency risk.
