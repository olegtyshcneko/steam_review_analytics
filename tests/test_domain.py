from __future__ import annotations

import pytest
from pydantic import ValidationError

from games_analytics.domain import Aspect, GameClassification, ReviewEnrichmentItem, Statement, enrichment_eligibility, review_band
from games_analytics.taxonomy import AspectTaxonomy, Taxonomy, deterministic_candidates
from games_analytics.config import Settings


@pytest.mark.parametrize(("count", "expected"), [
    (0, "unqualified"), (19, "unqualified"), (20, "micro"), (49, "micro"),
    (50, "small"), (99, "small"), (100, "traction"), (499, "traction"),
    (500, "established"), (999, "established"), (1000, "hit"),
    (4999, "hit"), (5000, "big_hit"), (19999, "big_hit"), (20000, "mega_hit"),
])
def test_review_band_boundaries(count, expected):
    assert review_band(count) == expected


def test_enrichment_eligibility(settings):
    assert enrichment_eligibility("This combat system is responsive and excellent.", "english", settings) == (True, "pending")
    assert enrichment_eligibility("good", "english", settings) == (False, "skipped_low_information")
    assert enrichment_eligibility("Das Kampfsystem ist wirklich sehr gut gestaltet.", "german", settings) == (False, "skipped_language")


def test_comma_separated_language_environment(monkeypatch):
    monkeypatch.setenv("ENRICH_LANGUAGES", "english,german")
    assert Settings(_env_file=None).enrich_languages == ["english", "german"]


def test_invalid_reasoning_effort_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_reasoning_effort="extreme")


def test_deterministic_candidates():
    result = deterministic_candidates(["Deckbuilding", "Roguelike"], ["Strategy"])
    assert "Roguelike Deckbuilder" in result
    assert "Turn-based Strategy" in result


def test_taxonomy_rejects_unknown_label():
    value = GameClassification(primary_genre="Made Up", confidence=.5, reasoning_summary="fixture")
    with pytest.raises(ValueError, match="Unknown canonical"):
        Taxonomy().validate(value)


def test_aspect_taxonomy_and_confidence_validation():
    assert AspectTaxonomy().validate("technical", "stuttering")
    assert not AspectTaxonomy().validate("technical", "enemy_variety")
    with pytest.raises(ValidationError):
        Aspect(category="technical", subcategory="bugs", sentiment="negative", confidence=2)


def test_compact_enrichment_normalizes_to_full_model():
    item = ReviewEnrichmentItem.model_validate({
        "id": "123", "s": "mixed", "i": "mixed", "q": .9,
        "a": [{"c": "technical", "s": "performance", "n": None, "p": "negative", "q": .8}],
        "co": [{"l": "technical.performance", "n": None, "t": "Frame rate drops."}],
    })
    result = item.normalized()
    assert result.sentiment == "mixed"
    assert result.aspects[0].subcategory == "performance"
    assert result.complaints[0].statement == "Frame rate drops."


def test_review_labels_use_canonical_taxonomy_or_constrained_discovery():
    assert Statement(label="gameplay.combat", statement="Combat is responsive.")
    discovered = Statement(
        label="gameplay.other", novel_topic="finisher_pacing",
        statement="Finishers interrupt combat flow.",
    )
    assert discovered.novel_topic == "finisher_pacing"

    with pytest.raises(ValidationError, match="unknown canonical review label"):
        Statement(label="combat", statement="Combat is responsive.")
    with pytest.raises(ValidationError, match="novel_topic is required"):
        Statement(label="gameplay.other", statement="Unclassified gameplay issue.")
    with pytest.raises(ValidationError, match="only allowed"):
        Statement(label="gameplay.combat", novel_topic="fighting", statement="Combat is responsive.")
    with pytest.raises(ValidationError):
        Statement(label="gameplay.other", novel_topic="Finisher Pacing!", statement="Bad finishers.")


def test_review_intent_is_controlled_and_bucket_category_matches():
    with pytest.raises(ValidationError):
        ReviewEnrichmentItem.model_validate({"id": "1", "s": "positive", "i": "recommended", "q": .9})
    with pytest.raises(ValidationError, match="technical_issues labels must use the technical category"):
        ReviewEnrichmentItem.model_validate({
            "id": "1", "s": "negative", "i": "bug_report", "q": .9,
            "ti": [{"l": "gameplay.combat", "n": None, "t": "Combat crashes."}],
        })
    with pytest.raises(ValidationError, match="statement discoveries must also appear in aspects"):
        ReviewEnrichmentItem.model_validate({
            "id": "1", "s": "negative", "i": "discourage", "q": .9,
            "co": [{"l": "gameplay.other", "n": "finisher_pacing", "t": "Finishers interrupt combat."}],
        })
