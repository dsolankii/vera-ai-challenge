# Fast final run + deploy

Your current local layout can be:

```text
~/Downloads/vera-work/
  dataset/
  expanded/
  challenge-brief.md
  challenge-testing-brief.md
  judge_simulator.py
  vera-bot/
```

## 1. Final local checks

From `~/Downloads/vera-work/vera-bot`:

```bash
python3 test_local.py ../expanded
python3 test_adaptive.py ../expanded
python3 test_gap_fixes.py ../expanded
python3 test_hardening.py ../expanded
python3 test_score_quality.py ../expanded
python3 test_phase3_replay.py ../expanded
python3 generate_submission.py ../expanded submission.jsonl
```

Expected key lines:

```text
30 messages composed; problems: none
gap regression checks: PASS
hardening checks: PASS (12/12)
score-quality checks: PASS (13/13)
phase3 adaptive injection checks: PASS
phase4 replay checks: PASS
wrote 30 lines -> submission.jsonl
```

## 2. Local HTTP smoke test

Start a fresh bot:

```bash
python3 bot.py
```

In a second terminal:

```bash
curl http://localhost:8080/v1/healthz
python3 test_e2e.py http://localhost:8080 ../expanded
python3 test_replies.py http://localhost:8080
```

The full public trigger run should stay at max 20 actions/tick, return 200/no-op on equal context versions, produce no URLs, and remain far below the 30-second timeout.

## 3. Metadata before submission

Do not edit secrets into source. Set these on the host:

```text
TEAM_NAME=<your team name>
TEAM_MEMBERS=<real member name(s), comma-separated>
CONTACT_EMAIL=<real email>
SUBMITTED_AT=<UTC ISO timestamp>
VERA_LLM_REWRITE=0
```

`VERA_LLM_REWRITE=0` is the recommended submission setting. The deterministic bot does not need a Gemini key.

## 4. GitHub

Push the contents of `vera-bot/` to a repository. `.gitignore` already excludes `.env`, `__pycache__/`, and `.pyc` files. Do not commit any API key or the locally modified judge containing credentials.

## 5. Render

Create a Python Web Service from the repo:

- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Health check path: `/v1/healthz`
- Add the metadata environment variables above.
- Prefer a non-sleeping/stable instance because evaluation state is in memory.

After deployment, verify:

```bash
curl https://YOUR-HOST/v1/healthz
curl https://YOUR-HOST/v1/metadata
python3 test_e2e.py https://YOUR-HOST ../expanded
```

Then restart once cleanly before the real evaluation and avoid redeploying while the judge is running.

Submit the **base URL only**, e.g. `https://YOUR-HOST`, not `/v1/healthz` or another endpoint.
