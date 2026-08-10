from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .domain import GameClassification, ReviewEnrichment
from .taxonomy import AspectTaxonomy, Taxonomy


T = TypeVar("T", bound=BaseModel)
ROOT = Path(__file__).resolve().parents[2]


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.llm_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def health_check(self) -> list[str]:
        try:
            response = await self.client.get(self.settings.llm_models_url)
            response.raise_for_status()
            models = [str(item.get("id")) for item in response.json().get("data", [])]
        except Exception as exc:
            raise LLMUnavailable(
                f"Local LLM endpoint is unavailable at {self.settings.llm_base_url}. "
                "Start the configured OpenAI-compatible Qwen server, then retry."
            ) from exc
        if self.settings.llm_model not in models:
            raise LLMUnavailable(
                f"Configured model {self.settings.llm_model!r} is not exposed by the endpoint. Available: {models}"
            )
        return models

    async def structured(self, system: str, user: dict, schema: type[T]) -> T:
        parse_error = ""
        for attempt in range(self.settings.llm_max_retries):
            prompt = {
                "input": user,
                "required_json_schema": schema.model_json_schema(),
                "parse_feedback": parse_error,
            }
            response = await self.client.post(f"{self.settings.llm_base_url.rstrip('/')}/chat/completions", json={
                "model": self.settings.llm_model,
                "temperature": self.settings.llm_temperature,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            })
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            try:
                return schema.model_validate_json(_extract_json(content))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                parse_error = f"Previous response failed schema validation: {exc}. Return only corrected JSON."
        raise ValueError(parse_error or "LLM returned invalid structured output")

    async def classify_game(self, metadata: dict, tags: dict[str, int], candidates: list[str], taxonomy: Taxonomy) -> GameClassification:
        system = (ROOT / "prompts" / "game_classification_v1.md").read_text()
        result = await self.structured(system, {
            "game": {"name": metadata.get("name"), "description": metadata.get("short_description"),
                     "steam_genres": metadata.get("genres", []), "categories": metadata.get("categories", [])},
            "tags": tags, "deterministic_candidates": candidates,
            "canonical_genres": sorted(taxonomy.labels),
        }, GameClassification)
        return taxonomy.validate(result)

    async def enrich_review(self, text: str, voted_up: bool | None, aspects: AspectTaxonomy) -> ReviewEnrichment:
        system = (ROOT / "prompts" / "review_enrichment_v1.md").read_text()
        result = await self.structured(system, {
            "review_text": text, "source_voted_up": voted_up,
            "allowed_aspects": {key: sorted(value) for key, value in aspects.categories.items()},
        }, ReviewEnrichment)
        invalid = [f"{x.category}/{x.subcategory}" for x in result.aspects if not aspects.validate(x.category, x.subcategory)]
        if invalid:
            raise ValueError(f"Unknown review aspects: {invalid}")
        return result


def _extract_json(content: str) -> str:
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response contains no JSON object")
    return value[start:end + 1]
