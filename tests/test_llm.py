from __future__ import annotations

import json

import httpx
import pytest
import respx

from games_analytics.domain import GameClassification
from games_analytics.llm import LLMClient
from games_analytics.taxonomy import AspectTaxonomy


@pytest.mark.asyncio
@respx.mock
async def test_structured_output_sends_full_json_schema(settings):
    route = respx.post(f"{settings.llm_base_url}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({
            "primary_genre": "FPS", "confidence": 0.9, "reasoning_summary": "Fixture"
        })}}]
    }))
    client = LLMClient(settings)
    try:
        result = await client.structured("system", {"fixture": True}, GameClassification)
    finally:
        await client.close()
    assert result.primary_genre == "FPS"
    body = json.loads(route.calls[0].request.content)
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["title"] == "GameClassification"
    assert body["reasoning_effort"] == "none"


@pytest.mark.asyncio
@respx.mock
async def test_review_schema_exposes_controlled_labels_and_intents(settings):
    route = respx.post(f"{settings.llm_base_url}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({
            "items": [{"id": "123", "s": "positive", "i": "recommend", "q": 0.9}]
        })}}]
    }))
    client = LLMClient(settings)
    try:
        result = await client.enrich_reviews(
            [("123", "The combat is responsive and enjoyable.", True)], AspectTaxonomy()
        )
    finally:
        await client.close()

    assert result["123"].review_intent == "recommend"
    body = json.loads(route.calls[0].request.content)
    schema = body["response_format"]["json_schema"]["schema"]
    item = schema["$defs"]["ReviewEnrichmentItem"]["properties"]
    statement = schema["$defs"]["CompactStatement"]["properties"]
    assert item["i"]["enum"] == ["recommend", "discourage", "mixed", "informational", "bug_report"]
    assert "gameplay.combat" in statement["l"]["enum"]
    assert "combat" not in statement["l"]["enum"]
