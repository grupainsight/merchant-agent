"""
Merchant Agent - Supabase client.
Shared memory layer for all Autonoma agents.

Every write goes through _insert/_update/_upsert so that a dry run records it
to a file instead of touching production (see src/runtime.py).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Optional
from supabase import create_client, Client

from src import runtime
from src.config import config
from src.snapshot import PreviousSnapshot

logger = logging.getLogger(__name__)

_client: Optional[Client] = None

# Rows per request when reading a snapshot. PostgREST may cap a response below
# this (max-rows), so readers continue after the last id actually returned.
PAGE_SIZE = 1000
# How many earlier snapshot dates to try before giving up on a complete one.
SNAPSHOT_CANDIDATES = 4


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(config.supabase_url, config.supabase_key)
        if runtime.is_dry_run():
            _client = runtime.ReadOnlyClient(_client)
    return _client


def _insert(table: str, rows: Any) -> list[dict]:
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": table, "op": "insert", "rows": rows})
        return rows if isinstance(rows, list) else [rows]
    return get_client().table(table).insert(rows).execute().data


def _update(table: str, values: dict, row_id: str) -> None:
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": table, "op": "update", "id": row_id, "values": values})
        return
    get_client().table(table).update(values).eq("id", row_id).execute()


def _upsert(table: str, row: dict, on_conflict: str) -> None:
    if runtime.is_dry_run():
        runtime.record("db_writes", {"table": table, "op": "upsert", "on_conflict": on_conflict, "row": row})
        return
    get_client().table(table).upsert(row, on_conflict=on_conflict).execute()


# ============================================================
# Agent runs
# ============================================================

def create_run(client_id: str, job_type: str, scheduled_for: Optional[str] = None) -> dict:
    """Create an agent run record. Returns the run dict with id."""
    run = {
        "client_id": client_id,
        "agent": "merchant",
        "job_type": job_type,
        "scheduled_for": scheduled_for,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "RUNNING",
        "llm_used": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if runtime.is_dry_run():
        run["id"] = f"dry-{uuid.uuid4()}"
    return _insert("agent_runs", run)[0]


def complete_run(
    run_id: str,
    status: str,
    input_records: int = 0,
    output_records: int = 0,
    llm_used: bool = False,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Update a run record with completion data."""
    cost = (input_tokens / 1_000_000 * 3) + (output_tokens / 1_000_000 * 15) if llm_used else 0

    _update("agent_runs", {
        "completed_at": datetime.now(UTC).isoformat(),
        "status": status,
        "input_records": input_records,
        "output_records": output_records,
        "llm_used": llm_used,
        "input_tokens": input_tokens if llm_used else None,
        "output_tokens": output_tokens if llm_used else None,
        "llm_cost_usd": cost if llm_used else None,
        "error_code": error_code,
        "error_message": error_message,
    }, run_id)


# ============================================================
# Dashboard module
# ============================================================

def upsert_dashboard_module(module_data: dict) -> None:
    """Upsert dashboard module for this agent. Keyed on (client_id, module)."""
    module_data["updated_at"] = datetime.now(UTC).isoformat()
    _upsert("agent_dashboard_modules", module_data, on_conflict="client_id,module")


# ============================================================
# Recommendations
# ============================================================

def get_active_recommendations(client_id: str, dedupe_key: str) -> list[dict]:
    """Get existing recommendations for dedupe check."""
    db = get_client()
    result = (
        db.table("agent_recommendations")
        .select("*")
        .eq("client_id", client_id)
        .eq("dedupe_key", dedupe_key)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


def insert_recommendation(rec: dict) -> None:
    """Insert a new recommendation. Never overwrites an existing id."""
    rec["updated_at"] = datetime.now(UTC).isoformat()
    _insert("agent_recommendations", rec)


def update_recommendation(rec_id: str, values: dict) -> None:
    values["updated_at"] = datetime.now(UTC).isoformat()
    _update("agent_recommendations", values, rec_id)


def add_recommendation_event(event: dict) -> None:
    """Record a status change event."""
    _insert("agent_recommendation_events", event)


# ============================================================
# Merchant-specific
# ============================================================

def _today_product_ids(client_id: str, merchant_id: str, snapshot_date: str) -> set[str]:
    rows = _fetch_all(
        lambda: get_client().table("merchant_product_status").select("id,product_id")
        .eq("client_id", client_id).eq("merchant_id", merchant_id).eq("snapshot_date", snapshot_date)
    )
    return {r["product_id"] for r in rows}


def save_product_status(records: list[dict], batch_size: int = 200) -> str:
    """
    Save product status snapshot in batches. A rerun on the same day does not
    insert the catalog again when that day's snapshot already covers every
    product. Returns "saved" or "already_saved".
    """
    if not records:
        return "saved"
    first = records[0]
    existing = _today_product_ids(first["client_id"], first["merchant_id"], first["snapshot_date"])
    if existing and {r["product_id"] for r in records} <= existing:
        logger.info(f"Snapshot {first['snapshot_date']} already saved ({len(existing)} products) - not inserting again")
        return "already_saved"
    if existing:
        # An earlier run today stopped part-way. Insert the full catalog; readers
        # keep the newest row per product, so the snapshot ends up complete.
        logger.warning(f"Snapshot {first['snapshot_date']} has {len(existing)} of {len(records)} products - inserting full catalog")
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        _insert("merchant_product_status", batch)
        logger.info(f"Saved product status batch {i // batch_size + 1} ({len(batch)} records)")
    return "saved"


def _find_row_id(table: str, client_id: str, merchant_id: str, date_column: str, value: str) -> Optional[str]:
    result = (
        get_client().table(table).select("id")
        .eq("client_id", client_id).eq("merchant_id", merchant_id).eq(date_column, value)
        .order("created_at", desc=True).limit(1).execute()
    )
    return result.data[0]["id"] if result.data else None


def save_merchant_metrics(metrics: dict) -> None:
    """Save weekly metrics. A rerun on the same day updates that day's row."""
    row_id = _find_row_id("merchant_metrics", metrics["client_id"], metrics["merchant_id"], "metric_date", metrics["metric_date"])
    if row_id:
        _update("merchant_metrics", metrics, row_id)
    else:
        _insert("merchant_metrics", metrics)


def save_account_issues(issues: list[dict]) -> None:
    """Save account-level issues. Skipped when this day's issues are already saved."""
    if not issues:
        return
    first = issues[0]
    if _find_row_id("merchant_account_issues", first["client_id"], first["merchant_id"], "snapshot_date", first["snapshot_date"]):
        logger.info(f"Account issues for {first['snapshot_date']} already saved - not inserting again")
        return
    _insert("merchant_account_issues", issues)


def save_merchant_report(report: dict) -> None:
    """Save full weekly report. A rerun on the same day updates that day's report."""
    row_id = _find_row_id("merchant_reports", report["client_id"], report["merchant_id"], "report_date", report["report_date"])
    if row_id:
        _update("merchant_reports", report, row_id)
    else:
        _insert("merchant_reports", report)


# ============================================================
# Previous snapshot
# ============================================================

SNAPSHOT_COLUMNS = "id,product_id,offer_id,status_per_reporting_context,issues,snapshot_date,created_at"


def _fetch_all(build_query) -> list[dict]:
    """
    Read every row of a query, page by page, with keyset pagination on id
    (the query must select id). Offset pagination re-sorts the whole snapshot
    for every page and hit the statement timeout at ~10k rows.
    """
    rows: list[dict] = []
    last_id = None
    while True:
        query = build_query()
        if last_id is not None:
            query = query.gt("id", last_id)
        page = query.order("id").limit(PAGE_SIZE).execute().data
        if not page:
            return rows
        rows.extend(page)
        last_id = page[-1]["id"]


def _latest_per_product(rows: list[dict]) -> list[dict]:
    """One row per product_id: the newest one (same-day reruns insert duplicates)."""
    latest: dict[str, dict] = {}
    for r in rows:
        cur = latest.get(r["product_id"])
        if cur is None or (r.get("created_at") or "") > (cur.get("created_at") or ""):
            latest[r["product_id"]] = r
    return list(latest.values())


def get_previous_snapshot(client_id: str, merchant_id: str, before_date: str) -> PreviousSnapshot:
    """
    The most recent complete snapshot strictly before `before_date` (the current
    run's date, so a same-day rerun never compares a run with itself).

    A snapshot date counts as complete when it holds at least as many distinct
    products as the run that wrote it reported in merchant_metrics. Incomplete
    dates (a run that stopped while saving) are skipped in favour of an older one.
    """
    db = get_client()
    try:
        metric_rows = (
            db.table("merchant_metrics")
            .select("metric_date,total_products,created_at")
            .eq("client_id", client_id).eq("merchant_id", merchant_id)
            .lt("metric_date", before_date)
            .order("metric_date", desc=True).order("created_at", desc=True)
            .limit(SNAPSHOT_CANDIDATES * 5)
            .execute().data
        )
        expected_by_date: dict[str, Optional[int]] = {}
        for m in metric_rows:
            expected_by_date.setdefault(m["metric_date"], m.get("total_products"))
        candidates = list(expected_by_date.items())[:SNAPSHOT_CANDIDATES]
        if not candidates:
            return PreviousSnapshot(status="none")

        skipped = []
        for snapshot_date, expected in candidates:
            def query(d=snapshot_date):
                return (
                    db.table("merchant_product_status").select(SNAPSHOT_COLUMNS)
                    .eq("client_id", client_id).eq("merchant_id", merchant_id).eq("snapshot_date", d)
                )
            rows = _fetch_all(query)
            total = (
                db.table("merchant_product_status").select("id", count="exact")
                .eq("client_id", client_id).eq("merchant_id", merchant_id).eq("snapshot_date", snapshot_date)
                .limit(1).execute().count
            )
            if total is not None and total != len(rows):
                raise RuntimeError(
                    f"snapshot {snapshot_date}: read {len(rows)} rows but the table holds {total}"
                )
            products = _latest_per_product(rows)
            if expected and len(products) >= expected:
                logger.info(
                    f"Previous snapshot {snapshot_date}: {len(products)} products "
                    f"({len(rows)} rows, expected {expected})"
                )
                return PreviousSnapshot(
                    status="ok", snapshot_date=snapshot_date, products=products,
                    raw_rows=len(rows), expected_products=expected, skipped_dates=skipped,
                )
            logger.warning(f"Snapshot {snapshot_date} incomplete: {len(products)} products, expected {expected}")
            skipped.append({"date": snapshot_date, "products": len(products), "expected": expected})
        return PreviousSnapshot(status="incomplete", skipped_dates=skipped)
    except Exception as e:
        logger.error(f"Failed to read previous snapshot: {e}")
        return PreviousSnapshot(status="error", error=str(e)[:500])


# ============================================================
# Cross-agent reads
# ============================================================

def get_ads_data(client_id: str) -> Optional[dict]:
    """Read latest Ads Agent data from shared memory."""
    db = get_client()
    result = (
        db.table("agent_dashboard_modules")
        .select("*")
        .eq("client_id", client_id)
        .eq("module", "ads")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_seo_data(client_id: str) -> Optional[dict]:
    """Read latest SEO Agent data from shared memory."""
    db = get_client()
    result = (
        db.table("agent_dashboard_modules")
        .select("*")
        .eq("client_id", client_id)
        .eq("module", "seo")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None
