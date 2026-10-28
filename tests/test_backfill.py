import json
from datetime import date, timedelta

from src import performance_job, performance_store, runtime, db
from src.collectors.product_performance import WeekLoad
from src.jobs import backfill_performance
from tests.helpers import product


def _load(mid, start):
    load = WeekLoad(period_start=start, period_end=start + timedelta(days=6))
    load.offers = {("1", "ADS"): [5, 1], ("x", "ORGANIC"): [3, 0]}
    load.totals = {"ADS": [8, 1], "ORGANIC": [9, 0]}
    return load


def _seed_catalog(fake):
    fake.seed("merchant_metrics", [{"client_id": "client_piccolo_tesoro", "merchant_id": "107773243",
                                    "metric_date": "2026-10-27", "total_products": 3}])
    rows = [product(i, snapshot_date="2026-10-27", client_id="client_piccolo_tesoro") for i in range(3)]
    for r in rows:
        r["merchant_id"] = "107773243"
        r["product_url"] = f"https://piccolotesoro.pl/produkt/{r['offer_id']}/"
    fake.seed("merchant_product_status", rows)


def test_backfill_loads_history_with_current_urls_and_skips_published(fake_db, monkeypatch):
    fake_db.rpc_results["resolve_client_key"] = "piccolotesoro"
    performance_store._dry_published.clear()
    _seed_catalog(fake_db)
    monkeypatch.setattr(performance_job, "fetch_week", _load)
    assert backfill_performance.backfill(weeks=5, reload=False, run_date=date(2026, 10, 28)) == []
    assert len(fake_db.rows("merchant_performance_periods")) == 5
    row = next(r for r in fake_db.rows("merchant_product_performance") if r["offer_id"] == "1")
    assert (row["current_product_url"], row["in_current_catalog"]) == ("https://piccolotesoro.pl/produkt/1/", True)
    assert fake_db.rows("merchant_performance_periods")[0]["catalog_snapshot_date"] == "2026-10-27"
    run = fake_db.rows("agent_runs")[0]
    assert (run["job_type"], run["status"]) == ("on_demand", "SUCCESS")

    rows_before = len(fake_db.rows("merchant_product_performance"))
    backfill_performance.backfill(weeks=5, reload=False, run_date=date(2026, 10, 28))
    assert len(fake_db.rows("merchant_product_performance")) == rows_before    # published weeks skipped


def test_backfill_dry_run_writes_nothing(fake_db, monkeypatch, tmp_path):
    fake_db.rpc_results["resolve_client_key"] = "piccolotesoro"
    performance_store._dry_published.clear()
    _seed_catalog(fake_db)
    monkeypatch.setattr(performance_job, "fetch_week", _load)
    runtime.configure(dry_run_dir=str(tmp_path), llm_enabled=False)
    monkeypatch.setattr(db, "_client", runtime.ReadOnlyClient(fake_db))
    backfill_performance.backfill(weeks=6, reload=False, run_date=date(2026, 10, 28))
    assert fake_db.writes == []
    report = json.loads((tmp_path / "backfill_report.jsonl").read_text())
    assert len(report["published"]) == 6 and report["summary"]["data_status"] == "complete"
