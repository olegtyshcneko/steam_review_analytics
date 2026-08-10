from __future__ import annotations

from pathlib import Path

import yaml

from .domain import GameClassification


ROOT = Path(__file__).resolve().parents[2]


class Taxonomy:
    def __init__(self, path: Path | None = None):
        self.path = path or ROOT / "taxonomy" / "game_genres_v1.yaml"
        self.data = yaml.safe_load(self.path.read_text())
        self.version = str(self.data["version"])
        self.labels = set(self.data["genres"])

    def validate(self, result: GameClassification) -> GameClassification:
        invalid = ({result.primary_genre} | set(result.secondary_genres)) - self.labels
        if invalid:
            raise ValueError(f"Unknown canonical genre labels: {sorted(invalid)}")
        return result


RULES: list[tuple[set[str], str]] = [
    ({"deckbuilding", "roguelike"}, "Roguelike Deckbuilder"),
    ({"bullet heaven"}, "Survivors-like / Bullet Heaven"),
    ({"automation", "base building"}, "Factory / Automation"),
    ({"colony sim"}, "Colony Sim"),
    ({"city builder"}, "City Builder"),
    ({"souls-like"}, "Soulslike"),
    ({"metroidvania"}, "Metroidvania"),
    ({"tower defense"}, "Tower Defense"),
    ({"visual novel"}, "Visual Novel"),
    ({"psychological horror"}, "Psychological Horror"),
    ({"survival horror"}, "Survival Horror"),
    ({"open world survival craft"}, "Open World Survival Craft"),
    ({"farming sim"}, "Farming"),
    ({"crpg"}, "CRPG"),
    ({"jrpg"}, "JRPG"),
    ({"tactical rpg"}, "Tactical RPG"),
    ({"boomer shooter"}, "Boomer Shooter"),
    ({"extraction shooter"}, "Extraction Shooter"),
]


def deterministic_candidates(tags: list[str], genres: list[str], description: str = "") -> list[str]:
    signals = {x.lower() for x in tags + genres}
    text = description.lower()
    candidates = []
    for required, label in RULES:
        if required <= signals or all(term in text for term in required):
            candidates.append(label)
    generic = [("strategy", "Turn-based Strategy"), ("rpg", "Action RPG"),
               ("simulation", "Management"), ("racing", "Racing"), ("sports", "Sports"),
               ("puzzle", "Puzzle"), ("platformer", "2D Platformer"), ("horror", "Horror Adventure")]
    for signal, label in generic:
        if signal in signals and label not in candidates:
            candidates.append(label)
    return candidates


class AspectTaxonomy:
    def __init__(self, path: Path | None = None):
        path = path or ROOT / "taxonomy" / "review_aspects_v1.yaml"
        self.data = yaml.safe_load(path.read_text())
        self.categories: dict[str, set[str]] = {k: set(v) for k, v in self.data["categories"].items()}

    def validate(self, category: str, subcategory: str) -> bool:
        return subcategory in self.categories.get(category, set())
