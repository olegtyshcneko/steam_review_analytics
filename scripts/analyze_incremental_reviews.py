#!/usr/bin/env python3
"""Prepare and enrich a configured incremental-review analysis corpus.

Raw Steam review text remains in DuckDB. This script saves only IDs, aggregate
manifest data, and model-derived structured outputs below ignored data/ paths.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_openrouter import (
    GEMINI_MODEL,
    ModelRun,
    load_model_run,
    make_batches,
    now,
    run_gemini_batch,
    save_json,
)
from games_analytics.config import Settings


GAMES = {
    2666510: "Rusty's Retirement",
    1473350: "(the) Gnorp Apologue",
}
DEFAULT_OUTPUT = Path("data/analysis/incremental-cross-game-2026-08-16")
POSITIVE_SAMPLE_PER_GAME = 500
SEED = 42
PRIOR_COST_PER_REVIEW = 0.00061131375


def informative(text: str) -> bool:
    meaningful = [token for token in text.split() if any(character.isalpha() for character in token)]
    return len(text.strip()) >= 40 and len(meaningful) >= 4


def stable_rank(recommendation_id: str, seed: int = SEED) -> bytes:
    return hashlib.sha256(f"{seed}:{recommendation_id}".encode()).digest()


def load_rows(settings: Settings) -> list[dict[str, Any]]:
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        placeholders = ",".join("?" for _ in GAMES)
        rows = connection.execute(
            f"""SELECT recommendation_id,appid,review_text,voted_up,language,
                      timestamp_created,playtime_at_review_minutes,votes_up,
                      received_for_free,written_during_early_access
               FROM reviews WHERE appid IN ({placeholders})""",
            list(GAMES),
        ).fetchall()
    finally:
        connection.close()
    keys = (
        "recommendation_id", "appid", "review_text", "voted_up", "language",
        "timestamp_created", "playtime_at_review_minutes", "votes_up",
        "received_for_free", "written_during_early_access",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


def select_corpus(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {"negative": [], "positive": []}
    for appid in GAMES:
        eligible = [row for row in rows if row["appid"] == appid and informative(row["review_text"])]
        negative = sorted((row for row in eligible if not row["voted_up"]), key=lambda row: row["recommendation_id"])
        positive = sorted(
            (row for row in eligible if row["voted_up"]),
            key=lambda row: stable_rank(row["recommendation_id"]),
        )[:POSITIVE_SAMPLE_PER_GAME]
        selected["negative"].extend(negative)
        selected["positive"].extend(positive)
    return selected


def corpus_hash(rows: list[dict[str, Any]]) -> str:
    ids = sorted(row["recommendation_id"] for row in rows)
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def median(values: list[int | float]) -> float | None:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2


def cohort_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "reviews": len(rows),
        "sample_hash": corpus_hash(rows),
        "languages": dict(Counter(row["language"] or "unknown" for row in rows).most_common()),
        "median_characters": median([len(row["review_text"]) for row in rows]),
        "median_playtime_at_review_minutes": median(
            [row["playtime_at_review_minutes"] for row in rows if row["playtime_at_review_minutes"] is not None]
        ),
        "helpful_votes": sum(int(row["votes_up"] or 0) for row in rows),
        "received_for_free": sum(bool(row["received_for_free"]) for row in rows),
        "early_access": sum(bool(row["written_during_early_access"]) for row in rows),
    }


def build_manifest(all_rows: list[dict[str, Any]], corpus: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    games: dict[str, Any] = {}
    for appid, name in GAMES.items():
        game_rows = [row for row in all_rows if row["appid"] == appid]
        eligible = [row for row in game_rows if informative(row["review_text"])]
        games[str(appid)] = {
            "name": name,
            "raw_reviews": len(game_rows),
            "raw_negative": sum(not row["voted_up"] for row in game_rows),
            "raw_positive": sum(bool(row["voted_up"]) for row in game_rows),
            "eligible_negative": sum(not row["voted_up"] for row in eligible),
            "eligible_positive": sum(bool(row["voted_up"]) for row in eligible),
            "selected_negative": sum(row["appid"] == appid for row in corpus["negative"]),
            "selected_positive": sum(row["appid"] == appid for row in corpus["positive"]),
        }
    total_selected = sum(len(rows) for rows in corpus.values())
    return {
        "created_at": now(),
        "games": games,
        "selection": {
            "seed": SEED,
            "negative": "all reviews with at least 40 characters and four alphabetic tokens",
            "positive": f"deterministic SHA-256 sample of {POSITIVE_SAMPLE_PER_GAME} eligible reviews per game",
            "language_policy": "all Steam languages; model normalizes statements and topics into English",
        },
        "cohorts": {name: cohort_summary(rows) for name, rows in corpus.items()},
        "selected_reviews": total_selected,
        "estimated_cost_usd_from_smoke_rate": total_selected * PRIOR_COST_PER_REVIEW,
        "recommendation_ids": {
            name: [row["recommendation_id"] for row in rows] for name, rows in corpus.items()
        },
    }


def request_rows(rows: list[dict[str, Any]]) -> list[tuple[str, str, bool | None]]:
    return [(row["recommendation_id"], row["review_text"], row["voted_up"]) for row in rows]


def merge_runs(runs: list[ModelRun]) -> dict[str, Any]:
    outputs: dict[str, dict[str, Any]] = {}
    for run in runs:
        outputs.update(run.outputs)
    return {
        "model": GEMINI_MODEL,
        "runs": [
            {
                "batch_id": run.batch_id,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "wall_seconds": run.wall_seconds,
                "requested": sum(len(record.review_ids) for record in run.requests),
                "valid": len(run.outputs),
                "request_failures": sum(bool(record.error) for record in run.requests),
                "prompt_tokens": sum(record.prompt_tokens for record in run.requests),
                "completion_tokens": sum(record.completion_tokens for record in run.requests),
                "reported_cost_usd": sum(record.cost_usd for record in run.requests),
            }
            for run in runs
        ],
        "requested": sum(len(record.review_ids) for record in runs[0].requests),
        "valid": len(outputs),
        "reported_cost_usd": sum(record.cost_usd for run in runs for record in run.requests),
        "outputs": outputs,
    }


async def enrich_cohort(
    cohort: str,
    rows: list[dict[str, Any]],
    output_dir: Path,
    resume: bool,
) -> dict[str, Any]:
    selected = request_rows(rows)
    row_by_id = {row[0]: row for row in selected}
    primary_settings = Settings(llm_batch_size=12, llm_batch_max_characters=18000)
    runs: list[ModelRun] = []
    primary_path = output_dir / f"{cohort}-pass-1.json"
    if resume and primary_path.exists():
        primary = load_model_run(primary_path)
    else:
        primary = await run_gemini_batch(make_batches(selected, primary_settings), 5)
        save_json(primary_path, asdict(primary))
    runs.append(primary)

    outputs = dict(primary.outputs)
    feedback = (
        "Correct only missing or invalid items. Return every supplied ID exactly once. "
        "Use exact category.topic pairs. novel_topic is empty unless topic is other, "
        "and each statement discovery has a matching aspect discovery."
    )
    retry_settings = Settings(llm_batch_size=4, llm_batch_max_characters=7000)
    for attempt in (1, 2):
        missing_ids = [recommendation_id for recommendation_id in row_by_id if recommendation_id not in outputs]
        if not missing_ids:
            break
        retry_path = output_dir / f"{cohort}-retry-{attempt}.json"
        if resume and retry_path.exists():
            retry = load_model_run(retry_path)
        else:
            retry_rows = [row_by_id[recommendation_id] for recommendation_id in missing_ids]
            retry = await run_gemini_batch(make_batches(retry_rows, retry_settings), 5, feedback)
            save_json(retry_path, asdict(retry))
        runs.append(retry)
        outputs.update(retry.outputs)

    result = merge_runs(runs)
    result["cohort"] = cohort
    result["requested"] = len(selected)
    result["valid"] = len(outputs)
    result["missing_ids"] = [recommendation_id for recommendation_id in row_by_id if recommendation_id not in outputs]
    result["outputs"] = outputs
    save_json(output_dir / f"{cohort}-result.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("prepare", "negative", "positive"), required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    all_rows = load_rows(settings)
    corpus = select_corpus(all_rows)
    manifest = build_manifest(all_rows, corpus)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_json(args.output_dir / "manifest.json", manifest)
    if args.phase == "prepare":
        print(json.dumps({key: value for key, value in manifest.items() if key != "recommendation_ids"}, indent=2))
        return
    result = asyncio.run(enrich_cohort(args.phase, corpus[args.phase], args.output_dir, args.resume))
    print(json.dumps({key: value for key, value in result.items() if key != "outputs"}, indent=2))


if __name__ == "__main__":
    main()
