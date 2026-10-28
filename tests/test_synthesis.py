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
        {"total": 3, "issues_by_code": {"lpe": {"products": 2, "occurrences": 5, "by_context": {"SHOPPING_ADS": {"PL": 2}}, "attributes": {"title": 2, "price": 1}}}},
        {"error": "HTTP 500"},
        {"status": "error", "error": "HTTP 400: bad query"},
        {"comparable": False, "summary": "No complete previous snapshot"},
        None,
    )
    assert "lpe: 2 products, 5 occurrences" in msg
    assert "SHOPPING_ADS: PL 2" in msg
    assert "[products by attribute: title 2, price 1]" in msg
    assert "UNAVAILABLE (collection failed, not a lack of data): HTTP 400" in msg
    assert "Account Issues\n- UNAVAILABLE" in msg
    assert "NOT AVAILABLE: No complete previous snapshot" in msg
    assert "dedupe_key_suffix" not in synthesis.SYSTEM_PROMPT


def test_unverified_numbers_flags_invented_thresholds():
    source = "- landing_page_error: 5993 products\n- landing_page_error: 5963 -> 5993 products (+30)\n- CTR: 0.68%"
    rec = {"title": "5 993 produkty odrzucone", "summary": "Przybyło 30, to 96% odrzuceń, CTR 0,68%",
           "recommended_action": "1. Zdjęcia min. 800×800 px. 2. Sprawdź 1000 stron"}
    assert synthesis.unverified_numbers(rec, source) == [800, 1000]
    assert synthesis.unverified_numbers({"title": "5 993 produkty", "summary": "+30"}, source) == []


def test_prompt_forbids_revenue_estimates_and_unsourced_requirements():
    assert "revenue impact" not in synthesis.SYSTEM_PROMPT
    assert "Do not estimate revenue" in synthesis.SYSTEM_PROMPT
    assert "Do not state technical requirements" in synthesis.SYSTEM_PROMPT
