# merchant-agent

Autonoma Platform - Merchant Center Agent.
Monitors Google Merchant Center product health, account issues, and market insights.

## Architecture

```
Railway cron (weekly, Tuesday 07:00 UTC)
    |
    v
Collect (Merchant API) -> Normalize -> Snapshot Compare
    |
    v (if significant change)
LLM Synthesis (Claude Sonnet) -> Recommendations + Dashboard Module
    |
    v
Supabase (shared memory) + Resend (email report)
```

## Setup

1. Copy `.env.example` to `.env` and fill in values
2. Complete `developerRegistration` in Google Cloud Console
3. Add client to `src/clients.py`
4. Deploy to Railway with cron schedule `0 7 * * 2`

## Dry run

Reads production data (Merchant API, previous snapshot, existing recommendations)
and writes nothing: database writes and emails go to JSON lines in DIR, and the
Supabase client refuses writes. `--no-llm` also skips the Claude call.

```
python -m src.jobs.weekly --dry-run DIR [--no-llm]
```

Same via env: `MERCHANT_DRY_RUN_DIR=DIR`, `MERCHANT_LLM=off`.

## Tests

```
pip install -r requirements.txt pytest && python -m pytest tests
```

## Contracts

Uses shared schemas from [autonoma-contracts](https://github.com/grupainsight/autonoma-contracts).

## Structure

```
src/
  config.py        - Environment config
  auth.py          - Google OAuth
  db.py            - Supabase client (shared memory)
  email.py         - Resend notifications
  snapshot.py      - Snapshot comparison logic
  product_status.py - Single definition of product status and issue counts
  runtime.py       - Dry-run mode (no writes, emails to file)
  clients.py       - Client registry
  collectors/      - Merchant API data collectors
  tools/           - Agent tools (cross-channel reads)
  jobs/
    weekly.py      - Railway cron entrypoint
```
