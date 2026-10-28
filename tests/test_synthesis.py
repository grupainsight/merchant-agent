from types import SimpleNamespace

import pytest

from src import runtime, synthesis

ARGS = dict(client_name="X", merchant_id="m1", health_summary={}, account_issues={}, performance={}, diff={})


def fake_anthropic(monkeypatch, text=None, exc=None, stop_reason="end_turn"):
    def create(**kw):
        if exc:
            raise exc
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
            stop_reason=stop_reason,
        )
    monkeypatch.setattr(synthesis.anthropic, "Anthropic", lambda api_key: SimpleNamespace(messages=SimpleNamespace(create=create)))


@pytest.fixture(autouse=True)
def llm_on():
    runtime.configure(dry_run_dir=None, llm_enabled=True)


def test_ok(monkeypatch):
    fake_anthropic(monkeypatch, text='```json\n[{"title": "a"}]\n```')
    r = synthesis.synthesize_recommendations(**ARGS)
    assert r.status == "ok" and r.recommendations == [{"title": "a"}]
    assert (r.input_tokens, r.output_tokens) == (1200, 300)


def test_api_error_is_a_failure_not_an_empty_list(monkeypatch):
    fake_anthropic(monkeypatch, exc=RuntimeError("overloaded"))
    r = synthesis.synthesize_recommendations(**ARGS)
    assert r.status == "failed" and "overloaded" in r.error and not r.llm_called


def test_unparseable_answer_is_a_failure_and_keeps_token_usage(monkeypatch):
    fake_anthropic(monkeypatch, text="Oto rekomendacje: ...")
    r = synthesis.synthesize_recommendations(**ARGS)
    assert r.status == "failed" and "JSON" in r.error
    assert r.llm_called and r.input_tokens == 1200


def test_truncated_answer_is_a_failure(monkeypatch):
    fake_anthropic(monkeypatch, text='[{"title": "a"', stop_reason="max_tokens")
    assert synthesis.synthesize_recommendations(**ARGS).status == "failed"


def test_disabled():
    runtime.configure(dry_run_dir=None, llm_enabled=False)
    assert synthesis.synthesize_recommendations(**ARGS).status == "disabled"


def test_prompt_marks_unavailable_sources_and_counts():
    msg = synthesis._build_user_message(
        "X", "m1",
        {"total": 3, "issues_by_code": {"lpe": {"products": 2, "occurrences": 5, "by_context": {"SHOPPING_ADS": {"PL": 2}}}}},
        {"error": "HTTP 500"},
        {"status": "error", "error": "HTTP 400: bad query"},
        {"comparable": False, "summary": "No complete previous snapshot"},
        None,
    )
    assert "lpe: 2 products, 5 occurrences" in msg
    assert "SHOPPING_ADS: PL 2" in msg
    assert "UNAVAILABLE (collection failed, not a lack of data): HTTP 400" in msg
    assert "Account Issues\n- UNAVAILABLE" in msg
    assert "NOT AVAILABLE: No complete previous snapshot" in msg
    assert "dedupe_key_suffix" not in synthesis.SYSTEM_PROMPT
