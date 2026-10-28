from src.product_status import classify, count_statuses, status_by_context, summarize_issues
from src.snapshot import PreviousSnapshot, compare_snapshots
from tests.helpers import DISAPPROVED, issue, product


def test_free_local_listings_alone_does_not_disapprove():
    p = product(1, contexts=[("SHOPPING_ADS", "PL", "APPROVED"), ("FREE_LOCAL_LISTINGS", "PL", "NOT_ELIGIBLE_OR_DISAPPROVED")])
    assert classify(p) == "approved"


def test_report_and_diff_use_the_same_definition():
    # Disapproved only in FREE_LOCAL_LISTINGS: neither the report nor the diff may count it.
    fll_only = product(1, contexts=[("SHOPPING_ADS", "PL", "APPROVED"), ("FREE_LOCAL_LISTINGS", "PL", "NOT_ELIGIBLE_OR_DISAPPROVED")])
    previous = PreviousSnapshot(status="ok", snapshot_date="2026-10-20", products=[product(1)])
    diff = compare_snapshots([fll_only], previous)
    assert count_statuses([fll_only])["disapproved"] == 0
    assert diff.new_disapprovals == []
    assert diff.disapproved_delta == 0


def test_issue_counts_products_not_occurrences():
    # Product 1 reports the code in two destinations (two entries, two countries each), product 2 once.
    p1 = product(1, issues=[
        issue("landing_page_error", context="SHOPPING_ADS", countries=("PL", "DE")),
        issue("landing_page_error", context="FREE_LISTINGS", countries=("PL", "DE"), severity="NOT_IMPACTED"),
    ])
    p2 = product(2, issues=[issue("landing_page_error", context="FREE_LISTINGS", countries=("PL",), severity="DEMOTED")])
    summary = summarize_issues([p1, p2])["landing_page_error"]
    assert summary["products"] == 2
    assert summary["count"] == 2
    assert summary["occurrences"] == 3
    assert summary["disapproving_products"] == 1
    assert summary["severity"] == "DISAPPROVED"
    assert summary["by_context"] == {"FREE_LISTINGS": {"DE": 1, "PL": 2}, "SHOPPING_ADS": {"DE": 1, "PL": 1}}
    assert summary["sample_offer_ids"] == ["1", "2"]


def test_status_by_context_keeps_destination_and_market():
    p1 = product(1, contexts=[("SHOPPING_ADS", "PL", "APPROVED"), ("SHOPPING_ADS", "DE", "NOT_ELIGIBLE_OR_DISAPPROVED")])
    p2 = product(2, contexts=[("SHOPPING_ADS", "PL", "PENDING")])
    rows = {(r["reporting_context"], r["country_code"]): r for r in status_by_context([p1, p2])}
    assert rows[("SHOPPING_ADS", "PL")] == {"reporting_context": "SHOPPING_ADS", "country_code": "PL", "approved": 1, "disapproved": 0, "pending": 1}
    assert rows[("SHOPPING_ADS", "DE")]["disapproved"] == 1


def test_identical_snapshots_report_no_change():
    products = [product(i, contexts=DISAPPROVED if i % 3 == 0 else None, issues=[issue("x")] if i % 2 else []) for i in range(100)]
    diff = compare_snapshots(products, PreviousSnapshot(status="ok", snapshot_date="2026-10-20", products=products))
    assert diff.comparable
    assert not diff.has_significant_change
    assert diff.new_disapprovals == [] and diff.new_issues == [] and diff.issue_changes == []


def test_issue_changes_are_per_code_in_products():
    before = [product(1, issues=[issue("a")]), product(2, issues=[issue("a")])]
    after = [product(1, issues=[issue("a")]), product(2, issues=[issue("b")])]
    diff = compare_snapshots(after, PreviousSnapshot(status="ok", snapshot_date="d", products=before))
    assert [i["code"] for i in diff.new_issues] == ["b"]
    assert {(c["code"], c["before"], c["after"]) for c in diff.issue_changes} == {("a", 2, 1), ("b", 0, 1)}


def test_not_comparable_without_complete_previous_snapshot():
    for status in ("none", "incomplete", "error"):
        diff = compare_snapshots([product(1, contexts=DISAPPROVED)], PreviousSnapshot(status=status))
        assert not diff.comparable
        assert diff.new_disapprovals == []
        assert diff.to_dict()["previous_status"] == status


def test_issue_only_in_free_local_listings_disapproves_nothing():
    p = product(1, contexts=[("SHOPPING_ADS", "PL", "APPROVED"), ("FREE_LOCAL_LISTINGS", "PL", "NOT_ELIGIBLE_OR_DISAPPROVED")],
                issues=[issue("local_stores_lack_inventory", context="FREE_LOCAL_LISTINGS")])
    summary = summarize_issues([p])["local_stores_lack_inventory"]
    assert summary["products"] == 1
    assert summary["disapproving_products"] == 0
