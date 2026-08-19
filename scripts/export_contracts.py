from __future__ import annotations

import json
from pathlib import Path

from games_analytics.contracts import compact_output_schema, contract_example, review_label_input_schema
from games_analytics.domain import GameClassification


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def game_classification_input_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "GameClassificationV1Input",
        "type": "object",
        "properties": {
            "game": {"type": "object"},
            "tags": {"type": "object", "additionalProperties": {"type": "integer"}},
            "deterministic_candidates": {"type": "array", "items": {"type": "string"}},
            "canonical_genres": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["game", "tags", "deterministic_candidates", "canonical_genres"],
        "additionalProperties": False,
    }


def documents() -> dict[Path, dict]:
    game_output = GameClassification.model_json_schema()
    game_output.update({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "GameClassificationV1Output",
    })
    return {
        CONTRACTS / "review-enrichment-v2.input.schema.json": review_label_input_schema(),
        CONTRACTS / "review-enrichment-v2.output.schema.json": compact_output_schema(document=True),
        CONTRACTS / "game-classification-v1.input.schema.json": game_classification_input_schema(),
        CONTRACTS / "game-classification-v1.output.schema.json": game_output,
        CONTRACTS / "examples" / "review-enrichment-v2.json": contract_example(),
    }


def main() -> None:
    for path, document in documents().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
