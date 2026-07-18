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
  clients.py       - Client registry
  collectors/      - Merchant API data collectors
  tools/           - Agent tools (cross-channel reads)
  jobs/
    weekly.py      - Railway cron entrypoint
```
