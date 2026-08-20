from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb

from .analysis_jobs import AnalysisJobStore
from .openrouter_batch import run_openrouter_batch


DEFAULT_MODEL = "google/gemini-3.7-flash"


def selected_rows(
    store: AnalysisJobStore, job_id: str, recommendation_ids: list[str]
) -> list[tuple[str, str, bool | None]]:
    if not recommendation_ids:
        return []
    placeholders = ",".join("?" for _ in recommendation_ids)
    connection = duckdb.connect(str(store.database_path), read_only=True)
    try:
        if store.manifest(job_id).get("source") == "store":
            rows = connection.execute(
                f"SELECT review_key,review_text,source_voted_up FROM store_reviews "
                f"WHERE review_key IN ({placeholders})",
                recommendation_ids,
            ).fetchall()
        else:
            rows = connection.execute(
                f"SELECT recommendation_id,review_text,voted_up FROM reviews "
                f"WHERE recommendation_id IN ({placeholders})",
                recommendation_ids,
            ).fetchall()
    finally:
        connection.close()
    by_id = {str(recommendation_id): (str(recommendation_id), text, voted_up)
             for recommendation_id, text, voted_up in rows}
    missing = [value for value in recommendation_ids if value not in by_id]
    if missing:
        raise RuntimeError(f"Selected reviews disappeared from the database: {missing[:5]}")
    return [by_id[value] for value in recommendation_ids]


async def run_worker(
    store: AnalysisJobStore,
    job_id: str,
    model: str,
    poll_seconds: float,
) -> None:
    manifest = store.manifest(job_id)
    if manifest["mode"] != "provider_batch":
        raise RuntimeError("This job was not created in provider_batch mode")
    outputs: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    usage = Counter()

    async def provider_update(state: dict[str, Any]) -> None:
        current_provider = store.manifest(job_id).get("provider") or {}
        store.set_provider_state(job_id, {
            "model": model,
            "pid": current_provider.get("pid"),
            "worker_log": current_provider.get("worker_log"),
            "attempts": attempts,
            "current": state,
            "usage": dict(usage),
        }, "batch_running")

    try:
        selected = selected_rows(store, job_id, manifest["review_ids"])
        feedback = ""
        for attempt in range(3):
            missing_ids = [row[0] for row in selected if row[0] not in outputs]
            if not missing_ids:
                break
            missing_set = set(missing_ids)
            current_rows = [row for row in selected if row[0] in missing_set]
            result = await run_openrouter_batch(
                current_rows,
                model=model,
                poll_seconds=poll_seconds,
                batch_size=12 if attempt == 0 else 4,
                max_characters=18_000 if attempt == 0 else 7_000,
                parse_feedback=feedback,
                callback=provider_update,
            )
            outputs.update(result.pop("outputs"))
            attempts.append(result)
            usage.update(result.get("usage") or {})
            feedback = (
                "Correct only missing or invalid items. Return every supplied ID exactly once. "
                "Use exact category.topic pairs. Use novel_topic only with category.other."
            )

        store.replace_labels(job_id, outputs)
        status = "ready_for_synthesis" if len(outputs) == len(selected) else "batch_incomplete"
        store.set_provider_state(job_id, {
            "model": model,
            "attempts": attempts,
            "requested": len(selected),
            "valid": len(outputs),
            "missing": len(selected) - len(outputs),
            "usage": dict(usage),
        }, status)
    except BaseException as exc:
        store.set_provider_state(job_id, {
            "model": model,
            "attempts": attempts,
            "error": f"{type(exc).__name__}: {str(exc)[:2000]}",
            "usage": dict(usage),
        }, "failed")
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Games Analytics provider batch job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--jobs-path", type=Path, required=True)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--poll-seconds", type=float, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = AnalysisJobStore(args.jobs_path, args.database_path)
    asyncio.run(run_worker(store, args.job_id, args.model, args.poll_seconds))


if __name__ == "__main__":
    main()
