"""
Merchant Agent - Weekly job.
Railway cron entrypoint: python -m src.jobs.weekly
Full pipeline: collect -> normalize -> snapshot compare -> LLM synthesis -> save.
Process MUST exit after completion (Railway cron requirement).
"""

import json
import html
import logging
import sys
from datetime import UTC, date, datetime

from src.email import send_report
from src.clients import CLIENTS
from src.config import config
from src.collectors.product_health import collect_product_health
from src.collectors.account_issues import collect_account_issues
from src.collectors.performance import collect_performance
from src.snapshot import compare_snapshots
from src.synthesis import synthesize_recommendations
from src.recommendations import process_recommendations
from src.db import (
    create_run,
    complete_run,
    save_product_status,
    save_merchant_metrics,
    save_account_issues,
    save_merchant_report,
    upsert_dashboard_module,
    get_previous_snapshot,
    get_ads_data,
    get_seo_data,
)

logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("merchant-agent")


def run_weekly() -> list:
    """Execute weekly pipeline for all clients. Returns the clients that failed (client, error)."""
    logger.info(f"Merchant Agent weekly run starting - {len(CLIENTS)} clients")

    if not CLIENTS:
        logger.warning("No clients configured. Exiting.")
        return []

    failures = []
    for client in CLIENTS:
        run_record = None
        try:
            run_record = create_run(
                client_id=client.client_id,
                job_type="weekly",
                scheduled_for=datetime.now(UTC).isoformat(),
            )
            run_id = run_record["id"]
            logger.info(f"Processing {client.name} ({client.merchant_id})")

            # Step 1: Collect product health
            health = collect_product_health(client.merchant_id)
            if health.error:
                raise Exception(f"Product health collection failed: {health.error}")

            # Set client_id on all records
            for p in health.products:
                p["client_id"] = client.client_id

            # Step 2: Collect account issues
            issues = collect_account_issues(client.merchant_id)
            for iss in issues.issues:
                iss["client_id"] = client.client_id

            # Step 3: Collect performance (graceful - may not be available)
            perf = collect_performance(client.merchant_id)

            # Step 4: Snapshot compare
            previous = get_previous_snapshot(client.client_id, client.merchant_id)
            diff = compare_snapshots(health.products, previous)
            logger.info(f"Snapshot diff: {diff.summary} (significant: {diff.has_significant_change})")

            # Step 5: Save to Supabase
            logger.info(f"Saving {len(health.products)} product statuses...")
            save_product_status(health.products)
            logger.info(f"Saving {len(issues.issues)} account issues...")
            save_account_issues(issues.issues)

            today = date.today().isoformat()
            save_merchant_metrics({
                "client_id": client.client_id,
                "merchant_id": client.merchant_id,
                "metric_date": today,
                "total_products": health.total,
                "approved": health.approved,
                "disapproved": health.disapproved,
                "pending": health.pending,
                "impressions": perf.impressions if not perf.error else None,
                "clicks": perf.clicks if not perf.error else None,
                "ctr": perf.ctr if not perf.error else None,
            })

            # Step 6: Build and save report
            report_content = {
                "total_products": health.total,
                "approved": health.approved,
                "disapproved": health.disapproved,
                "pending": health.pending,
                "issues_by_code": health.issues_by_code,
                "account_issues": {
                    "critical": issues.critical_count,
                    "error": issues.error_count,
                    "suggestion": issues.suggestion_count,
                    "details": issues.issues,
                },
                "performance": {
                    "impressions": perf.impressions,
                    "clicks": perf.clicks,
                    "ctr": perf.ctr,
                    "error": perf.error,
                },
                "diff": {
                    "has_significant_change": diff.has_significant_change,
                    "summary": diff.summary,
                    "new_disapprovals_count": len(diff.new_disapprovals),
                    "resolved_disapprovals_count": len(diff.resolved_disapprovals),
                    "disapproved_delta": diff.disapproved_delta,
                },
            }

            save_merchant_report({
                "client_id": client.client_id,
                "merchant_id": client.merchant_id,
                "report_date": today,
                "report_type": "weekly",
                "content": json.dumps(report_content),
                "run_id": run_id,
            })

            # Step 7: LLM synthesis (only if significant change)
            llm_used = False
            input_tokens = 0
            output_tokens = 0
            recs_created = 0

            if diff.has_significant_change:
                logger.info("Significant change detected - running LLM synthesis")

                # Read cross-channel data
                cross_channel = {}
                ads_data = get_ads_data(client.client_id)
                if ads_data:
                    cross_channel["ads"] = ads_data
                seo_data = get_seo_data(client.client_id)
                if seo_data:
                    cross_channel["seo"] = seo_data

                raw_recs = synthesize_recommendations(
                    client_name=client.name,
                    merchant_id=client.merchant_id,
                    health_summary={
                        "total": health.total,
                        "approved": health.approved,
                        "disapproved": health.disapproved,
                        "pending": health.pending,
                        "issues_by_code": health.issues_by_code,
                    },
                    account_issues={
                        "critical": issues.critical_count,
                        "error": issues.error_count,
                        "suggestion": issues.suggestion_count,
                        "details": issues.issues,
                    },
                    performance={
                        "impressions": perf.impressions,
                        "clicks": perf.clicks,
                        "ctr": perf.ctr,
                        "error": perf.error,
                    },
                    diff={
                        "has_significant_change": diff.has_significant_change,
                        "summary": diff.summary,
                        "new_disapprovals_count": len(diff.new_disapprovals),
                        "resolved_disapprovals_count": len(diff.resolved_disapprovals),
                        "disapproved_delta": diff.disapproved_delta,
                    },
                    cross_channel=cross_channel if cross_channel else None,
                )

                if raw_recs:
                    # Extract token usage from first rec
                    if raw_recs[0].get("_input_tokens"):
                        input_tokens = raw_recs[0].pop("_input_tokens", 0)
                        output_tokens = raw_recs[0].pop("_output_tokens", 0)
                        llm_used = True

                    created, updated, skipped = process_recommendations(
                        client_id=client.client_id,
                        merchant_id=client.merchant_id,
                        raw_recommendations=raw_recs,
                    )
                    recs_created = created
                    logger.info(f"Recommendations: {created} created, {updated} updated, {skipped} skipped")
            else:
                logger.info("No significant change - skipping LLM synthesis")

            # Step 8: Upsert dashboard module
            module_status = _determine_status(health, issues)
            top_issue = _determine_top_issue(health, issues)
            sources_data = {
                "products": {
                    "last_successful_run": datetime.now(UTC).isoformat(),
                    "stale_after_seconds": 1209600,
                },
            }
            if not perf.error:
                sources_data["performance"] = {
                    "last_successful_run": datetime.now(UTC).isoformat(),
                    "stale_after_seconds": 1209600,
                }

            upsert_dashboard_module({
                "client_id": client.client_id,
                "module": "merchant",
                "status": module_status,
                "summary": _build_summary(health, issues, diff),
                "data_period_from": today,
                "data_period_to": today,
                "last_successful_run": datetime.now(UTC).isoformat(),
                "sources": json.dumps(sources_data),
                "confidence": _calculate_confidence(health, perf),
                "metrics": json.dumps({
                    "total_products": health.total,
                    "approved": health.approved,
                    "disapproved": health.disapproved,
                    "pending": health.pending,
                    "impressions": perf.impressions if not perf.error else None,
                    "clicks": perf.clicks if not perf.error else None,
                }),
                "top_issue": json.dumps(top_issue) if top_issue else None,
                "run_id": run_id,
            })

            complete_run(
                run_id=run_id,
                status="SUCCESS",
                input_records=health.total,
                output_records=recs_created,
                llm_used=llm_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            logger.info(f"Completed {client.name}: {health.total} products, {module_status}, {recs_created} recommendations")

        except Exception as e:
            logger.error(f"Failed processing {client.name}: {e}", exc_info=True)
            failures.append((client, e))
            if run_record:
                complete_run(
                    run_id=run_record["id"],
                    status="FAILED",
                    error_code=type(e).__name__,
                    error_message=str(e)[:2000],
                )
            _notify_failure(client, e)

    logger.info(f"Merchant Agent weekly run complete ({len(failures)} failed of {len(CLIENTS)})")
    return failures


def _notify_failure(client, error: Exception) -> None:
    """Failures used to be recorded only in agent_runs while the process exited 0, so a
    revoked OAuth token went unnoticed for weeks. Tell the operator right away."""
    hint = ""
    if type(error).__name__ == "RefreshError" or "invalid_grant" in str(error):
        hint = (
            "<p>Token Google OAuth wygasl lub zostal cofniety (invalid_grant). Jesli ekran zgody OAuth "
            "w Google Cloud ma status <b>Testing</b>, tokeny odswiezania wygasaja po 7 dniach - opublikuj "
            "aplikacje (Production), wygeneruj nowy refresh token dla zakresu "
            "<code>https://www.googleapis.com/auth/content</code> i zaktualizuj zmienna "
            "<code>GOOGLE_REFRESH_TOKEN</code> w Railway.</p>"
        )
    send_report(
        to=client.email,
        subject=f"[Autonoma] Merchant Agent: przebieg nieudany - {client.name}",
        html_body=(
            f"<p>Tygodniowy przebieg Merchant Agenta dla <b>{html.escape(client.name)}</b> "
            f"({html.escape(client.merchant_id)}) zakonczyl sie bledem i nie zebral danych.</p>"
            f"<p><b>{html.escape(type(error).__name__)}</b>: {html.escape(str(error)[:500])}</p>{hint}"
        ),
    )


def _determine_status(health, issues) -> str:
    """Determine dashboard module status."""
    if issues.critical_count > 0:
        return "critical"
    if health.disapproved > 0 or issues.error_count > 0:
        return "warning"
    return "ok"


def _determine_top_issue(health, issues) -> dict | None:
    """Pick the single most important issue."""
    if issues.critical_count > 0:
        critical = [i for i in issues.issues if i["severity"] == "critical"]
        if critical:
            return {"title": critical[0]["title"], "impact": "CRITICAL"}

    if health.disapproved > 0 and health.issues_by_code:
        top_code = max(health.issues_by_code.values(), key=lambda x: x["count"])
        return {
            "title": f"{top_code['count']} products: {top_code['description']}",
            "impact": "HIGH" if top_code["count"] > 5 else "MEDIUM",
        }

    if issues.error_count > 0:
        errors = [i for i in issues.issues if i["severity"] == "error"]
        if errors:
            return {"title": errors[0]["title"], "impact": "MEDIUM"}

    return None


def _build_summary(health, issues, diff) -> str:
    """Build human-readable summary."""
    parts = []

    if health.disapproved > 0:
        parts.append(f"{health.disapproved} products disapproved")
    if issues.critical_count > 0:
        parts.append(f"{issues.critical_count} critical account issues")
    if diff.has_significant_change:
        parts.append(diff.summary)

    if not parts:
        return f"{health.total} products, all approved"

    return ". ".join(parts)[:200]


def _calculate_confidence(health, perf) -> int:
    """Calculate confidence score."""
    confidence = 90
    if health.total == 0:
        confidence -= 30
    if perf.error:
        confidence -= 10
    return max(10, confidence)


if __name__ == "__main__":
    # Non-zero exit when any client failed, so Railway shows the run as failed instead of green.
    sys.exit(1 if run_weekly() else 0)
