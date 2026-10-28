import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from src.collectors import performance


@pytest.fixture
def api(monkeypatch):
    """Route the collector's HTTP calls to a handler; returns the list of request bodies."""
    calls = []
    state = {"handler": None}

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return state["handler"](request, len(calls))

    real_client = httpx.Client
    monkeypatch.setattr(performance.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setattr(performance, "get_credentials", lambda: SimpleNamespace(token="t"))
    return state, calls


def _rows(*pairs):
    return [{"productPerformanceView": {"impressions": str(i), "clicks": str(c)}} for i, c in pairs]


def test_query_uses_merchant_api_v1_syntax():
    q = performance.build_query("2026-10-21", "2026-10-27")
    assert "metrics." not in q and "segments." not in q
    assert "SELECT impressions, clicks, click_through_rate FROM product_performance_view" in q
    assert "WHERE date BETWEEN '2026-10-21' AND '2026-10-27'" in q


def test_sums_all_pages(api):
    state, calls = api

    def handler(request, n):
        if n == 1:
            return httpx.Response(200, json={"results": _rows((100, 2), (50, 1)), "nextPageToken": "p2"})
        return httpx.Response(200, json={"results": _rows((850, 7))})

    state["handler"] = handler
    r = performance.collect_performance("m1", today=date(2026, 10, 28))
    assert (r.status, r.impressions, r.clicks, r.rows) == ("ok", 1000, 10, 3)
    assert r.ctr == 0.01
    assert (r.date_from, r.date_to) == ("2026-10-21", "2026-10-27")
    assert calls[1]["pageToken"] == "p2"


def test_http_400_is_an_error_not_missing_data(api):
    state, _ = api
    state["handler"] = lambda req, n: httpx.Response(400, json={"error": {"message": "invalid field name 'metrics.impressions'"}})
    r = performance.collect_performance("m1", today=date(2026, 10, 28))
    assert r.status == "error"
    assert "HTTP 400" in r.error and "invalid field name" in r.error


def test_empty_answer_is_no_data(api):
    state, _ = api
    state["handler"] = lambda req, n: httpx.Response(200, json={})
    r = performance.collect_performance("m1", today=date(2026, 10, 28))
    assert (r.status, r.error, r.impressions) == ("no_data", None, 0)


def test_auth_failure_is_an_error(monkeypatch):
    def boom():
        raise RuntimeError("invalid_grant")
    monkeypatch.setattr(performance, "get_credentials", boom)
    r = performance.collect_performance("m1", today=date(2026, 10, 28))
    assert r.status == "error" and "invalid_grant" in r.error
