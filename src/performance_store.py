"""
Merchant Agent - Storage of weekly product performance (sql/005 in autonoma-contracts).

Publication protocol, so that readers never see a partial or mixed week:
1. insert the offer rows of a new load under a new fetch_id (not visible yet);
2. publish: insert the week's row in merchant_performance_periods, or update it
   only if its fetch_id is still the one read before the load (compare-and-set);
   a load that loses the race deletes its own rows and stops;
3. delete the rows of the replaced load (that exact fetch_id).
Rows of loads that stopped part-way stay unpublished and are deleted by the next
load of that week once older than ORPHAN_AGE. In a dry run every write goes to
the dry-run file (src/runtime.py).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Optional

from src import runtime
from src.collectors.product_performance import METHODS, WeekLoad
from src.db import get_client

logger = logging.getLogger(__name__)

ROWS_TABLE = "merchant_product_performance"
PERIODS_TABLE = "merchant_performance_periods"
BATCH_SIZE = 500
ORPHAN_AGE = timedelta(days=1)


@dataclass
class PublishResult:
    status: str               # "published" or "lost_race"
    fetch_id: str
    replaced_fetch_id: Optional[str]
    rows: int


def resolve_client_key(merchant_id: str) -> Optional[str]:
    """client_profile.client_key for a Merchant Center id, via the production resolver (sql/004)."""
    result = get_client().rpc("resolve_client_key", {"p_kind": "merchant_id", "p_value": merchant_id}).execute()
    return result.data or None


# Weeks "published" by this process in a dry run, so that later reads in the
# same dry run (summary, reloads) see them. Keyed by (client_key, merchant_id, period_start).
_dry_published: dict[tuple, dict] = {}


def _read_periods(build_query) -> list[dict]:
    try:
        return build_query().execute().data
    except Exception as e:
        # A dry run may happen before sql/005 is applied: the table does not exist yet.
        if runtime.is_dry_run() and ("PGRST205" in str(e) or "42P01" in str(e)):
            logger.warning(f"[dry run] {PERIODS_TABLE} not found in the database - using dry-run state only")
            return []
        raise


def _published(client_key: str, merchant_id: str, period_start: date) -> Optional[dict]:
    if runtime.is_dry_run() and (client_key, merchant_id, period_start.isoformat()) in _dry_published:
        return _dry_published[(client_key, merchant_id, period_start.isoformat())]
    rows = _read_periods(lambda: (
        get_client().table(PERIODS_TABLE).select("*")
        .eq("client_key", client_key).eq("merchant_id", merchant_id)
        .eq("period_start", period_start.isoformat()).limit(1)
    ))
    return rows[0] if rows else None


def published_periods(client_key: str, merchant_id: str, since: date) -> dict[str, dict]:
    """Published weeks from `since` on, keyed by period_start (ISO date)."""
    rows = _read_periods(lambda: (
        get_client().table(PERIODS_TABLE).select("*")
        .eq("client_key", client_key).eq("merchant_id", merchant_id)
        .gte("period_start", since.isoformat()).order("period_start")
    ))
    periods = {r["period_start"]: r for r in rows}
    if runtime.is_dry_run():
        periods.update({k[2]: v for k, v in _dry_published.items()
                        if k[0] == client_key and k[1] == merchant_id and k[2] >= since.isoformat()})
    return dict(sorted(periods.items()))


def _delete_rows(client_key: str, merchant_id: str, period_start: date, fetch_id: str) -> None:
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": ROWS_TABLE, "op": "delete", "period_start": period_start, "fetch_id": fetch_id})
        return
    (get_client().table(ROWS_TABLE).delete()
     .eq("client_key", client_key).eq("merchant_id", merchant_id)
     .eq("period_start", period_start.isoformat()).eq("fetch_id", fetch_id).execute())


def _delete_orphans(client_key: str, merchant_id: str, period_start: date, keep_fetch_id: Optional[str], now: datetime) -> None:
    """Rows of loads of this week that were never published and are older than ORPHAN_AGE."""
    cutoff = (now - ORPHAN_AGE).isoformat()
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": ROWS_TABLE, "op": "delete_orphans", "period_start": period_start,
                                     "keep_fetch_id": keep_fetch_id, "older_than": cutoff})
        return
    query = (get_client().table(ROWS_TABLE).delete()
             .eq("client_key", client_key).eq("merchant_id", merchant_id)
             .eq("period_start", period_start.isoformat()).lt("created_at", cutoff))
    if keep_fetch_id:
        query = query.neq("fetch_id", keep_fetch_id)
    query.execute()


def _insert(table: str, rows) -> None:
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": table, "op": "insert", "rows": rows})
        return
    get_client().table(table).insert(rows).execute()


def _period_row(client_key, merchant_id, load: WeekLoad, fetch_id, now, run_id, catalog_snapshot_date) -> dict:
    row = {
        "client_key": client_key,
        "merchant_id": merchant_id,
        "period_start": load.period_start.isoformat(),
        "period_end": load.period_end.isoformat(),
        "fetch_id": fetch_id,
        "fetched_at": now.isoformat(),
        "run_id": None if (run_id or "").startswith("dry-") else run_id,
        "catalog_snapshot_date": catalog_snapshot_date,
        "offer_rows": len(load.offers),
        "offers": len({offer for offer, _ in load.offers}),
        "rows_without_offer": load.rows_without_offer,
        "rows_unknown_method": load.rows_unknown_method,
        "updated_at": now.isoformat(),
    }
    for method in METHODS:
        prefix = method.lower()
        row[f"{prefix}_impressions"], row[f"{prefix}_clicks"] = load.totals[method]
        row[f"{prefix}_impressions_by_offer"], row[f"{prefix}_clicks_by_offer"] = load.by_offer(method)
    return row


def publish_week(
    client_key: str,
    merchant_id: str,
    load: WeekLoad,
    current_urls: dict[str, str],
    catalog_snapshot_date: Optional[str],
    run_id: Optional[str],
    now: Optional[datetime] = None,
) -> PublishResult:
    """
    Store one fully fetched week and publish it (see module docstring).
    current_urls: offer_id -> feed link in the catalog snapshot of this run
    (current, not historical; offers missing from it get NULL / false).
    """
    now = now or datetime.now(UTC)
    current = _published(client_key, merchant_id, load.period_start)
    old_fetch_id = current["fetch_id"] if current else None
    fetch_id = str(uuid.uuid4())

    _delete_orphans(client_key, merchant_id, load.period_start, old_fetch_id, now)

    rows = [
        {
            "client_key": client_key,
            "merchant_id": merchant_id,
            "period_start": load.period_start.isoformat(),
            "period_end": load.period_end.isoformat(),
            "offer_id": offer_id,
            "marketing_method": method,
            "impressions": impressions,
            "clicks": clicks,
            "current_product_url": current_urls.get(offer_id),
            "in_current_catalog": offer_id in current_urls,
            "fetch_id": fetch_id,
        }
        for (offer_id, method), (impressions, clicks) in sorted(load.offers.items())
    ]
    try:
        for i in range(0, len(rows), BATCH_SIZE):
            _insert(ROWS_TABLE, rows[i:i + BATCH_SIZE])
    except Exception:
        # Nothing was published; remove what this load wrote (the orphan sweep catches it otherwise).
        try:
            _delete_rows(client_key, merchant_id, load.period_start, fetch_id)
        except Exception as cleanup_error:
            logger.warning(f"Could not remove unpublished rows of {fetch_id}: {cleanup_error}")
        raise

    period = _period_row(client_key, merchant_id, load, fetch_id, now, run_id, catalog_snapshot_date)
    if not _publish(period, old_fetch_id):
        logger.warning(f"Week {load.period_start}: another load published first - discarding {fetch_id}")
        _delete_rows(client_key, merchant_id, load.period_start, fetch_id)
        return PublishResult("lost_race", fetch_id, old_fetch_id, len(rows))

    if old_fetch_id:
        _delete_rows(client_key, merchant_id, load.period_start, old_fetch_id)
    return PublishResult("published", fetch_id, old_fetch_id, len(rows))


def _publish(period: dict, expected_fetch_id: Optional[str]) -> bool:
    """Insert the week's row, or replace it only while it still holds expected_fetch_id."""
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": PERIODS_TABLE, "op": "publish",
                                     "expected_fetch_id": expected_fetch_id, "row": period})
        _dry_published[(period["client_key"], period["merchant_id"], period["period_start"])] = period
        return True
    table = get_client().table(PERIODS_TABLE)
    if expected_fetch_id is None:
        try:
            table.insert(period).execute()
            return True
        except Exception as e:
            if "23505" in str(e) or "duplicate key" in str(e):
                return False
            raise
    updated = (
        table.update(period)
        .eq("client_key", period["client_key"]).eq("merchant_id", period["merchant_id"])
        .eq("period_start", period["period_start"]).eq("fetch_id", expected_fetch_id)
        .execute().data
    )
    return bool(updated)


def apply_retention(client_key: str, merchant_id: str, keep_from: date) -> None:
    """
    Delete weeks that start before keep_from, for this client and account only.
    The publication rows go first, so a reader never sees a published week without its rows.
    """
    for table in (PERIODS_TABLE, ROWS_TABLE):
        if runtime.is_dry_run():
            runtime.record("db_writes", {"table": table, "op": "retention", "before": keep_from})
            continue
        (get_client().table(table).delete()
         .eq("client_key", client_key).eq("merchant_id", merchant_id)
         .lt("period_start", keep_from.isoformat()).execute())
