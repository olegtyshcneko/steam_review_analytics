from __future__ import annotations

import pytest
from pydantic import ValidationError

from steam_market.domain import Aspect, GameClassification, enrichment_eligibility, review_band
from steam_market.taxonomy import AspectTaxonomy, Taxonomy, deterministic_candidates
from steam_market.config import Settings


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
