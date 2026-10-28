import json
from datetime import date

import pytest

from src import db, email, runtime
from src.collectors.account_issues import AccountIssuesResult
from src.collectors.performance import PerformanceResult
from src.collectors.product_health import ProductHealthResult
from src.jobs import weekly
from src.product_status import count_statuses, status_by_context, summarize_issues
from src.synthesis import SynthesisResult
from tests.helpers import DISAPPROVED, issue, product

CLIENT = weekly.CLIENTS[0].__class__(client_id="c1", merchant_id="m1", name="Test", email="ops@example.invalid")
RUN_DATE = date(2026, 10, 27)


def _health():
    products = [product(i, contexts=DISAPPROVED if i < 3 else None, issues=[issue("landing_page_error")] if i < 3 else [])
                for i in range(10)]
    for p in products:
        p.pop("client_id"), p.pop("snapshot_date")
    c = count_statuses(products)
    return ProductHealthResult(products=products, total=10, approved=c["approved"], disapproved=c["disapproved"],
                               pending=c["pending"], issues_by_code=summarize_issues(products),
                               status_by_context=status_by_context(products))


@pytest.fixture
def pipeline(monkeypatch, fake_db):
    state = {
        "perf": PerformanceResult(impressions=100, clicks=5, ctr=0.05, status="ok", rows=1),
        "issues": AccountIssuesResult(),
        "synthesis": SynthesisResult(status="ok", recommendations=[{
            "type": "RECOMMENDATION", "title": "Napraw strony", "impact": "HIGH", "issue_code": "landing_page_error"}],
            input_tokens=1000, output_tokens=200),
    }
    sent = []
    monkeypatch.setattr(weekly, "collect_product_health", lambda mid: _health())
    monkeypatch.setattr(weekly, "collect_account_issues", lambda mid: state["issues"])
    monkeypatch.setattr(weekly, "collect_performance", lambda mid, today: state["perf"])
    monkeypatch.setattr(weekly, "synthesize_recommendations", lambda **kw: state["synthesis"])
    monkeypatch.setattr(email.resend.Emails, "send", lambda params: sent.append(params))
    monkeypatch.setattr(weekly, "CLIENTS", [CLIENT])
    state["sent"] = sent
    return state


def test_success(pipeline, fake_db):
    out = weekly.process_client(CLIENT, "run1", RUN_DATE)
    assert out["status"] == "SUCCESS" and out["error_code"] is None
    assert out["llm_used"] and out["output_records"] == 1
    assert out["report"]["run_health"]["performance"] == "ok"
    assert out["report"]["issues_by_code"]["landing_page_error"]["products"] == 3


def test_synthesis_failure_makes_the_run_partial_and_is_reported(pipeline, fake_db):
    pipeline["synthesis"] = SynthesisResult(status="failed", error="answer is not valid JSON", input_tokens=900, output_tokens=50)
    weekly.run_weekly()
    run = fake_db.rows("agent_runs")[0]
    assert run["status"] == "PARTIAL"
    assert run["error_code"] == "SYNTHESIS_FAILED"
    assert run["llm_used"] is True and run["input_tokens"] == 900
    # Product data was still saved
    assert len(fake_db.rows("merchant_product_status")) == 10
    report = json.loads(fake_db.rows("merchant_reports")[0]["content"])
    assert report["run_health"]["synthesis"] == "failed"
    assert any("czesciowy" in m["subject"] for m in pipeline["sent"])


def test_performance_error_is_not_reported_as_missing_data(pipeline, fake_db):
    pipeline["perf"] = PerformanceResult(status="error", error="HTTP 400: invalid field name")
    out = weekly.process_client(CLIENT, "run1", RUN_DATE)
    assert out["status"] == "PARTIAL" and out["error_code"] == "PERFORMANCE_FAILED"
    assert fake_db.rows("merchant_metrics")[0]["impressions"] is None
    module = fake_db.rows("agent_dashboard_modules")[0]
    assert "performance" not in json.loads(module["sources"])


def test_account_issue_failure_is_not_silent(pipeline, fake_db):
    pipeline["issues"] = AccountIssuesResult(error="HTTP 403")
    out = weekly.process_client(CLIENT, "run1", RUN_DATE)
    assert out["status"] == "PARTIAL" and "ACCOUNT_ISSUES_FAILED" in out["error_code"]
    assert fake_db.rows("agent_dashboard_modules")[0]["status"] != "ok"


def test_product_collection_failure_fails_the_run(pipeline, fake_db, monkeypatch):
    monkeypatch.setattr(weekly, "collect_product_health", lambda mid: ProductHealthResult(error="HTTP 401"))
    assert weekly.run_weekly()
    assert fake_db.rows("agent_runs")[0]["status"] == "FAILED"


def test_rerun_same_day_keeps_one_snapshot_and_one_recommendation(pipeline, fake_db):
    weekly.process_client(CLIENT, "run1", RUN_DATE)
    weekly.process_client(CLIENT, "run2", RUN_DATE)
    assert len(fake_db.rows("merchant_product_status")) == 10
    assert len(fake_db.rows("merchant_metrics")) == 1
    assert len(fake_db.rows("merchant_reports")) == 1
    recs = fake_db.rows("agent_recommendations")
    assert len(recs) == 1 and recs[0]["occurrence_count"] == 1


def test_dry_run_writes_nothing_and_sends_nothing(pipeline, fake_db, tmp_path, monkeypatch):
    fake_db.seed("merchant_metrics", [{"client_id": "c1", "merchant_id": "m1", "metric_date": "2026-10-20", "total_products": 10}])
    fake_db.seed("merchant_product_status", [product(i, snapshot_date="2026-10-20") for i in range(10)])
    pipeline["synthesis"] = SynthesisResult(status="failed", error="x")  # forces a PARTIAL email too
    def sizes():
        return {name: len(rows) for name, rows in fake_db.tables.items() if rows}
    before = sizes()
    runtime.configure(dry_run_dir=str(tmp_path), llm_enabled=True)
    monkeypatch.setattr(db, "_client", runtime.ReadOnlyClient(fake_db))

    assert weekly.run_weekly() == []

    assert fake_db.writes == []
    assert sizes() == before
    assert pipeline["sent"] == []
    writes = [json.loads(line) for line in (tmp_path / "db_writes.jsonl").read_text().splitlines()]
    tables = {w["table"] for w in writes}
    assert {"agent_runs", "merchant_product_status", "merchant_reports", "agent_dashboard_modules"} <= tables
    emails = (tmp_path / "emails.jsonl").read_text()
    assert "czesciowy" in emails
    report = next(json.loads(w["rows"]["content"]) for w in writes if w["table"] == "merchant_reports")
    assert report["previous_snapshot"]["status"] == "ok"
    assert report["diff"]["comparable"] is True
    assert report["diff"]["new_disapprovals_count"] == 3


def test_read_only_client_blocks_writes(fake_db):
    guarded = runtime.ReadOnlyClient(fake_db)
    for op in ("insert", "upsert", "update", "delete"):
        with pytest.raises(runtime.DryRunWriteError):
            getattr(guarded.table("agent_runs"), op)
    with pytest.raises(runtime.DryRunWriteError):
        guarded.rpc
    assert guarded.table("agent_runs").select("*").execute().data == []


def test_top_issue_ranks_by_products_actually_disapproved():
    products = [product(i, contexts=[("SHOPPING_ADS", "PL", "APPROVED"), ("FREE_LOCAL_LISTINGS", "PL", "NOT_ELIGIBLE_OR_DISAPPROVED")],
                        issues=[issue("local_stores_lack_inventory", context="FREE_LOCAL_LISTINGS")]) for i in range(20)]
    products += [product(100 + i, contexts=DISAPPROVED, issues=[issue("landing_page_error")]) for i in range(6)]
    health = ProductHealthResult(products=products, disapproved=6, issues_by_code=summarize_issues(products))
    top = weekly._determine_top_issue(health, AccountIssuesResult())
    assert top["title"].startswith("6 products disapproved: landing_page_error")


def test_numbers_not_in_data_are_reported(pipeline, fake_db):
    pipeline["synthesis"] = SynthesisResult(status="ok", source_text="landing_page_error: 3 products", recommendations=[{
        "type": "RECOMMENDATION", "title": "3 produkty", "summary": "min. 800x800 px", "impact": "HIGH",
        "issue_code": "landing_page_error"}])
    out = weekly.process_client(CLIENT, "run1", RUN_DATE)
    assert out["status"] == "SUCCESS"
    assert out["report"]["recommendations"]["numbers_not_in_data"] == [
        {"issue_code": "landing_page_error", "title": "3 produkty", "numbers": [800]}]
