"""
Merchant Agent - Weekly job.
Railway cron entrypoint: python -m src.jobs.weekly
Full pipeline: collect -> normalize -> snapshot compare -> LLM synthesis -> save.
Process MUST exit after completion (Railway cron requirement).
"""

import argparse
import json
import html
import logging
import os
import sys
from datetime import UTC, date, datetime

from src import runtime

from src.email import send_report
from src.clients import CLIENTS
from src.config import config
from src.collectors.product_health import collect_product_health
from src.collectors.account_issues import collect_account_issues
from src.collectors.performance import collect_performance
from src.snapshot import compare_snapshots
from src.synthesis import synthesize_recommendations, unverified_numbers
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
    logger.info(
        f"Merchant Agent weekly run starting - {len(CLIENTS)} clients"
        + (" [DRY RUN - no database writes, no emails]" if runtime.is_dry_run() else "")
    )

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
            outcome = process_client(client, run_id, date.today())
            complete_run(
                run_id=run_id,
                status=outcome["status"],
                input_records=outcome["input_records"],
                output_records=outcome["output_records"],
                llm_used=outcome["llm_used"],
                input_tokens=outcome["input_tokens"],
                output_tokens=outcome["output_tokens"],
                error_code=outcome["error_code"],
                error_message=outcome["error_message"],
            )
            if outcome["status"] == "PARTIAL":
                _notify_partial(client, outcome["problems"])
            logger.info(f"Completed {client.name}: {outcome['status']} ({outcome['error_code'] or 'no problems'})")

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


def process_client(client, run_id: str, run_date: date) -> dict:
    """
    Collect, compare, save and synthesize for one client.
    Raises when product data cannot be collected (the run is FAILED). Any other
    source or step that fails is recorded as a problem and the run is PARTIAL:
    collecting the data and completing the whole run are reported separately.
    """
    today = run_date.isoformat()
    problems: list[tuple[str, str]] = []

    # Step 1: Collect product health
    health = collect_product_health(client.merchant_id)
    if health.error:
        raise Exception(f"Product health collection failed: {health.error}")
    for p in health.products:
        p["client_id"] = client.client_id
        p["snapshot_date"] = today

    # Step 2: Collect account issues
    issues = collect_account_issues(client.merchant_id)
    if issues.error:
        problems.append(("ACCOUNT_ISSUES_FAILED", issues.error))
    for iss in issues.issues:
        iss["client_id"] = client.client_id
        iss["snapshot_date"] = today

    # Step 3: Collect performance
    perf = collect_performance(client.merchant_id, today=run_date)
    if perf.status == "error":
        problems.append(("PERFORMANCE_FAILED", perf.error or "unknown error"))

    # Step 4: Snapshot compare (with the last complete snapshot before today)
    previous = get_previous_snapshot(client.client_id, client.merchant_id, before_date=today)
    if previous.status == "error":
        problems.append(("PREVIOUS_SNAPSHOT_FAILED", previous.error or "unknown error"))
    elif previous.status == "incomplete":
        problems.append(("PREVIOUS_SNAPSHOT_INCOMPLETE", json.dumps(previous.skipped_dates)))
    diff = compare_snapshots(health.products, previous)
    logger.info(f"Snapshot diff: {diff.summary} (significant: {diff.has_significant_change}, comparable: {diff.comparable})")

    # Step 5: Save to Supabase
    logger.info(f"Saving {len(health.products)} product statuses...")
    snapshot_save = save_product_status(health.products)
    if not issues.error:
        logger.info(f"Saving {len(issues.issues)} account issues...")
        save_account_issues(issues.issues)

    perf_ok = perf.status != "error"
    save_merchant_metrics({
        "client_id": client.client_id,
        "merchant_id": client.merchant_id,
        "metric_date": today,
        "total_products": health.total,
        "approved": health.approved,
        "disapproved": health.disapproved,
        "pending": health.pending,
        "impressions": perf.impressions if perf_ok else None,
        "clicks": perf.clicks if perf_ok else None,
        "ctr": perf.ctr if perf_ok else None,
    })

    account_summary = {
        "critical": issues.critical_count,
        "error": issues.error_count,
        "suggestion": issues.suggestion_count,
        "details": issues.issues,
        "collection_error": issues.error,
    }
    performance_summary = {
        "impressions": perf.impressions,
        "clicks": perf.clicks,
        "ctr": perf.ctr,
        "date_from": perf.date_from,
        "date_to": perf.date_to,
        "status": perf.status,
        "error": perf.error,
    }
    diff_summary = diff.to_dict()

    # Step 6: LLM synthesis (only if significant change)
    synthesis = None
    rec_result = None
    number_warnings: list[dict] = []
    if diff.has_significant_change:
        logger.info("Significant change detected - running LLM synthesis")

        cross_channel = {}
        ads_data = get_ads_data(client.client_id)
        if ads_data:
            cross_channel["ads"] = ads_data
        seo_data = get_seo_data(client.client_id)
        if seo_data:
            cross_channel["seo"] = seo_data

        synthesis = synthesize_recommendations(
            client_name=client.name,
            merchant_id=client.merchant_id,
            health_summary={
                "total": health.total,
                "approved": health.approved,
                "disapproved": health.disapproved,
                "pending": health.pending,
                "issues_by_code": health.issues_by_code,
            },
            account_issues={**account_summary, "error": issues.error},
            performance=performance_summary,
            diff=diff_summary,
            cross_channel=cross_channel if cross_channel else None,
        )
        if synthesis.status == "failed":
            problems.append(("SYNTHESIS_FAILED", synthesis.error or "unknown error"))
        elif synthesis.status == "ok":
            # Numbers the model wrote that are not in the data it was given (e.g. an
            # invented "800x800 px"). Reported for review; the recommendation is kept.
            for raw in synthesis.recommendations:
                numbers = unverified_numbers(raw, synthesis.source_text)
                if numbers:
                    number_warnings.append({"issue_code": raw.get("issue_code"), "title": raw.get("title"), "numbers": numbers})
                    logger.warning(f"Numbers not found in the data in {raw.get('title')!r}: {numbers}")
            rec_result = process_recommendations(
                client_id=client.client_id,
                merchant_id=client.merchant_id,
                raw_recommendations=synthesis.recommendations,
                snapshot_date=today,
                issues_by_code=health.issues_by_code,
                account_issues=issues.issues,
            )
            if rec_result.errors:
                problems.append(("RECOMMENDATIONS_FAILED", json.dumps(rec_result.errors, ensure_ascii=False)))
            logger.info(
                f"Recommendations: {rec_result.created} created, {rec_result.updated} updated, "
                f"{rec_result.skipped} skipped, {len(rec_result.invalid)} rejected, {len(rec_result.errors)} errors"
            )
        else:
            logger.info(f"LLM synthesis not run: {synthesis.error}")
    else:
        logger.info("No significant change - skipping LLM synthesis")

    # Step 7: Build and save report
    report_content = {
        "snapshot_date": today,
        "total_products": health.total,
        "approved": health.approved,
        "disapproved": health.disapproved,
        "pending": health.pending,
        "status_definition": "disapproved in at least one destination other than FREE_LOCAL_LISTINGS",
        "status_by_context": health.status_by_context,
        "issues_by_code": health.issues_by_code,
        "account_issues": account_summary,
        "performance": performance_summary,
        "previous_snapshot": {
            "status": previous.status,
            "date": previous.snapshot_date,
            "products": len(previous.products),
            "raw_rows": previous.raw_rows,
            "expected_products": previous.expected_products,
            "skipped_dates": previous.skipped_dates,
            "error": previous.error,
        },
        "diff": diff_summary,
        "run_health": {
            "products": "ok",
            "snapshot_save": snapshot_save,
            "account_issues": "error" if issues.error else "ok",
            "performance": perf.status,
            "previous_snapshot": previous.status,
            "synthesis": synthesis.status if synthesis else "not_needed",
            "synthesis_error": synthesis.error if synthesis else None,
            "problems": [{"code": c, "message": m[:500]} for c, m in problems],
        },
        "recommendations": {
            "created": rec_result.created,
            "updated": rec_result.updated,
            "skipped": rec_result.skipped,
            "rejected": [{"title": t, "reason": r} for t, r in rec_result.invalid],
            "errors": [{"title": t, "error": r} for t, r in rec_result.errors],
            "numbers_not_in_data": number_warnings,
            "action_saved_as_recommendation": rec_result.retyped,
        } if rec_result else None,
    }
    save_merchant_report({
        "client_id": client.client_id,
        "merchant_id": client.merchant_id,
        "report_date": today,
        "report_type": "weekly",
        "content": json.dumps(report_content),
        "run_id": run_id,
    })

    # Step 8: Upsert dashboard module
    now = datetime.now(UTC).isoformat()
    sources_data = {"products": {"last_successful_run": now, "stale_after_seconds": 1209600}}
    if perf_ok:
        sources_data["performance"] = {"last_successful_run": now, "stale_after_seconds": 1209600}
    if not issues.error:
        sources_data["account_issues"] = {"last_successful_run": now, "stale_after_seconds": 1209600}

    upsert_dashboard_module({
        "client_id": client.client_id,
        "module": "merchant",
        "status": _determine_status(health, issues),
        "summary": _build_summary(health, issues, diff),
        "data_period_from": today,
        "data_period_to": today,
        "last_successful_run": now,
        "sources": json.dumps(sources_data),
        "confidence": _calculate_confidence(health, perf, issues, diff),
        "metrics": json.dumps({
            "total_products": health.total,
            "approved": health.approved,
            "disapproved": health.disapproved,
            "pending": health.pending,
            "impressions": perf.impressions if perf_ok else None,
            "clicks": perf.clicks if perf_ok else None,
        }),
        "top_issue": json.dumps(t) if (t := _determine_top_issue(health, issues)) else None,
        "run_id": run_id,
    })

    return {
        "status": "PARTIAL" if problems else "SUCCESS",
        "problems": problems,
        "error_code": ",".join(c for c, _ in problems) or None,
        "error_message": ("; ".join(f"{c}: {m}" for c, m in problems))[:2000] or None,
        "input_records": health.total,
        "output_records": rec_result.created if rec_result else 0,
        "llm_used": bool(synthesis and synthesis.llm_called),
        "input_tokens": synthesis.input_tokens if synthesis else 0,
        "output_tokens": synthesis.output_tokens if synthesis else 0,
        "report": report_content,
    }


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


def _notify_partial(client, problems: list[tuple[str, str]]) -> None:
    """Data was saved but part of the run failed (a source, the synthesis or saving recommendations)."""
    items = "".join(
        f"<li><b>{html.escape(code)}</b>: {html.escape(message[:500])}</li>" for code, message in problems
    )
    send_report(
        to=client.email,
        subject=f"[Autonoma] Merchant Agent: przebieg czesciowy - {client.name}",
        html_body=(
            f"<p>Tygodniowy przebieg Merchant Agenta dla <b>{html.escape(client.name)}</b> "
            f"({html.escape(client.merchant_id)}) zapisal dane produktow, ale czesc krokow sie nie udala "
            f"(status PARTIAL w agent_runs):</p><ul>{items}</ul>"
        ),
    )


def _determine_status(health, issues) -> str:
    """Determine dashboard module status."""
    if issues.critical_count > 0:
        return "critical"
    if health.disapproved > 0 or issues.error_count > 0 or issues.error:
        return "warning"
    return "ok"


def _determine_top_issue(health, issues) -> dict | None:
    """Pick the single most important issue."""
    if issues.critical_count > 0:
        critical = [i for i in issues.issues if i["severity"] == "critical"]
        if critical:
            return {"title": critical[0]["title"], "impact": "CRITICAL"}

    if health.disapproved > 0 and health.issues_by_code:
        top_code = max(health.issues_by_code.values(), key=lambda x: (x["disapproving_products"], x["products"]))
        return {
            "title": f"{top_code['disapproving_products']} products disapproved: {top_code['description']}"[:200],
            "impact": "HIGH" if top_code["disapproving_products"] > 5 else "MEDIUM",
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
    if diff.comparable and diff.has_significant_change:
        parts.append(diff.summary)

    if not parts:
        return f"{health.total} products, all approved"

    return ". ".join(parts)[:200]


def _calculate_confidence(health, perf, issues, diff) -> int:
    """Calculate confidence score."""
    confidence = 90
    if health.total == 0:
        confidence -= 30
    if perf.status == "error":
        confidence -= 10
    if issues.error:
        confidence -= 10
    if not diff.comparable:
        confidence -= 10
    return max(10, confidence)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merchant Agent weekly run")
    parser.add_argument("--dry-run", metavar="DIR",
                        help="read production data but write nothing: database writes and emails go to DIR as JSON lines")
    parser.add_argument("--no-llm", action="store_true", help="do not call the LLM")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    runtime.configure(
        dry_run_dir=args.dry_run or os.getenv("MERCHANT_DRY_RUN_DIR") or None,
        llm_enabled=not args.no_llm and os.getenv("MERCHANT_LLM", "on") != "off",
    )
    # Non-zero exit when any client failed, so Railway shows the run as failed instead of green.
    sys.exit(1 if run_weekly() else 0)
