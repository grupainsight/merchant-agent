"""
Merchant Agent - Loading weeks of product performance.

Used by the weekly job (last complete week + revision window) and by the
separate backfill command (history), so backfill never delays weekly reporting.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from autonoma_contracts.merchant_performance import last_complete_week

from src import performance_store
from src.collectors.product_performance import fetch_week
from src.performance_summary import summarize

logger = logging.getLogger(__name__)


@dataclass
class LoadOutcome:
    client_key: Optional[str]
    published: list = field(default_factory=list)      # period_start ISO dates
    lost_race: list = field(default_factory=list)
    skipped: list = field(default_factory=list)        # already published, not reloaded
    errors: list = field(default_factory=list)         # (period_start, message)
    api_calls: int = 0


def load_weeks(
    merchant_id: str,
    weeks: list[date],
    current_urls: dict[str, str],
    catalog_snapshot_date: Optional[str],
    run_id: Optional[str],
    reload: bool = True,
) -> LoadOutcome:
    """Fetch and publish each week; one failing week does not stop the others."""
    client_key = performance_store.resolve_client_key(merchant_id)
    outcome = LoadOutcome(client_key=client_key)
    if not client_key:
        # Contract: new flows refuse to write without a resolved client_key.
        outcome.errors.append((None, f"merchant_id {merchant_id} does not resolve to a client_key"))
        return outcome

    already = set()
    if not reload and weeks:
        already = set(performance_store.published_periods(client_key, merchant_id, min(weeks)))

    for week in weeks:
        if week.isoformat() in already:
            outcome.skipped.append(week.isoformat())
            continue
        try:
            load = fetch_week(merchant_id, week)
            outcome.api_calls += load.api_calls
            result = performance_store.publish_week(
                client_key, merchant_id, load, current_urls, catalog_snapshot_date, run_id,
            )
            (outcome.published if result.status == "published" else outcome.lost_race).append(week.isoformat())
        except Exception as e:
            logger.error(f"Week {week} not loaded: {e}")
            outcome.errors.append((week.isoformat(), f"{type(e).__name__}: {e}"[:300]))
    return outcome


def weeks_back(run_date: date, count: int) -> list[date]:
    """The last complete Monday-Sunday week and the count-1 weeks before it, newest first."""
    last = last_complete_week(run_date)
    return [last - timedelta(weeks=i) for i in range(count)]


def channel_summary(client_key: str, merchant_id: str, run_date: date) -> dict:
    last = last_complete_week(run_date)
    periods = performance_store.published_periods(client_key, merchant_id, since=last - timedelta(weeks=52))
    return summarize(periods, last)
