from __future__ import annotations

import json

import httpx
import pytest
import respx

from steam_market.domain import GameClassification
from steam_market.llm import LLMClient


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
