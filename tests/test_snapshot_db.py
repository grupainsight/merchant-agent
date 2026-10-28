from src import db
from tests.helpers import DISAPPROVED, product


def _seed_run(fake, snapshot_date, n, *, created_at="2026-01-01T07:00:00", total=None, disapproved_every=0):
    fake.seed("merchant_product_status", [
        product(i, snapshot_date=snapshot_date, created_at=created_at,
                contexts=DISAPPROVED if disapproved_every and i % disapproved_every == 0 else None)
        for i in range(n)
    ])
    fake.seed("merchant_metrics", [{
        "client_id": "c1", "merchant_id": "m1", "metric_date": snapshot_date,
        "total_products": total if total is not None else n, "created_at": created_at,
    }])


def test_reads_the_whole_catalog_past_row_caps(fake_db):
    fake_db.max_rows = 300  # PostgREST max-rows below our page size
    _seed_run(fake_db, "2026-10-20", 2500)
    prev = db.get_previous_snapshot("c1", "m1", before_date="2026-10-27")
    assert prev.status == "ok"
    assert prev.snapshot_date == "2026-10-20"
    assert len(prev.products) == 2500


def test_never_compares_with_a_same_day_run(fake_db):
    _seed_run(fake_db, "2026-10-20", 10)
    _seed_run(fake_db, "2026-10-27", 10)
    prev = db.get_previous_snapshot("c1", "m1", before_date="2026-10-27")
    assert prev.snapshot_date == "2026-10-20"


def test_same_day_duplicates_keep_newest_row_per_product(fake_db):
    _seed_run(fake_db, "2026-10-20", 50, created_at="2026-10-20T07:00:00")
    _seed_run(fake_db, "2026-10-20", 50, created_at="2026-10-20T09:00:00", disapproved_every=1)
    prev = db.get_previous_snapshot("c1", "m1", before_date="2026-10-27")
    assert prev.raw_rows == 100
    assert len(prev.products) == 50
    assert all(p["created_at"] == "2026-10-20T09:00:00" for p in prev.products)


def test_incomplete_snapshot_falls_back_to_last_complete_one(fake_db):
    _seed_run(fake_db, "2026-10-13", 40)
    _seed_run(fake_db, "2026-10-20", 25, total=40)  # run stopped while saving
    prev = db.get_previous_snapshot("c1", "m1", before_date="2026-10-27")
    assert prev.status == "ok"
    assert prev.snapshot_date == "2026-10-13"
    assert prev.skipped_dates == [{"date": "2026-10-20", "products": 25, "expected": 40}]


def test_no_complete_snapshot(fake_db):
    _seed_run(fake_db, "2026-10-20", 25, total=40)
    prev = db.get_previous_snapshot("c1", "m1", before_date="2026-10-27")
    assert prev.status == "incomplete"
    assert prev.products == []


def test_first_run(fake_db):
    assert db.get_previous_snapshot("c1", "m1", before_date="2026-10-27").status == "none"


def test_read_error_is_reported_not_raised(fake_db, monkeypatch):
    _seed_run(fake_db, "2026-10-20", 10)
    monkeypatch.setattr(db, "_fetch_all", lambda q: (_ for _ in ()).throw(RuntimeError("boom")))
    prev = db.get_previous_snapshot("c1", "m1", before_date="2026-10-27")
    assert prev.status == "error"
    assert "boom" in prev.error


def test_rerun_does_not_insert_the_snapshot_twice(fake_db):
    records = [product(i, snapshot_date="2026-10-27") for i in range(30)]
    assert db.save_product_status([dict(r) for r in records]) == "saved"
    assert db.save_product_status([dict(r) for r in records]) == "already_saved"
    assert len(fake_db.rows("merchant_product_status")) == 30


def test_partial_earlier_save_is_completed(fake_db):
    records = [product(i, snapshot_date="2026-10-27") for i in range(30)]
    db.save_product_status([dict(r) for r in records[:10]])
    assert db.save_product_status([dict(r) for r in records]) == "saved"
    ids = {r["product_id"] for r in fake_db.rows("merchant_product_status")}
    assert len(ids) == 30


def test_rerun_updates_metrics_and_report_instead_of_duplicating(fake_db):
    base = {"client_id": "c1", "merchant_id": "m1"}
    db.save_merchant_metrics({**base, "metric_date": "2026-10-27", "total_products": 1})
    db.save_merchant_metrics({**base, "metric_date": "2026-10-27", "total_products": 2})
    db.save_merchant_report({**base, "report_date": "2026-10-27", "content": "a"})
    db.save_merchant_report({**base, "report_date": "2026-10-27", "content": "b"})
    issues = [{**base, "snapshot_date": "2026-10-27", "title": "t"}]
    db.save_account_issues(issues)
    db.save_account_issues(issues)
    assert [m["total_products"] for m in fake_db.rows("merchant_metrics")] == [2]
    assert [r["content"] for r in fake_db.rows("merchant_reports")] == ["b"]
    assert len(fake_db.rows("merchant_account_issues")) == 1
