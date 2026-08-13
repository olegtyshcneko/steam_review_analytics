#!/usr/bin/env python3
"""Benchmark OpenRouter models against the Steam review enrichment contract.

Raw reviews and model responses are written below data/ (gitignored). The compact
summary is safe to commit because it contains aggregate metrics only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
from pydantic import BaseModel, Field, ValidationError

from steam_market.config import Settings
from steam_market.domain import ReviewEnrichmentBatch, ReviewEnrichmentItem, enrichment_eligibility
from steam_market.llm import _extract_json
from steam_market.taxonomy import AspectTaxonomy


ROOT = Path(__file__).resolve().parents[1]
OPENROUTER_API = "https://openrouter.ai/api"
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash-0731"
GEMINI_MODEL = "google/gemini-3.7-flash"
JUDGE_MODEL = "anthropic/claude-opus-5"
TERMINAL_BATCH_STATES = {"completed", "failed", "cancelled", "expired"}


class CandidateScore(BaseModel):
    faithfulness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    taxonomy_fit: int = Field(ge=1, le=5)
    precision: int = Field(ge=1, le=5)


class PairwiseJudgment(BaseModel):
    winner: str
    a: CandidateScore
    b: CandidateScore
    reason: str


@dataclass
class RequestRecord:
    custom_id: str
    review_ids: list[str]
    elapsed_seconds: float = 0.0
    attempts: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class ModelRun:
    model: str
    mode: str
    started_at: str
    completed_at: str = ""
    wall_seconds: float = 0.0
    batch_id: str | None = None
    requests: list[RequestRecord] = field(default_factory=list)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def api_key() -> str:
    value = os.getenv("OPENROUTER_API_KEY") or Settings().llm_api_key
    if not value.startswith("sk-or-"):
        raise RuntimeError("Set OPENROUTER_API_KEY or LLM_API_KEY to an OpenRouter key")
    return value


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/olegtyshcneko/steam_review_analytics",
        "X-OpenRouter-Title": "steam-review-analytics benchmark",
    }


def stable_sample(rows: list[tuple[str, str, bool | None]], size: int, seed: int) -> list[tuple[str, str, bool | None]]:
    ranked = sorted(rows, key=lambda row: hashlib.sha256(f"{seed}:{row[0]}".encode()).digest())
    return ranked[:size]


def select_reviews(settings: Settings, appid: int, size: int, seed: int) -> list[tuple[str, str, bool | None]]:
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT recommendation_id,review_text,language,voted_up FROM reviews WHERE appid=?",
            [appid],
        ).fetchall()
    finally:
        con.close()
    eligible = [(rid, text, voted_up) for rid, text, language, voted_up in rows
                if enrichment_eligibility(text, language or "", settings)[0]]
    if len(eligible) < size:
        raise RuntimeError(f"appid {appid} has only {len(eligible)} eligible reviews; requested {size}")
    return stable_sample(eligible, size, seed)


def make_batches(rows: list[tuple[str, str, bool | None]], settings: Settings) -> list[list[tuple[str, str, bool | None]]]:
    batches: list[list[tuple[str, str, bool | None]]] = []
    current: list[tuple[str, str, bool | None]] = []
    characters = 0
    for row in rows:
        if current and (len(current) >= settings.llm_batch_size
                        or characters + len(row[1]) > settings.llm_batch_max_characters):
            batches.append(current)
            current, characters = [], 0
        current.append(row)
        characters += len(row[1])
    if current:
        batches.append(current)
    return batches


def request_body(model: str, batch: list[tuple[str, str, bool | None]], reasoning_effort: str) -> dict[str, Any]:
    schema = compact_schema()
    aspects = AspectTaxonomy()
    user = {
        "reviews": [
            {"recommendation_id": rid, "review_text": text, "source_voted_up": voted_up}
            for rid, text, voted_up in batch
        ],
        "allowed_aspects": {key: sorted(value) for key, value in aspects.categories.items()},
        "batch_rule": "Return exactly one compact item for every supplied recommendation_id, in the same order.",
        "compact_field_legend": {
            "id": "recommendation_id", "s": "sentiment", "i": "review_intent", "q": "confidence",
            "pc": "player_context", "a": "aspects", "co": "complaints", "pr": "praises",
            "fr": "feature_requests", "ti": "technical_issues", "mo": "monetization_comments",
            "ac": "accessibility_comments", "mu": "multiplayer_comments",
            "aspect": {"c": "category", "s": "subcategory", "p": "sentiment", "q": "confidence"},
            "statement": {"l": "label", "t": "statement"},
        },
    }
    prompt = {"input": user, "required_json_schema": schema, "parse_feedback": ""}
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": (ROOT / "prompts/review_enrichment_v1.md").read_text()},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "ReviewEnrichmentBatch", "strict": True, "schema": schema},
        },
        "reasoning": {"effort": reasoning_effort, "exclude": True},
        "seed": 42,
        "max_tokens": 8000,
        "provider": {"require_parameters": True, "data_collection": "deny"},
    }


def compact_schema() -> dict[str, Any]:
    """Inline the schema for providers whose native Batch API rejects JSON Schema refs."""
    sentiment = {"type": "string", "enum": ["positive", "mixed", "negative", "neutral"]}
    statement = {
        "type": "object",
        "properties": {"l": {"type": "string"}, "t": {"type": "string"}},
        "required": ["l", "t"],
        "additionalProperties": False,
    }
    aspect = {
        "type": "object",
        "properties": {
            "c": {"type": "string"}, "s": {"type": "string"}, "p": sentiment,
            "q": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["c", "s", "p", "q"],
        "additionalProperties": False,
    }
    properties: dict[str, Any] = {
        "id": {"type": "string"}, "s": sentiment, "i": {"type": "string"},
        "q": {"type": "number", "minimum": 0, "maximum": 1},
        "pc": {"type": "array", "items": {"type": "string"}},
        "a": {"type": "array", "items": aspect},
    }
    for key in ("co", "pr", "fr", "ti", "mo", "ac", "mu"):
        properties[key] = {"type": "array", "items": statement}
    item = {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }
def parse_response(body: dict[str, Any], expected_ids: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, float | int]]:
    content = body["choices"][0]["message"]["content"]
    parsed = ReviewEnrichmentBatch.model_validate_json(_extract_json(content))
    actual_ids = [item.id for item in parsed.items]
    if actual_ids != expected_ids:
        raise ValueError(f"response IDs differ: expected {expected_ids}, got {actual_ids}")
    usage = body.get("usage") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return (
        {item.id: item.model_dump() for item in parsed.items},
        {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
            "cost_usd": float(usage.get("cost") or 0),
        },
    )


def load_model_run(path: Path) -> ModelRun:
    raw = json.loads(path.read_text())
    requests = [RequestRecord(**value) for value in raw.pop("requests")]
    return ModelRun(**raw, requests=requests)


async def post_with_retry(client: httpx.AsyncClient, url: str, body: dict[str, Any], attempts: int = 4) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.post(url, headers=headers(), json=body)
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            last_error = httpx.HTTPStatusError(response.text[:500], request=response.request, response=response)
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
        if attempt < attempts:
            await asyncio.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}")


async def run_deepseek(batches: list[list[tuple[str, str, bool | None]]], concurrency: int) -> ModelRun:
    run = ModelRun(model=DEEPSEEK_MODEL, mode="concurrent semantic batches", started_at=now())
    started = time.monotonic()
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        async def one(index: int, batch: list[tuple[str, str, bool | None]]) -> None:
            expected = [row[0] for row in batch]
            record = RequestRecord(custom_id=f"deepseek-{index:04d}", review_ids=expected)
            async with semaphore:
                call_started = time.monotonic()
                try:
                    response = await post_with_retry(
                        client,
                        f"{OPENROUTER_API}/v1/chat/completions",
                        request_body(DEEPSEEK_MODEL, batch, "none"),
                        attempts=2,
                    )
                    outputs, usage = parse_response(response.json(), expected)
                    run.outputs.update(outputs)
                    for key, value in usage.items():
                        setattr(record, key, value)
                except Exception as exc:  # preserve failures as benchmark results
                    record.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                finally:
                    record.elapsed_seconds = time.monotonic() - call_started
                    run.requests.append(record)

        await asyncio.gather(*(one(index, batch) for index, batch in enumerate(batches)))
    run.requests.sort(key=lambda value: value.custom_id)
    run.wall_seconds = time.monotonic() - started
    run.completed_at = now()
    return run


async def run_gemini_batch(batches: list[list[tuple[str, str, bool | None]]], poll_seconds: float) -> ModelRun:
    run = ModelRun(model=GEMINI_MODEL, mode="OpenRouter asynchronous Batch API", started_at=now())
    started = time.monotonic()
    requests = []
    records: dict[str, RequestRecord] = {}
    for index, batch in enumerate(batches):
        custom_id = f"gemini-{index:04d}"
        records[custom_id] = RequestRecord(custom_id=custom_id, review_ids=[row[0] for row in batch])
        body = request_body(GEMINI_MODEL, batch, "low")
        body.pop("provider", None)  # routing preferences are invalid inside native provider batch items
        requests.append({"custom_id": custom_id, "body": body})
    async with httpx.AsyncClient(timeout=httpx.Timeout(240)) as client:
        created = await post_with_retry(client, f"{OPENROUTER_API}/beta/batches", {
            "endpoint": "/v1/chat/completions", "model": GEMINI_MODEL, "requests": requests,
        })
        state = created.json()
        run.batch_id = state["id"]
        while state.get("status") not in TERMINAL_BATCH_STATES:
            await asyncio.sleep(poll_seconds)
            response = await client.get(f"{OPENROUTER_API}/beta/batches/{run.batch_id}", headers=headers())
            if response.status_code == 404:  # short eventual-consistency window after creation
                continue
            response.raise_for_status()
            state = response.json()
        if state.get("status") != "completed":
            raise RuntimeError(f"Gemini batch ended as {state.get('status')}: {state.get('error')}")
        for result in state.get("results") or []:
            custom_id = result.get("custom_id")
            if custom_id not in records:
                continue
            record = records[custom_id]
            response = result.get("response") or {}
            body = response.get("body") or response
            try:
                outputs, usage = parse_response(body, record.review_ids)
                run.outputs.update(outputs)
                for key, value in usage.items():
                    setattr(record, key, value)
            except Exception as exc:
                error = result.get("error")
                record.error = f"{type(exc).__name__}: {str(exc)[:400]}; upstream={str(error)[:200]}"
        missing = set(records) - {str(item.get("custom_id")) for item in state.get("results") or []}
        for custom_id in missing:
            records[custom_id].error = "missing from completed batch results"
        aggregate = state.get("usage") or {}
        if sum(record.cost_usd for record in records.values()) == 0 and aggregate:
            # Batch responses may expose cost only at the job level.
            completed = [record for record in records.values() if not record.error]
            total_cost = float(aggregate.get("cost") or 0)
            if completed and total_cost:
                each = total_cost / len(completed)
                for record in completed:
                    record.cost_usd = each
    run.requests = sorted(records.values(), key=lambda value: value.custom_id)
    run.wall_seconds = time.monotonic() - started
    run.completed_at = now()
    return run


def normalized_text_values(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("co", "pr", "fr", "ti", "mo", "ac", "mu"):
        values.extend(str(entry.get("t", "")) for entry in item.get(key, []))
    return values


def automatic_metrics(run: ModelRun, reviews: dict[str, tuple[str, bool | None]]) -> dict[str, Any]:
    successful = len(run.outputs)
    request_failures = sum(bool(record.error) for record in run.requests)
    sentiments = Counter(item.get("s") for item in run.outputs.values())
    vote_matches = 0
    vote_comparable = 0
    empty = 0
    aspect_count = 0
    statement_count = 0
    duplicates = 0
    invalid_aspects = 0
    taxonomy = AspectTaxonomy()
    for rid, item in run.outputs.items():
        _, voted_up = reviews[rid]
        sentiment = item.get("s")
        if voted_up is not None and sentiment in {"positive", "negative"}:
            vote_comparable += 1
            vote_matches += int((sentiment == "positive") == voted_up)
        aspects = item.get("a", [])
        invalid_aspects += sum(not taxonomy.validate(value["c"], value["s"]) for value in aspects)
        statements = normalized_text_values(item)
        aspect_count += len(aspects)
        statement_count += len(statements)
        empty += int(not aspects and not statements)
        duplicates += len(statements) - len(set(value.casefold().strip() for value in statements))
    latencies = [record.elapsed_seconds for record in run.requests if not record.error and record.elapsed_seconds]
    return {
        "reviews_requested": len(reviews),
        "reviews_valid": successful,
        "review_success_rate": successful / len(reviews),
        "requests": len(run.requests),
        "request_failures": request_failures,
        "wall_seconds": run.wall_seconds,
        "request_latency_p50_seconds": statistics.median(latencies) if latencies else None,
        "request_latency_p95_seconds": percentile(latencies, 0.95) if latencies else None,
        "prompt_tokens": sum(record.prompt_tokens for record in run.requests),
        "completion_tokens": sum(record.completion_tokens for record in run.requests),
        "reasoning_tokens": sum(record.reasoning_tokens for record in run.requests),
        "reported_cost_usd": sum(record.cost_usd for record in run.requests),
        "vote_sentiment_agreement": vote_matches / vote_comparable if vote_comparable else None,
        "vote_comparable_reviews": vote_comparable,
        "empty_extraction_rate": empty / successful if successful else None,
        "aspects_per_review": aspect_count / successful if successful else None,
        "statements_per_review": statement_count / successful if successful else None,
        "duplicate_statements": duplicates,
        "invalid_aspects": invalid_aspects,
        "sentiment_distribution": dict(sentiments),
    }


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)]


def pair_agreement(left: ModelRun, right: ModelRun) -> dict[str, Any]:
    ids = sorted(set(left.outputs) & set(right.outputs))
    sentiment = sum(left.outputs[rid]["s"] == right.outputs[rid]["s"] for rid in ids)
    aspect_jaccards = []
    for rid in ids:
        a = {(value["c"], value["s"], value["p"]) for value in left.outputs[rid].get("a", [])}
        b = {(value["c"], value["s"], value["p"]) for value in right.outputs[rid].get("a", [])}
        aspect_jaccards.append(len(a & b) / len(a | b) if a | b else 1.0)
    return {
        "comparable_reviews": len(ids),
        "sentiment_agreement": sentiment / len(ids) if ids else None,
        "mean_aspect_jaccard": statistics.mean(aspect_jaccards) if aspect_jaccards else None,
    }


def compact_for_judge(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "id"}


def judge_schema() -> dict[str, Any]:
    score = {
        "type": "object",
        "properties": {
            key: {"type": "integer", "enum": [1, 2, 3, 4, 5]}
            for key in ("faithfulness", "completeness", "taxonomy_fit", "precision")
        },
        "required": ["faithfulness", "completeness", "taxonomy_fit", "precision"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "a": score,
            "b": score,
            "reason": {"type": "string"},
        },
        "required": ["winner", "a", "b", "reason"],
        "additionalProperties": False,
    }


async def judge_pairs(
    reviews: dict[str, tuple[str, bool | None]], left: ModelRun, right: ModelRun, size: int, concurrency: int, seed: int,
) -> dict[str, Any]:
    common = sorted(set(left.outputs) & set(right.outputs))
    chosen = stable_sample([(rid, reviews[rid][0], reviews[rid][1]) for rid in common], min(size, len(common)), seed + 991)
    schema = judge_schema()
    semaphore = asyncio.Semaphore(concurrency)
    judgments: list[dict[str, Any]] = []
    total_cost = 0.0
    total_tokens = Counter()

    async with httpx.AsyncClient(timeout=httpx.Timeout(240)) as client:
        async def one(index: int, row: tuple[str, str, bool | None]) -> None:
            nonlocal total_cost
            rid, text, voted_up = row
            swapped = hashlib.sha256(f"judge:{seed}:{rid}".encode()).digest()[0] % 2 == 1
            first, second = (right.outputs[rid], left.outputs[rid]) if swapped else (left.outputs[rid], right.outputs[rid])
            prompt = {
                "review_text": text,
                "source_voted_up": voted_up,
                "allowed_aspects": {key: sorted(value) for key, value in AspectTaxonomy().categories.items()},
                "candidate_a": compact_for_judge(first),
                "candidate_b": compact_for_judge(second),
                "rubric": {
                    "faithfulness": "Every extracted claim must be supported by the review; penalize invention.",
                    "completeness": "Capture important praise, complaints, requests, technical issues, and context.",
                    "taxonomy_fit": "Aspect categories and sentiments must accurately represent the review.",
                    "precision": "Prefer concise, non-duplicative, correctly scoped extraction.",
                    "winner": "Return A, B, or tie based on overall extraction usefulness.",
                },
            }
            body = {
                "model": JUDGE_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a strict blind evaluator of structured review extraction. Judge only against the supplied review and rubric."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_schema", "json_schema": {"name": "PairwiseJudgment", "strict": True, "schema": schema}},
                "reasoning": {"effort": "none", "exclude": True},
                "max_tokens": 1000,
                "provider": {"require_parameters": True, "data_collection": "deny"},
            }
            async with semaphore:
                try:
                    response = await post_with_retry(client, f"{OPENROUTER_API}/v1/chat/completions", body)
                    data = response.json()
                    judgment = PairwiseJudgment.model_validate_json(_extract_json(data["choices"][0]["message"]["content"]))
                    winner = judgment.winner.strip().lower()
                    canonical = "tie" if winner == "tie" else (("gemini" if swapped else "deepseek") if winner == "a" else ("deepseek" if swapped else "gemini"))
                    scores = {"deepseek": judgment.b.model_dump(), "gemini": judgment.a.model_dump()} if swapped else {"deepseek": judgment.a.model_dump(), "gemini": judgment.b.model_dump()}
                    judgments.append({"recommendation_id": rid, "winner": canonical, "scores": scores, "reason": judgment.reason})
                    usage = data.get("usage") or {}
                    total_cost += float(usage.get("cost") or 0)
                    total_tokens.update(prompt=int(usage.get("prompt_tokens") or 0), completion=int(usage.get("completion_tokens") or 0))
                except Exception as exc:
                    judgments.append({"recommendation_id": rid, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})

        await asyncio.gather(*(one(index, row) for index, row in enumerate(chosen)))
    valid = [item for item in judgments if "error" not in item]
    winners = Counter(item["winner"] for item in valid)
    mean_scores: dict[str, dict[str, float]] = {}
    for model in ("deepseek", "gemini"):
        mean_scores[model] = {
            dimension: statistics.mean(item["scores"][model][dimension] for item in valid)
            for dimension in ("faithfulness", "completeness", "taxonomy_fit", "precision")
        } if valid else {}
    return {
        "model": JUDGE_MODEL,
        "requested": len(chosen),
        "valid": len(valid),
        "failures": len(judgments) - len(valid),
        "wins": dict(winners),
        "mean_scores": mean_scores,
        "reported_cost_usd": total_cost,
        "prompt_tokens": total_tokens["prompt"],
        "completion_tokens": total_tokens["completion"],
        "judgments": judgments,
    }


def now() -> str:
    return datetime.now(UTC).isoformat()


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


async def main(args: argparse.Namespace) -> None:
    settings = Settings()
    reviews = select_reviews(settings, args.appid, args.sample_size, args.seed)
    batches = make_batches(reviews, settings)
    review_map = {rid: (text, voted_up) for rid, text, voted_up in reviews}
    output_dir = Path(args.output_dir)
    manifest = {
        "created_at": now(), "appid": args.appid, "sample_size": len(reviews), "seed": args.seed,
        "sample_hash": hashlib.sha256("\n".join(sorted(review_map)).encode()).hexdigest(),
        "requests": len(batches), "batch_size": settings.llm_batch_size,
        "batch_max_characters": settings.llm_batch_max_characters,
        "review_characters": sum(len(text) for text, _ in review_map.values()),
        "recommendation_ids": sorted(review_map),
    }
    save_json(output_dir / "manifest.json", manifest)
    print(f"Selected {len(reviews)} eligible reviews in {len(batches)} requests; sample={manifest['sample_hash'][:12]}", flush=True)

    if args.resume and (output_dir / "deepseek.json").exists() and (output_dir / "gemini.json").exists():
        deepseek = load_model_run(output_dir / "deepseek.json")
        gemini = load_model_run(output_dir / "gemini.json")
        print("Loaded existing model outputs; rerunning analysis/judge only", flush=True)
    else:
        deepseek, gemini = await asyncio.gather(
            run_deepseek(batches, args.concurrency),
            run_gemini_batch(batches, args.poll_seconds),
        )
        save_json(output_dir / "deepseek.json", asdict(deepseek))
        save_json(output_dir / "gemini.json", asdict(gemini))
        print(f"DeepSeek valid={len(deepseek.outputs)} wall={deepseek.wall_seconds:.1f}s", flush=True)
        print(f"Gemini valid={len(gemini.outputs)} wall={gemini.wall_seconds:.1f}s", flush=True)

    judge = await judge_pairs(review_map, deepseek, gemini, args.judge_size, args.judge_concurrency, args.seed)
    save_json(output_dir / "judge.json", judge)
    summary = {
        "benchmark": manifest,
        "models": {
            "deepseek": {"model": deepseek.model, "mode": deepseek.mode, **automatic_metrics(deepseek, review_map)},
            "gemini": {"model": gemini.model, "mode": gemini.mode, "batch_id": gemini.batch_id, **automatic_metrics(gemini, review_map)},
        },
        "agreement": pair_agreement(deepseek, gemini),
        "blind_judge": {key: value for key, value in judge.items() if key != "judgments"},
    }
    save_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--appid", type=int, default=261110)
    parser.add_argument("--sample-size", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--judge-size", type=int, default=100)
    parser.add_argument("--judge-concurrency", type=int, default=10)
    parser.add_argument("--output-dir", default="data/benchmarks/openrouter-2026-08-13")
    parser.add_argument("--resume", action="store_true", help="Reuse saved provider outputs and rerun judge/analysis")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
