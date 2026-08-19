from __future__ import annotations

import json
from pathlib import Path

from games_analytics.analysis_jobs import analysis_contract
from games_analytics.contracts import (
    compact_output_schema,
    contract_example,
    normalize_compact_item,
    review_label_input_schema,
)
from games_analytics.domain import ReviewEnrichmentBatch
from games_analytics.openrouter_batch import request_body

from scripts.export_contracts import documents


ROOT = Path(__file__).resolve().parents[1]


def test_public_contract_files_match_generated_source():
    for path, expected in documents().items():
        assert json.loads(path.read_text()) == expected, f"Regenerate stale contract: {path}"


def test_mcp_and_openrouter_share_the_public_output_schema():
    contract = analysis_contract()
    assert contract["input_schema"] == review_label_input_schema()
    assert contract["output_schema"] == compact_output_schema(document=True)
    request = request_body("fixture/model", [("review-1", "A useful review fixture.", False)])
    assert request["response_format"]["json_schema"]["schema"] == compact_output_schema()


def test_published_example_passes_runtime_output_validation():
    example = contract_example()
    normalized = {"items": [normalize_compact_item(item) for item in example["output"]["items"]]}
    parsed = ReviewEnrichmentBatch.model_validate(normalized)
    assert parsed.items[0].id == example["input"]["reviews"][0]["recommendation_id"]
