from scripts.benchmark_openrouter import (
    ModelRun,
    RequestRecord,
    automatic_metrics,
    compact_schema,
    judge_schema,
    make_batches,
    parse_response_partial,
    stable_sample,
)
from steam_market.config import Settings


def test_stable_sample_is_reproducible_and_seeded():
    rows = [(str(index), f"review {index}", bool(index % 2)) for index in range(20)]
    first = stable_sample(rows, 5, 42)
    assert first == stable_sample(rows, 5, 42)
    assert first != stable_sample(rows, 5, 43)


def test_make_batches_enforces_count_and_character_limits():
    settings = Settings(llm_batch_size=2, llm_batch_max_characters=1000)
    rows = [(str(index), "x" * 600, True) for index in range(3)]
    assert [len(batch) for batch in make_batches(rows, settings)] == [1, 1, 1]


def test_compact_schema_is_fully_inlined_and_requires_all_fields():
    schema = compact_schema()
    assert "$defs" not in str(schema)
    item = schema["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["properties"]["i"]["enum"] == [
        "recommend", "discourage", "mixed", "informational", "bug_report"
    ]
    statement = item["properties"]["co"]["items"]
    assert "gameplay.combat" in statement["properties"]["l"]["enum"]


def test_partial_parser_accepts_reordering_and_reports_missing_reviews():
    body = {
        "choices": [{"message": {"content": '{"items":[{"id":"2","s":"positive","i":"recommend","q":0.9}]}'}}],
        "usage": {"prompt_tokens": 10},
    }
    outputs, usage, errors = parse_response_partial(body, ["1", "2"])
    assert list(outputs) == ["2"]
    assert errors == ["1: missing from response"]
    assert usage["prompt_tokens"] == 10


def test_judge_schema_uses_an_enumerated_five_point_scale():
    schema = judge_schema()
    assert schema["properties"]["winner"]["enum"] == ["A", "B", "tie"]
    assert schema["properties"]["a"]["properties"]["precision"]["enum"] == [1, 2, 3, 4, 5]


def test_automatic_metrics_counts_valid_outputs_and_vote_agreement():
    run = ModelRun(model="model", mode="test", started_at="now")
    run.requests = [RequestRecord("batch", ["1", "2"], prompt_tokens=10, completion_tokens=5, cost_usd=0.01)]
    run.outputs = {
        "1": {"s": "positive", "a": [], "co": [], "pr": [], "fr": [], "ti": [], "mo": [], "ac": [], "mu": []},
        "2": {"s": "negative", "a": [], "co": [], "pr": [], "fr": [], "ti": [], "mo": [], "ac": [], "mu": []},
    }
    metrics = automatic_metrics(run, {"1": ("good", True), "2": ("bad", False)})
    assert metrics["review_success_rate"] == 1
    assert metrics["vote_sentiment_agreement"] == 1
    assert metrics["reported_cost_usd"] == 0.01
