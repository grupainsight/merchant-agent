"""
Merchant Agent - Supabase client.
Shared memory layer for all Autonoma agents.
"""

from datetime import UTC, datetime
from typing import Any, Optional
from supabase import create_client, Client

from src.config import config


_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(config.supabase_url, config.supabase_key)
    return _client


# ============================================================
# Agent runs
# ============================================================

def create_run(client_id: str, job_type: str, scheduled_for: Optional[str] = None) -> dict:
    """Create an agent run record. Returns the run dict with id."""
    db = get_client()
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
    result = db.table("agent_runs").insert(run).execute()
    return result.data[0]


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
    db = get_client()
    cost = (input_tokens / 1_000_000 * 3) + (output_tokens / 1_000_000 * 15) if llm_used else 0

    db.table("agent_runs").update({
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
    }).eq("id", run_id).execute()


# ============================================================
# Dashboard module
# ============================================================

def upsert_dashboard_module(module_data: dict) -> None:
    """Upsert dashboard module for this agent. Keyed on (client_id, module)."""
    db = get_client()
    module_data["updated_at"] = datetime.now(UTC).isoformat()
    db.table("agent_dashboard_modules").upsert(
        module_data,
        on_conflict="client_id,module",
    ).execute()


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


def upsert_recommendation(rec: dict) -> None:
    """Insert or update a recommendation."""
    db = get_client()
    rec["updated_at"] = datetime.now(UTC).isoformat()
    db.table("agent_recommendations").upsert(rec).execute()


def add_recommendation_event(event: dict) -> None:
    """Record a status change event."""
    db = get_client()
    db.table("agent_recommendation_events").insert(event).execute()


# ============================================================
# Merchant-specific
# ============================================================

def save_product_status(records: list[dict]) -> None:
    """Save product status snapshot."""
    db = get_client()
    if records:
        db.table("merchant_product_status").insert(records).execute()


def save_merchant_metrics(metrics: dict) -> None:
    """Save weekly metrics."""
    db = get_client()
    db.table("merchant_metrics").insert(metrics).execute()


def save_account_issues(issues: list[dict]) -> None:
    """Save account-level issues."""
    db = get_client()
    if issues:
        db.table("merchant_account_issues").insert(issues).execute()


def save_merchant_report(report: dict) -> None:
    """Save full weekly report."""
    db = get_client()
    db.table("merchant_reports").insert(report).execute()


def get_previous_snapshot(client_id: str, merchant_id: str) -> Optional[list[dict]]:
    """Get the most recent product status snapshot for comparison."""
    db = get_client()
    result = (
        db.table("merchant_product_status")
        .select("*")
        .eq("client_id", client_id)
        .eq("merchant_id", merchant_id)
        .order("snapshot_date", desc=True)
        .limit(500)
        .execute()
    )
    if not result.data:
        return None
    latest_date = result.data[0]["snapshot_date"]
    return [r for r in result.data if r["snapshot_date"] == latest_date]


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
