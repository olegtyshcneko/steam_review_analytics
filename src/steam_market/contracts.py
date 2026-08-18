from __future__ import annotations

from copy import deepcopy
from typing import Any

from .taxonomy import AspectTaxonomy


COMPACT_FIELD_LEGEND: dict[str, Any] = {
    "id": "recommendation_id",
    "s": "sentiment",
    "i": "review_intent",
    "q": "confidence",
    "pc": "player_context",
    "a": "aspects",
    "co": "complaints",
    "pr": "praises",
    "fr": "feature_requests",
    "ti": "technical_issues",
    "mo": "monetization_comments",
    "ac": "accessibility_comments",
    "mu": "multiplayer_comments",
    "aspect": {
        "c": "category",
        "s": "topic",
        "n": "novel topic or empty string",
        "p": "sentiment",
        "q": "confidence",
    },
    "statement": {
        "l": "category.topic",
        "n": "novel topic or empty string",
        "t": "normalized statement",
    },
}


def review_label_input_schema() -> dict[str, Any]:
    """Return the public logical input contract for one labeling batch."""
    review = {
        "type": "object",
        "properties": {
            "recommendation_id": {"type": "string", "minLength": 1},
            "review_text": {"type": "string"},
            "source_voted_up": {"type": ["boolean", "null"]},
            "language": {"type": "string"},
        },
        "required": ["recommendation_id", "review_text", "source_voted_up"],
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/olegtyshcneko/steam_review_analytics/contracts/review-enrichment-v2.input.schema.json",
        "title": "ReviewEnrichmentV2Input",
        "description": "A bounded batch of untrusted Steam review text to classify.",
        "type": "object",
        "properties": {
            "reviews": {"type": "array", "minItems": 1, "maxItems": 100, "items": review},
        },
        "required": ["reviews"],
        "additionalProperties": False,
    }


def compact_output_schema(*, document: bool = False) -> dict[str, Any]:
    """Return the strict compact review-v2 output schema shared by MCP and providers."""
    taxonomy = AspectTaxonomy()
    sentiment = {"type": "string", "enum": ["positive", "mixed", "negative", "neutral"]}
    # OpenRouter providers used in the validated benchmark require a string-only
    # schema here. Empty strings are normalized to None before Pydantic validation.
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
        "id": {"type": "string", "minLength": 1},
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
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }
    if document:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://github.com/olegtyshcneko/steam_review_analytics/contracts/review-enrichment-v2.output.schema.json",
            "title": "ReviewEnrichmentV2Output",
            "description": "Strict compact labels for a complete review batch.",
            **schema,
        }
    return schema


def contract_example() -> dict[str, Any]:
    return {
        "input": {
            "reviews": [{
                "recommendation_id": "example-1001",
                "review_text": "The automation is satisfying, but progression ends before builds become interesting.",
                "source_voted_up": False,
                "language": "english",
            }]
        },
        "output": {
            "items": [{
                "id": "example-1001",
                "s": "mixed",
                "i": "discourage",
                "q": 0.94,
                "pc": [],
                "a": [
                    {"c": "gameplay", "s": "core_loop", "n": "", "p": "positive", "q": 0.96},
                    {"c": "content", "s": "content_amount", "n": "", "p": "negative", "q": 0.93},
                ],
                "co": [{"l": "content.content_amount", "n": "", "t": "Progression ends before builds become interesting"}],
                "pr": [{"l": "gameplay.core_loop", "n": "", "t": "Automation feels satisfying"}],
                "fr": [],
                "ti": [],
                "mo": [],
                "ac": [],
                "mu": [],
            }]
        },
    }


def compact_field_legend() -> dict[str, Any]:
    return deepcopy(COMPACT_FIELD_LEGEND)


def normalize_compact_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert the provider wire representation into the canonical runtime model."""
    normalized = deepcopy(item)
    for aspect in normalized.get("a", []):
        if aspect.get("n") == "":
            aspect["n"] = None
    for key in ("co", "pr", "fr", "ti", "mo", "ac", "mu"):
        for statement in normalized.get(key, []):
            if statement.get("n") == "":
                statement["n"] = None
    return normalized
