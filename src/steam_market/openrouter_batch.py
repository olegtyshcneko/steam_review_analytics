from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from pydantic import ValidationError

from .domain import ReviewEnrichmentItem
from .config import Settings
from .llm import _extract_json
from .taxonomy import AspectTaxonomy


OPENROUTER_API = "https://openrouter.ai/api"
TERMINAL_BATCH_STATES = {"completed", "failed", "cancelled", "expired"}
ROOT = Path(__file__).resolve().parents[2]
StateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def api_key() -> str:
    value = os.getenv("OPENROUTER_API_KEY") or Settings().openrouter_api_key
    if not value.startswith("sk-or-"):
        raise RuntimeError(
            "Set OPENROUTER_API_KEY in the MCP server environment. Never paste the key into chat or a tool call."
        )
    return value


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/olegtyshcneko/steam_review_analytics",
        "X-OpenRouter-Title": "Steam Review Intelligence",
    }


def make_batches(
    rows: list[tuple[str, str, bool | None]],
    batch_size: int = 12,
    max_characters: int = 18_000,
) -> list[list[tuple[str, str, bool | None]]]:
    batches: list[list[tuple[str, str, bool | None]]] = []
    current: list[tuple[str, str, bool | None]] = []
    characters = 0
    for row in rows:
        if current and (len(current) >= batch_size or characters + len(row[1]) > max_characters):
            batches.append(current)
            current, characters = [], 0
        current.append(row)
        characters += len(row[1])
    if current:
        batches.append(current)
    return batches


def compact_schema() -> dict[str, Any]:
    taxonomy = AspectTaxonomy()
    sentiment = {"type": "string", "enum": ["positive", "mixed", "negative", "neutral"]}
    novel_topic = {
        "type": "string",
        "pattern": "^(?:|[a-z][a-z0-9]*(?:_[a-z0-9]+){0,3})$",
        "maxLength": 64,
    }
    statement = {
        "type": "object",
        "properties": {
            "l": {"type": "string", "enum": sorted(taxonomy.labels)},
            "n": novel_topic,
            "t": {"type": "string", "maxLength": 240},
        },
        "required": ["l", "n", "t"],
        "additionalProperties": False,
    }
    aspect = {
        "type": "object",
        "properties": {
            "c": {"type": "string", "enum": sorted(taxonomy.categories)},
            "s": {"type": "string", "enum": sorted(set().union(*taxonomy.categories.values()))},
            "n": novel_topic,
            "p": sentiment,
            "q": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["c", "s", "n", "p", "q"],
        "additionalProperties": False,
    }
    properties: dict[str, Any] = {
        "id": {"type": "string"},
        "s": sentiment,
        "i": {
            "type": "string",
            "enum": ["recommend", "discourage", "mixed", "informational", "bug_report"],
        },
        "q": {"type": "number", "minimum": 0, "maximum": 1},
        "pc": {"type": "array", "items": {"type": "string", "maxLength": 120}},
        "a": {"type": "array", "items": aspect},
    }
    for key in ("co", "pr", "fr", "ti", "mo", "ac", "mu"):
        properties[key] = {"type": "array", "items": statement}
    item = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }


def request_body(
    model: str,
    batch: list[tuple[str, str, bool | None]],
    parse_feedback: str = "",
) -> dict[str, Any]:
    schema = compact_schema()
    taxonomy = AspectTaxonomy()
    prompt = {
        "input": {
            "reviews": [
                {"recommendation_id": rid, "review_text": text, "source_voted_up": voted_up}
                for rid, text, voted_up in batch
            ],
            "allowed_aspects": {key: sorted(value) for key, value in taxonomy.categories.items()},
            "batch_rule": "Return every supplied recommendation_id exactly once and in the same order.",
            "compact_field_legend": {
                "id": "recommendation_id", "s": "sentiment", "i": "review_intent", "q": "confidence",
                "pc": "player_context", "a": "aspects", "co": "complaints", "pr": "praises",
                "fr": "feature_requests", "ti": "technical_issues", "mo": "monetization_comments",
                "ac": "accessibility_comments", "mu": "multiplayer_comments",
                "aspect": {"c": "category", "s": "topic", "n": "novel topic or empty", "p": "sentiment", "q": "confidence"},
                "statement": {"l": "category.topic", "n": "novel topic or empty", "t": "normalized statement"},
            },
        },
        "required_json_schema": schema,
        "parse_feedback": parse_feedback,
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": (ROOT / "prompts" / "review_enrichment_v2.md").read_text()},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "ReviewEnrichmentBatch", "strict": True, "schema": schema},
        },
        "reasoning": {"effort": "low", "exclude": True},
        "seed": 42,
        "max_tokens": 8000,
    }


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], attempts: int = 4
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.post(url, headers=headers(), json=body)
            if response.status_code == 429 or response.status_code >= 500:
                delay = float(response.headers.get("Retry-After") or min(30, 2 ** attempt))
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc
            await asyncio.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"OpenRouter request failed after {attempts} attempts: {last_error}")


def _parse_result(
    body: dict[str, Any], expected_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, float | int], list[str]]:
    content = body["choices"][0]["message"]["content"]
    payload = json.loads(_extract_json(content))
    raw_items = payload.get("items", [])
    actual_ids = [str(item.get("id")) for item in raw_items]
    unexpected = sorted(set(actual_ids) - set(expected_ids))
    duplicates = sorted(value for value, count in Counter(actual_ids).items() if count > 1)
    if unexpected or duplicates:
        raise ValueError(f"Unexpected IDs {unexpected}; duplicate IDs {duplicates}")
    outputs: dict[str, dict[str, Any]] = {}
    errors = [f"{value}: missing" for value in expected_ids if value not in actual_ids]
    for raw in raw_items:
        for aspect in raw.get("a", []):
            if aspect.get("n") == "":
                aspect["n"] = None
        for key in ("co", "pr", "fr", "ti", "mo", "ac", "mu"):
            for statement in raw.get(key, []):
                if statement.get("n") == "":
                    statement["n"] = None
        try:
            item = ReviewEnrichmentItem.model_validate(raw)
            outputs[item.id] = item.model_dump(mode="json")
        except ValidationError as exc:
            errors.append(f"{raw.get('id')}: {str(exc)[:500]}")
    usage = body.get("usage") or {}
    return outputs, {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cost_usd": float(usage.get("cost") or 0),
    }, errors


async def _notify(callback: StateCallback | None, state: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(state)
    if result is not None:
        await result


async def run_openrouter_batch(
    rows: list[tuple[str, str, bool | None]],
    model: str,
    poll_seconds: float = 10,
    batch_size: int = 12,
    max_characters: int = 18_000,
    parse_feedback: str = "",
    callback: StateCallback | None = None,
) -> dict[str, Any]:
    batches = make_batches(rows, batch_size, max_characters)
    requests: list[dict[str, Any]] = []
    expected: dict[str, list[str]] = {}
    for index, batch in enumerate(batches):
        custom_id = f"reviews-{index:05d}"
        expected[custom_id] = [row[0] for row in batch]
        requests.append({"custom_id": custom_id, "body": request_body(model, batch, parse_feedback)})

    async with httpx.AsyncClient(timeout=httpx.Timeout(240)) as client:
        response = await _post_with_retry(client, f"{OPENROUTER_API}/beta/batches", {
            "endpoint": "/v1/chat/completions",
            "model": model,
            "requests": requests,
        })
        state = response.json()
        batch_id = str(state["id"])
        await _notify(callback, {"batch_id": batch_id, "status": state.get("status", "submitted")})
        while state.get("status") not in TERMINAL_BATCH_STATES:
            await asyncio.sleep(poll_seconds)
            response = await client.get(f"{OPENROUTER_API}/beta/batches/{batch_id}", headers=headers())
            if response.status_code == 404:
                continue
            response.raise_for_status()
            state = response.json()
            await _notify(callback, {"batch_id": batch_id, "status": state.get("status", "running")})
        if state.get("status") != "completed":
            raise RuntimeError(
                f"OpenRouter batch {batch_id} ended as {state.get('status')}: {state.get('error')}"
            )

    outputs: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    usage = Counter()
    result_ids: set[str] = set()
    for result in state.get("results") or []:
        custom_id = str(result.get("custom_id"))
        if custom_id not in expected:
            continue
        result_ids.add(custom_id)
        response = result.get("response") or {}
        body = response.get("body") or response
        try:
            parsed, item_usage, item_errors = _parse_result(body, expected[custom_id])
            outputs.update(parsed)
            usage.update(item_usage)
            errors.extend(item_errors)
        except Exception as exc:
            errors.append(f"{custom_id}: {type(exc).__name__}: {str(exc)[:500]}")
    for missing in sorted(set(expected) - result_ids):
        errors.append(f"{missing}: missing batch result")
    aggregate_usage = state.get("usage") or {}
    if not usage["cost_usd"]:
        usage["cost_usd"] = float(aggregate_usage.get("cost") or 0)
    return {
        "batch_id": batch_id,
        "model": model,
        "requested": len(rows),
        "valid": len(outputs),
        "outputs": outputs,
        "errors": errors,
        "usage": dict(usage),
    }
