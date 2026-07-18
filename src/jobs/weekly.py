"""
Merchant Agent - Weekly job.
Railway cron entrypoint: python -m src.jobs.weekly
Runs the full pipeline: collect -> normalize -> snapshot compare -> LLM synthesis if needed.
Process MUST exit after completion (Railway cron requirement).
"""

import logging
import sys
from datetime import UTC, datetime

from src.clients import CLIENTS
from src.config import config
from src.db import (
    create_run,
    complete_run,
)

logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("merchant-agent")


def run_weekly():
    """Execute weekly pipeline for all clients."""
    logger.info(f"Merchant Agent weekly run starting - {len(CLIENTS)} clients")

    if not CLIENTS:
        logger.warning("No clients configured. Exiting.")
        return

    for client in CLIENTS:
        run_record = None
        try:
            # Create run record
            run_record = create_run(
                client_id=client.client_id,
                job_type="weekly",
                scheduled_for=datetime.now(UTC).isoformat(),
            )
            run_id = run_record["id"]
            logger.info(f"Processing {client.name} (run: {run_id})")

            # TODO: Step 1 - Collect (get_merchant_product_health, get_merchant_account_issues)
            # TODO: Step 2 - Normalize (map to contract schema)
            # TODO: Step 3 - Snapshot compare (src.snapshot.compare_snapshots)
            # TODO: Step 4 - If significant change -> LLM synthesis
            # TODO: Step 5 - Save dashboard module, recommendations, report
            # TODO: Step 6 - Send email if needed

            complete_run(
                run_id=run_id,
                status="SUCCESS",
                input_records=0,
                output_records=0,
            )
            logger.info(f"Completed {client.name}")

        except Exception as e:
            logger.error(f"Failed processing {client.name}: {e}", exc_info=True)
            if run_record:
                complete_run(
                    run_id=run_record["id"],
                    status="FAILED",
                    error_code=type(e).__name__,
                    error_message=str(e)[:2000],
                )

    logger.info("Merchant Agent weekly run complete")


if __name__ == "__main__":
    run_weekly()
    sys.exit(0)
