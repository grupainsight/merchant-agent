"""
Merchant Agent - History backfill of weekly product performance (one-off command).

Separate from the weekly cron so that loading history never delays the weekly
report:  python -m src.jobs.backfill_performance [--weeks N] [--reload] [--dry-run DIR]
Weeks already published are skipped unless --reload. Loads publish with
compare-and-set (src/performance_store.py), so running this while the weekly job
runs is safe: one load of a week wins, the other is discarded.
"""

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta

from src import runtime
from src.clients import CLIENTS
from src.config import config
from src.db import complete_run, create_run, get_previous_snapshot
from src.performance_job import channel_summary, load_weeks, weeks_back

logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("merchant-agent.backfill")


def backfill(weeks: int, reload: bool, run_date: date) -> list:
    """Returns the clients that failed completely (client, error)."""
    failures = []
    for client in CLIENTS:
        run = create_run(client_id=client.client_id, job_type="on_demand", scheduled_for=None)
        try:
            # Current feed links: the last complete catalog snapshot (current, not historical).
            snapshot = get_previous_snapshot(client.client_id, client.merchant_id,
                                             before_date=(run_date + timedelta(days=1)).isoformat())
            current_urls = {p["offer_id"]: p.get("product_url") for p in snapshot.products if p.get("offer_id")}
            outcome = load_weeks(client.merchant_id, weeks_back(run_date, weeks), current_urls,
                                 snapshot.snapshot_date, run["id"], reload=reload)
            summary = channel_summary(outcome.client_key, client.merchant_id, run_date) if outcome.client_key else None
            report = {
                "client_key": outcome.client_key,
                "catalog_snapshot": {"status": snapshot.status, "date": snapshot.snapshot_date, "offers": len(current_urls)},
                "published": outcome.published, "skipped": outcome.skipped, "lost_race": outcome.lost_race,
                "errors": [{"week": w, "error": m} for w, m in outcome.errors],
                "api_calls": outcome.api_calls, "summary": summary,
            }
            if runtime.is_dry_run():
                runtime.record("backfill_report", report)
            logger.info(f"{client.name}: {json.dumps({k: v for k, v in report.items() if k != 'summary'}, default=str)[:1500]}")
            complete_run(
                run_id=run["id"],
                status="PARTIAL" if outcome.errors else "SUCCESS",
                input_records=len(outcome.published),
                error_code="PRODUCT_PERFORMANCE_FAILED" if outcome.errors else None,
                error_message="; ".join(f"{w}: {m}" for w, m in outcome.errors)[:2000] or None,
            )
        except Exception as e:
            logger.error(f"Backfill failed for {client.name}: {e}", exc_info=True)
            failures.append((client, e))
            complete_run(run_id=run["id"], status="FAILED", error_code=type(e).__name__, error_message=str(e)[:2000])
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill weekly product performance")
    parser.add_argument("--weeks", type=int, default=config.perf_backfill_weeks)
    parser.add_argument("--reload", action="store_true", help="reload weeks that are already published")
    parser.add_argument("--dry-run", metavar="DIR", help="write nothing; database writes go to DIR as JSON lines")
    args = parser.parse_args()
    runtime.configure(dry_run_dir=args.dry_run, llm_enabled=False)
    sys.exit(1 if backfill(args.weeks, args.reload, datetime.now(UTC).date()) else 0)
