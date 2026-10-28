from datetime import UTC, datetime, timedelta

from autonoma_contracts.models import RecommendationStatus

from src.product_status import summarize_issues
from src.recommendations import process_recommendations
from tests.helpers import issue, product

NOW = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
ISSUES = summarize_issues([
    product(1, issues=[issue("landing_page_error", countries=("PL", "DE"))]),
    product(2, issues=[issue("landing_page_error"), issue("image_too_small")]),
])
ACCOUNT = [{"issue_id": "accounts/m1/issues/abc-123", "severity": "error", "title": "Shipping"}]


def raw(code, title="T", **kw):
    return {"type": "RECOMMENDATION", "title": title, "summary": "s", "impact": "HIGH",
            "confidence": 80, "issue_code": code, **kw}


def run(recs, now=NOW):
    return process_recommendations("c1", "m1", recs, now.date().isoformat(), ISSUES, ACCOUNT, now=now)


def recs(fake):
    return fake.rows("agent_recommendations")


def test_key_is_built_from_the_data_not_from_the_wording(fake_db):
    run([raw("landing_page_error", title="35 778 produktów z błędem")])
    run([raw("landing_page_error", title="Masowy błąd strony docelowej")], now=NOW + timedelta(days=7))
    rows = recs(fake_db)
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "merchant:landing_page_error:m1"
    assert rows[0]["subject_key"] == "issue:landing_page_error"
    assert rows[0]["occurrence_count"] == 2


def test_evidence_numbers_come_from_the_data(fake_db):
    run([raw("landing_page_error", evidence=[{"source": "llm", "data": {"products": 35778}}])])
    data = recs(fake_db)[0]["evidence"][0]["data"]
    assert (data["products"], data["occurrences"]) == (2, 2)
    assert data["by_context"] == {"SHOPPING_ADS": {"DE": 1, "PL": 2}}


def test_unknown_subject_is_rejected(fake_db):
    result = run([raw("landing_page_error_2026_10_27"), raw("topic:weather"), raw(None)])
    assert result.created == 0 and len(result.invalid) == 3
    assert recs(fake_db) == []


def test_account_issue_and_topic_subjects(fake_db):
    result = run([raw("account:abc-123"), raw("topic:performance", type="INSIGHT")])
    assert result.created == 2
    keys = {r["dedupe_key"]: r for r in recs(fake_db)}
    assert set(keys) == {"merchant:account_issue:m1:abc-123", "merchant:topic_performance:m1"}
    assert keys["merchant:topic_performance:m1"]["expires_at"] is not None


def test_one_recommendation_per_subject_per_run(fake_db):
    result = run([raw("image_too_small", title="a"), raw("image_too_small", title="b")])
    assert result.created == 1 and len(result.invalid) == 1


def test_ids_are_unique_and_not_positional(fake_db):
    run([raw("landing_page_error"), raw("image_too_small")])
    ids = [r["id"] for r in recs(fake_db)]
    assert len(set(ids)) == 2
    assert all(i.startswith("rec_merchant_") and not i.endswith(("_000", "_001")) for i in ids)


def test_same_day_rerun_is_not_another_occurrence(fake_db):
    run([raw("landing_page_error")])
    run([raw("landing_page_error")], now=NOW + timedelta(hours=2))
    assert recs(fake_db)[0]["occurrence_count"] == 1


def test_recently_rejected_is_skipped_and_old_rejection_reopens(fake_db):
    run([raw("image_too_small")])
    first = recs(fake_db)[0]
    first["status"] = RecommendationStatus.REJECTED.value
    first["updated_at"] = (NOW + timedelta(days=1)).isoformat()
    assert run([raw("image_too_small")], now=NOW + timedelta(days=7)).skipped == 1
    assert run([raw("image_too_small")], now=NOW + timedelta(days=40)).created == 1
    newest = recs(fake_db)[-1]
    assert newest["supersedes_recommendation_id"] == first["id"]


def test_existing_records_keep_their_status(fake_db):
    """Stage A never changes a recommendation's status; it only creates NEW ones."""
    run([raw("landing_page_error"), raw("image_too_small")])
    run([raw("landing_page_error")], now=NOW + timedelta(days=7))
    assert {r["status"] for r in recs(fake_db)} == {"NEW"}
    events = fake_db.rows("agent_recommendation_events")
    assert {(e["previous_status"], e["new_status"]) for e in events} == {("NEW", "NEW")}


def test_invalid_type_or_impact_is_rejected(fake_db):
    result = run([raw("image_too_small", type="TODO"), raw("landing_page_error", impact="HUGE")])
    assert result.created == 0 and len(result.invalid) == 2
