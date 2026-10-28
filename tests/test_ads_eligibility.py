import json
from datetime import date

from src import ads_eligibility, performance_store
from src.fix_paths import fix_path
from src.product_status import summarize_issues
from src.recommendations import resolve_subject
from tests.helpers import issue, product

MID = "107773243"


def _p(pid, status=None, codes=(), url=True):
    contexts = [("SHOPPING_ADS", "PL", status)] if status else [("FREE_LISTINGS", "PL", "APPROVED")]
    row = product(pid, contexts=contexts, issues=[issue(c, context="SHOPPING_ADS") for c in codes])
    if url:
        row["product_url"] = f"https://x.pl/produkt/{pid}/"
    return row


CATALOG = [
    _p(1, "APPROVED"),
    _p(2, "NOT_ELIGIBLE_OR_DISAPPROVED", ["landing_page_error"]),
    _p(3, "NOT_ELIGIBLE_OR_DISAPPROVED", ["image_unwanted_overlays", "landing_page_error"]),
    _p(4, "PENDING"),
    _p(5),                                   # no SHOPPING_ADS status at all
]


def test_counts_and_top_codes_carry_the_recommendation_key_and_fix_path():
    r = ads_eligibility.build(CATALOG, MID, "2026-10-28", None, [])
    assert (r["approved"], r["disapproved"], r["pending"], r["no_status"]) == (1, 2, 1, 1)
    assert r["top_disapproval_codes"][0] == {"issue_code": "landing_page_error", "products": 2,
                                             "fix_path": "website", "dedupe_key": f"merchant:landing_page_error:{MID}"}
    assert r["lost_paid_visibility"] is None and "lost_paid_visibility_unavailable" in r


def test_lost_paid_visibility_lists_products_with_their_identifiers():
    paid = {"2": 7, "3": 2, "1": 50}         # product 1 has clicks but is approved: not lost
    r = ads_eligibility.build(CATALOG, MID, "2026-10-28", paid, ["2026-10-19", "2026-10-12"])
    lost = r["lost_paid_visibility"]
    assert (lost["products"], lost["paid_clicks"], lost["weeks"]) == (2, 9, ["2026-10-19", "2026-10-12"])
    first = lost["top"][0]
    assert first == {"product_id": "accounts/m1/products/2", "offer_id": "2",
                     "current_product_url": "https://x.pl/produkt/2/", "paid_clicks": 7,
                     "issue_codes": ["landing_page_error"], "fix_paths": ["website"],
                     "dedupe_keys": [f"merchant:landing_page_error:{MID}"]}
    assert lost["top"][1]["fix_paths"] == ["feed_source", "website"]


def test_paid_weeks_are_the_last_four_complete_weeks():
    assert ads_eligibility.paid_weeks(date(2026, 10, 19)) == [
        date(2026, 10, 19), date(2026, 10, 12), date(2026, 10, 5), date(2026, 9, 28)]


def test_paid_clicks_come_only_from_published_weeks(fake_db):
    fake_db.seed("merchant_performance_periods", [
        {"client_key": "k", "merchant_id": MID, "period_start": "2026-10-19", "fetch_id": "a"}])
    fake_db.seed("merchant_product_performance_published", [
        {"client_key": "k", "merchant_id": MID, "period_start": "2026-10-19", "offer_id": "2", "marketing_method": "ADS", "clicks": 3},
        {"client_key": "k", "merchant_id": MID, "period_start": "2026-10-19", "offer_id": "2", "marketing_method": "ORGANIC", "clicks": 9},
        {"client_key": "k", "merchant_id": MID, "period_start": "2026-10-12", "offer_id": "2", "marketing_method": "ADS", "clicks": 4},
    ])
    clicks, weeks = performance_store.paid_clicks_by_offer("k", MID, [date(2026, 10, 19), date(2026, 10, 12)])
    assert clicks == {"2": 3} and weeks == ["2026-10-19"]     # 2026-10-12 is not published


def test_fix_paths_are_classified():
    assert fix_path("landing_page_error") == "website"
    assert fix_path("item_missing_required_attribute") == "feed_source"
    assert fix_path("personal_hardships_policy_violation") == "policy_review"
    assert fix_path("local_stores_lack_inventory") == "merchant_center_settings"
    assert fix_path("something_new") == "unclassified"
    assert summarize_issues([_p(2, "NOT_ELIGIBLE_OR_DISAPPROVED", ["landing_page_error"])])["landing_page_error"]["fix_path"] == "website"


def test_recommendation_evidence_links_issue_to_its_products():
    issues = summarize_issues(CATALOG)
    subject = resolve_subject({"issue_code": "landing_page_error"}, MID, "2026-10-28", issues, [])
    data = subject.evidence[0]["data"]
    assert data["fix_path"] == "website"
    assert data["products_ref"] == {"table": "merchant_product_status", "merchant_id": MID,
                                    "snapshot_date": "2026-10-28", "issue_code": "landing_page_error"}
