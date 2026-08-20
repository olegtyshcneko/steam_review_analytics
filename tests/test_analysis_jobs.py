from __future__ import annotations

import json
from pathlib import Path

from mcp import Client

from games_analytics.analysis_jobs import AnalysisJobStore, analysis_contract
from games_analytics.batch_worker import selected_rows
from games_analytics.database import Database
from games_analytics.domain import StoreProduct, StoreReview
from games_analytics.mcp_server import mcp
from games_analytics.openrouter_batch import _parse_result, request_body


def review(rec_id: str, text: str, voted_up: bool) -> dict:
    return {
        "recommendationid": rec_id,
        "language": "english",
        "review": text,
        "voted_up": voted_up,
        "votes_up": 3,
        "author": {"steamid": f"author-{rec_id}", "playtime_at_review": 600},
    }


def compact_item(rec_id: str, voted_up: bool) -> dict:
    if voted_up:
        return {
            "id": rec_id, "s": "positive", "i": "recommend", "q": .9, "pc": [],
            "a": [{"c": "gameplay", "s": "core_loop", "n": None, "p": "positive", "q": .9}],
            "co": [], "pr": [{"l": "gameplay.core_loop", "n": None, "t": "Core loop stays satisfying"}],
            "fr": [], "ti": [], "mo": [], "ac": [], "mu": [],
        }
    return {
        "id": rec_id, "s": "negative", "i": "discourage", "q": .9, "pc": [],
        "a": [{"c": "content", "s": "content_amount", "n": None, "p": "negative", "q": .9}],
        "co": [{"l": "content.content_amount", "n": None, "t": "Needs more meaningful content"}],
        "pr": [], "fr": [{"l": "content.content_amount", "n": None, "t": "Add new systems and goals"}],
        "ti": [], "mo": [], "ac": [], "mu": [],
    }


def populated_store(tmp_path: Path) -> AnalysisJobStore:
    database_path = tmp_path / "reviews.duckdb"
    database = Database(database_path)
    database.initialize()
    for appid, name in ((10, "First Game"), (20, "Second Game")):
        database.upsert_catalog_game(appid, name)
        database.upsert_reviews(appid, [
            review(f"{appid}-n", "The promising loop ends too early and needs several meaningful systems.", False),
            review(f"{appid}-p", "The core loop feels polished, satisfying, and consistently enjoyable to play.", True),
        ], lambda value: f"hash-{value}")
    database.close()
    return AnalysisJobStore(tmp_path / "jobs", database_path)


def test_harness_job_is_checkpointed_aggregated_and_rendered(tmp_path):
    store = populated_store(tmp_path)
    created = store.create([10, 20], "What do players lack?", negative_limit_per_game=10, positive_limit_per_game=10)
    job_id = created["job_id"]
    batch = store.next_batch(job_id, 10)
    assert [item["source_voted_up"] for item in batch["reviews"]] == [False, True, False, True]
    status = store.submit(job_id, [
        compact_item(item["recommendation_id"], item["source_voted_up"])
        for item in batch["reviews"]
    ])
    assert status["status"] == "ready_for_synthesis"
    aggregate = store.aggregate(job_id)
    assert aggregate["coverage"] == {"selected": 4, "labeled": 4, "complete": True}
    assert aggregate["shared_complaints"][0]["label"] == "content.content_amount"
    report = store.save_report(job_id, {
        "title": "Two-game review analysis",
        "executive_summary": "Players enjoy the basic loop but want more meaningful systems and longer-term goals.",
        "conclusions": ["Depth matters more than simply extending waiting times."],
        "findings": [{
            "title": "Content ends before the loop develops",
            "explanation": "Both games receive the same recurring request for meaningful systems and continued goals.",
            "evidence": ["Content complaints appear in both negative samples."],
            "recommendation": "Add interacting systems that create new decisions instead of slower progression.",
        }],
        "game_ideas": [{
            "name": "Branching Workshop",
            "pitch": "An incremental workshop where every production branch changes future constraints.",
            "core_loop": "Automate production, choose mutually exclusive branches, and redesign the workshop around consequences.",
            "evidence_fit": "It preserves the satisfying core loop while supplying meaningful systems and longer-term goals.",
            "risks": ["Branches must remain legible."],
        }],
    })
    assert report["status"] == "complete"
    html = Path(report["artifacts"]["report_html"]).read_text()
    assert "Branching Workshop" in html
    assert "First Game" in html


def test_contract_marks_reviews_as_untrusted():
    assert "untrusted" in analysis_contract()["untrusted_content_rule"].lower()


def test_mobile_store_job_uses_rating_polarity_and_shared_analysis_contract(tmp_path):
    database_path = tmp_path / "mobile.duckdb"
    database = Database(database_path)
    database.initialize()
    product = StoreProduct(platform="app_store", product_id="123", name="Mobile Fixture")
    key = database.upsert_store_product(product)
    database.upsert_store_reviews(key, [
        StoreReview(review_id="n", rating=1, text="The promising progression stops early and needs several meaningful systems."),
        StoreReview(review_id="p", rating=5, text="The progression loop feels polished, satisfying, and consistently enjoyable."),
        StoreReview(review_id="m", rating=3, text="This neutral review is deliberately excluded from polarized sampling."),
    ])
    database.close()
    store = AnalysisJobStore(tmp_path / "jobs", database_path)
    created = store.create_store([key], "What is missing?", negative_limit_per_game=10, positive_limit_per_game=10)
    batch = store.next_batch(created["job_id"], 10)
    assert [item["source_voted_up"] for item in batch["reviews"]] == [False, True]
    assert created["source"] == "store"
    provider_rows = selected_rows(store, created["job_id"], store.manifest(created["job_id"])["review_ids"])
    assert [row[2] for row in provider_rows] == [False, True]


async def test_mcp_exposes_public_workflow_tools():
    async with Client(mcp) as client:
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    assert {"create_analysis", "create_store_analysis", "mine_store_game", "next_review_batch",
            "start_provider_batch", "save_analysis_report"} <= names
    batch_tool = next(tool for tool in result.tools if tool.name == "start_provider_batch")
    assert "key" not in " ".join(batch_tool.input_schema["properties"]).lower()


def test_openrouter_batch_contract_parses_valid_items_without_credentials():
    item = compact_item("10-n", False)
    request = request_body("google/gemini-3.7-flash", [("10-n", "A detailed review fixture.", False)])
    assert "Authorization" not in json.dumps(request)
    outputs, usage, errors = _parse_result({
        "choices": [{"message": {"content": json.dumps({"items": [item]})}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.001},
    }, ["10-n"])
    assert errors == []
    assert outputs["10-n"]["co"][0]["l"] == "content.content_amount"
    assert usage == {"prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.001}
