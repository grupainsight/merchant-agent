import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from src import db, performance_job, performance_store, runtime, synthesis
from src.collectors import product_performance as collector
from src.collectors.product_performance import WeekFetchError, WeekLoad
from src.performance_summary import prompt_lines, summarize

W = date(2026, 10, 19)          # a Monday
NOW = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
KEY, MID = "piccolotesoro", "107773243"


def week(offers, totals=None, start=W):
    load = WeekLoad(period_start=start, period_end=start + timedelta(days=6))
    load.offers = {k: list(v) for k, v in offers.items()}
    load.totals = totals or {"ADS": [100, 10], "ORGANIC": [200, 2]}
    return load


def visible(fake, start=W, client_key=KEY):
    """What a reader of merchant_product_performance_published sees."""
    period = next((p for p in fake.rows("merchant_performance_periods")
                   if p["client_key"] == client_key and p["period_start"] == start.isoformat()), None)
    if not period:
        return {}
    return {(r["offer_id"], r["marketing_method"]): (r["impressions"], r["clicks"])
            for r in fake.rows("merchant_product_performance")
            if r["fetch_id"] == period["fetch_id"] and r["period_start"] == start.isoformat()}


@pytest.fixture
def store(fake_db):
    fake_db.rpc_results["resolve_client_key"] = lambda p: KEY if p == {"p_kind": "merchant_id", "p_value": MID} else None
    performance_store._dry_published.clear()
    return fake_db


def publish(load, urls=None, now=NOW):
    return performance_store.publish_week(KEY, MID, load, urls or {"a": "https://x.pl/a/"}, "2026-10-27", "run1", now=now)


# ---------------------------------------------------------------- collector

@pytest.fixture
def api(monkeypatch):
    calls, state = [], {}

    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        return state["handler"](body)

    real = httpx.Client
    monkeypatch.setattr(collector.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setattr(collector, "get_credentials", lambda: SimpleNamespace(token="t"))
    return state, calls


def _v(offer=None, method="ADS", i=0, c=0):
    row = {"marketingMethod": method, "impressions": str(i), "clicks": str(c)}
    if offer is not None:
        row["offerId"] = offer
    return {"productPerformanceView": row}


def test_queries_are_v1_and_have_no_per_offer_calls():
    q1, q2 = collector.offer_query(W, W + timedelta(days=6)), collector.totals_query(W, W + timedelta(days=6))
    assert q1 == ("SELECT offer_id, marketing_method, impressions, clicks FROM product_performance_view "
                  "WHERE date BETWEEN '2026-10-19' AND '2026-10-25'")
    assert q2.startswith("SELECT marketing_method, impressions, clicks FROM product_performance_view")
    assert "offer_id IN" not in q1 + q2


def test_fetch_week_pages_and_counts_odd_rows(api):
    state, calls = api

    def handler(body):
        if body["query"].startswith("SELECT offer_id"):
            if "pageToken" not in body:
                return httpx.Response(200, json={"results": [_v("a", "ADS", 10, 1), _v("", "ADS", 5, 0)], "nextPageToken": "p2"})
            return httpx.Response(200, json={"results": [_v("a", "ORGANIC", 20, 0), _v("b", "LOCAL", 3, 0), _v("a", "ADS", 1, 0)]})
        return httpx.Response(200, json={"results": [_v(None, "ADS", 40, 1), _v(None, "ORGANIC", 30, 0)]})

    state["handler"] = handler
    load = collector.fetch_week(MID, W)
    assert load.offers == {("a", "ADS"): [11, 1], ("a", "ORGANIC"): [20, 0]}
    assert load.totals == {"ADS": [40, 1], "ORGANIC": [30, 0]}
    assert (load.rows_without_offer, load.rows_unknown_method, load.api_calls) == (1, 1, 3)
    assert all(c["pageSize"] == 5000 for c in calls)


def test_any_http_error_fails_the_whole_week(api):
    state, _ = api
    state["handler"] = lambda body: (httpx.Response(500, json={"error": {"message": "backend"}})
                                     if body["query"].startswith("SELECT marketing_method") else httpx.Response(200, json={}))
    with pytest.raises(WeekFetchError, match="HTTP 500"):
        collector.fetch_week(MID, W)


def test_week_must_start_on_monday():
    with pytest.raises(ValueError):
        collector.fetch_week(MID, W + timedelta(days=1))


def test_weeks_back():
    assert performance_job.weeks_back(date(2026, 11, 3), 2) == [date(2026, 10, 26), date(2026, 10, 19)]
    assert performance_job.weeks_back(date(2027, 1, 5), 1) == [date(2026, 12, 28)]


# ---------------------------------------------------------------- publication

def test_publish_writes_rows_totals_and_current_urls(store):
    result = publish(week({("a", "ADS"): (10, 1), ("a", "ORGANIC"): (20, 0), ("gone", "ADS"): (5, 0)}))
    assert result.status == "published" and result.replaced_fetch_id is None
    assert visible(store) == {("a", "ADS"): (10, 1), ("a", "ORGANIC"): (20, 0), ("gone", "ADS"): (5, 0)}
    rows = {r["offer_id"]: r for r in store.rows("merchant_product_performance")}
    assert (rows["a"]["current_product_url"], rows["a"]["in_current_catalog"]) == ("https://x.pl/a/", True)
    assert (rows["gone"]["current_product_url"], rows["gone"]["in_current_catalog"]) == (None, False)
    period = store.rows("merchant_performance_periods")[0]
    assert (period["ads_impressions"], period["ads_impressions_by_offer"], period["ads_clicks_by_offer"]) == (100, 15, 1)
    assert (period["organic_impressions_by_offer"], period["offers"], period["offer_rows"]) == (20, 2, 3)
    assert period["catalog_snapshot_date"] == "2026-10-27"


def test_reload_replaces_the_week_and_removes_vanished_offers(store):
    first = publish(week({("a", "ADS"): (10, 1), ("b", "ADS"): (5, 0)}))
    second = publish(week({("a", "ADS"): (12, 1)}))
    assert second.replaced_fetch_id == first.fetch_id
    assert visible(store) == {("a", "ADS"): (12, 1)}
    assert {r["fetch_id"] for r in store.rows("merchant_product_performance")} == {second.fetch_id}


def test_write_failure_never_publishes_a_partial_week(store, monkeypatch):
    publish(week({("a", "ADS"): (10, 1)}))
    before = visible(store)
    monkeypatch.setattr(performance_store, "BATCH_SIZE", 1)
    store.fail_inserts["merchant_product_performance"] = 3      # first load used call 1; fail the 2nd batch of the reload
    with pytest.raises(RuntimeError, match="simulated"):
        publish(week({("a", "ADS"): (99, 9), ("b", "ADS"): (1, 0), ("c", "ADS"): (1, 0)}))
    assert visible(store) == before
    assert len(store.rows("merchant_performance_periods")) == 1
    assert len(store.rows("merchant_product_performance")) == 1   # own unpublished rows removed


def test_first_publication_race_is_lost_cleanly(store, monkeypatch):
    other = publish(week({("a", "ADS"): (10, 1)}))
    # This load read "nothing published" before the other one published.
    monkeypatch.setattr(performance_store, "_published", lambda *a: None)
    result = publish(week({("a", "ADS"): (77, 7)}))
    assert result.status == "lost_race"
    assert visible(store) == {("a", "ADS"): (10, 1)}
    assert {r["fetch_id"] for r in store.rows("merchant_product_performance")} == {other.fetch_id}


def test_compare_and_set_rejects_a_stale_replacement(store, monkeypatch):
    first = publish(week({("a", "ADS"): (10, 1)}))
    second = publish(week({("a", "ADS"): (20, 2)}))
    # A load that still believes the first load is current must not overwrite the second.
    monkeypatch.setattr(performance_store, "_published", lambda *a: {"fetch_id": first.fetch_id})
    result = publish(week({("a", "ADS"): (30, 3)}))
    assert result.status == "lost_race"
    assert visible(store) == {("a", "ADS"): (20, 2)}
    assert {r["fetch_id"] for r in store.rows("merchant_product_performance")} == {second.fetch_id}


def test_orphans_are_swept_only_when_old(store):
    publish(week({("a", "ADS"): (10, 1)}))
    store.seed("merchant_product_performance", [
        {"client_key": KEY, "merchant_id": MID, "period_start": W.isoformat(), "offer_id": "old", "marketing_method": "ADS",
         "fetch_id": "stopped", "created_at": (NOW - timedelta(days=2)).isoformat()},
        {"client_key": KEY, "merchant_id": MID, "period_start": W.isoformat(), "offer_id": "new", "marketing_method": "ADS",
         "fetch_id": "in-progress", "created_at": (NOW - timedelta(hours=1)).isoformat()},
    ])
    publish(week({("a", "ADS"): (11, 1)}))
    fetch_ids = {r["fetch_id"] for r in store.rows("merchant_product_performance")}
    assert "stopped" not in fetch_ids and "in-progress" in fetch_ids


def test_retention_unpublishes_first_and_only_touches_this_client(store):
    publish(week({("a", "ADS"): (1, 0)}, start=W - timedelta(weeks=61)))
    publish(week({("a", "ADS"): (2, 0)}))
    store.seed("merchant_performance_periods", [{"client_key": "other", "merchant_id": MID,
                                                 "period_start": (W - timedelta(weeks=70)).isoformat(), "fetch_id": "x"}])
    before = len(store.writes)
    performance_store.apply_retention(KEY, MID, keep_from=W - timedelta(weeks=59))
    assert store.writes[before:] == [
        ("merchant_performance_periods", "delete"), ("merchant_product_performance", "delete")]
    assert {p["period_start"] for p in store.rows("merchant_performance_periods")} == {
        W.isoformat(), (W - timedelta(weeks=70)).isoformat()}
    assert {r["period_start"] for r in store.rows("merchant_product_performance")} == {W.isoformat()}


# ---------------------------------------------------------------- load_weeks

def test_unresolved_client_key_writes_nothing(store, monkeypatch):
    monkeypatch.setattr(performance_job, "fetch_week", lambda *a: pytest.fail("must not fetch"))
    outcome = performance_job.load_weeks("999", [W], {}, None, "run1")
    assert outcome.client_key is None and outcome.errors
    assert store.writes == []


def test_one_failing_week_does_not_stop_the_others_and_published_weeks_are_skipped(store, monkeypatch):
    def fetch(mid, start):
        if start == W - timedelta(weeks=1):
            raise WeekFetchError("HTTP 503")
        return week({("a", "ADS"): (1, 0)}, start=start)
    monkeypatch.setattr(performance_job, "fetch_week", fetch)
    weeks = [W, W - timedelta(weeks=1), W - timedelta(weeks=2)]
    first = performance_job.load_weeks(MID, weeks, {}, None, "run1")
    assert first.published == [W.isoformat(), (W - timedelta(weeks=2)).isoformat()]
    assert first.errors[0][0] == (W - timedelta(weeks=1)).isoformat()
    again = performance_job.load_weeks(MID, weeks, {}, None, "run2", reload=False)
    assert again.skipped == [W.isoformat(), (W - timedelta(weeks=2)).isoformat()]
    assert again.errors[0][0] == (W - timedelta(weeks=1)).isoformat()


# ---------------------------------------------------------------- summary (M2)

def _period(start, ads=(100, 10), organic=(200, 2), ads_by=(80, 10), organic_by=(150, 2)):
    return {"period_start": start.isoformat(), "fetched_at": "t",
            "ads_impressions": ads[0], "ads_clicks": ads[1], "organic_impressions": organic[0], "organic_clicks": organic[1],
            "ads_impressions_by_offer": ads_by[0], "ads_clicks_by_offer": ads_by[1],
            "organic_impressions_by_offer": organic_by[0], "organic_clicks_by_offer": organic_by[1]}


def test_summary_keeps_channels_apart_and_trends_from_totals():
    periods = {p["period_start"]: p for p in [_period(W - timedelta(weeks=i)) for i in range(1, 5)]}
    periods[W.isoformat()] = _period(W, ads=(150, 12), ads_by=(1, 12), organic_by=(150, 1))
    periods[(W - timedelta(weeks=52)).isoformat()] = _period(W - timedelta(weeks=52), ads=(50, 4))
    s = summarize(periods, W)
    assert s["data_status"] == "complete"
    assert set(s) >= {"ADS", "ORGANIC"} and "impressions" not in s and "total" not in json.dumps(s)
    assert s["ADS"]["trend_vs_prev_4w"] == {"impressions_pct": 50.0, "clicks_pct": 20.0, "weeks_used": 4}
    assert s["ADS"]["trend_yoy"]["impressions_pct"] == 200.0
    assert s["ADS"]["completeness"] == {"impressions_by_offer_share": 0.0067, "clicks_by_offer_share": 1.0}
    assert s["ORGANIC"]["completeness"]["clicks_by_offer_share"] == 0.5     # clicks are measured, not assumed complete
    assert s["ORGANIC"]["trend_yoy"]["impressions_pct"] == 0.0


def test_summary_without_full_trend_window_or_current_week():
    periods = {W.isoformat(): _period(W), (W - timedelta(weeks=1)).isoformat(): _period(W - timedelta(weeks=1))}
    s = summarize(periods, W)
    assert s["data_status"] == "partial" and s["ADS"]["trend_vs_prev_4w"] is None and s["ADS"]["trend_yoy"] is None
    assert len(s["missing_trend_weeks"]) == 3
    assert summarize({}, W)["data_status"] == "missing"
    zero = summarize({W.isoformat(): _period(W, ads=(0, 0), ads_by=(0, 0))}, W)
    assert zero["ADS"]["ctr"] is None and zero["ADS"]["completeness"]["impressions_by_offer_share"] is None


def test_prompt_shows_channels_separately_and_replaces_the_7_day_total():
    s = summarize({W.isoformat(): _period(W)}, W)
    lines = "\n".join(prompt_lines(s))
    assert "- ADS: 100 impressions" in lines and "- ORGANIC: 200 impressions" in lines
    assert "never add them up" in lines and "does not have zero traffic" in lines
    msg = synthesis._build_user_message("X", MID, {}, {}, {"status": "ok", "impressions": 300, "clicks": 12, "ctr": 0.04},
                                        {"comparable": False}, s)
    assert "Impressions: 300" not in msg and "- ADS: 100 impressions" in msg
    assert "Cross-Channel" not in msg and "other agents (Ads, SEO)" not in synthesis.SYSTEM_PROMPT


# ---------------------------------------------------------------- dry run

def test_dry_run_publishes_nothing_but_sees_its_own_weeks(store, tmp_path, monkeypatch):
    runtime.configure(dry_run_dir=str(tmp_path), llm_enabled=False)
    monkeypatch.setattr(db, "_client", runtime.ReadOnlyClient(store))
    monkeypatch.setattr(performance_job, "fetch_week", lambda mid, start: week({("a", "ADS"): (1, 0)}, start=start))
    outcome = performance_job.load_weeks(MID, [W], {}, None, "dry-1")
    assert outcome.published == [W.isoformat()]
    assert store.writes == [] and store.rows("merchant_performance_periods") == []
    assert performance_job.channel_summary(KEY, MID, W + timedelta(days=8))["data_status"] == "partial"
    ops = [json.loads(l)["op"] for l in (tmp_path / "db_writes.jsonl").read_text().splitlines()]
    assert "publish" in ops and "insert" in ops
    with pytest.raises(runtime.DryRunWriteError):
        runtime.ReadOnlyClient(store).rpc("delete_everything", {})


def test_dry_run_before_the_migration(store, tmp_path, monkeypatch):
    runtime.configure(dry_run_dir=str(tmp_path), llm_enabled=False)

    class Missing:
        def table(self, name):
            raise RuntimeError("{'code': 'PGRST205', 'message': 'Could not find the table'}")

        def rpc(self, name, params):
            return store.rpc(name, params)

    monkeypatch.setattr(db, "_client", runtime.ReadOnlyClient(Missing()))
    monkeypatch.setattr(performance_store, "get_client", lambda: runtime.ReadOnlyClient(Missing()))
    monkeypatch.setattr(performance_job, "fetch_week", lambda mid, start: week({("a", "ADS"): (1, 0)}, start=start))
    assert performance_job.load_weeks(MID, [W], {}, None, "dry-1").published == [W.isoformat()]
