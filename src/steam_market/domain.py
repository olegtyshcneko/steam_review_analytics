from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CatalogGame(BaseModel):
    appid: int
    name: str
    source_metadata: dict = {}


class ReviewSummary(BaseModel):
    total_reviews: int = 0
    total_positive: int = 0
    total_negative: int = 0
    review_score: int | None = None
    review_score_desc: str | None = None


class ReviewPage(BaseModel):
    summary: ReviewSummary
    reviews: list[dict]
    cursor: str | None = None


class Aspect(BaseModel):
    category: str
    subcategory: str
    sentiment: Literal["positive", "mixed", "negative", "neutral"]
    confidence: float = Field(ge=0, le=1)


class Statement(BaseModel):
    label: str
    statement: str


class ReviewEnrichment(BaseModel):
    sentiment: Literal["positive", "mixed", "negative", "neutral"]
    review_intent: str
    player_context: list[str] = []
    aspects: list[Aspect] = []
    complaints: list[Statement] = []
    praises: list[Statement] = []
    feature_requests: list[Statement] = []
    technical_issues: list[Statement] = []
    monetization_comments: list[Statement] = []
    accessibility_comments: list[Statement] = []
    multiplayer_comments: list[Statement] = []
    confidence: float = Field(ge=0, le=1)


class CompactAspect(BaseModel):
    c: str
    s: str
    p: Literal["positive", "mixed", "negative", "neutral"]
    q: float = Field(ge=0, le=1)


class CompactStatement(BaseModel):
    l: str
    t: str


class ReviewEnrichmentItem(BaseModel):
    id: str
    s: Literal["positive", "mixed", "negative", "neutral"]
    i: str
    q: float = Field(ge=0, le=1)
    pc: list[str] = []
    a: list[CompactAspect] = []
    co: list[CompactStatement] = []
    pr: list[CompactStatement] = []
    fr: list[CompactStatement] = []
    ti: list[CompactStatement] = []
    mo: list[CompactStatement] = []
    ac: list[CompactStatement] = []
    mu: list[CompactStatement] = []

    def normalized(self) -> ReviewEnrichment:
        def statements(values: list[CompactStatement]) -> list[Statement]:
            return [Statement(label=value.l, statement=value.t) for value in values]

        return ReviewEnrichment(
            sentiment=self.s,
            review_intent=self.i,
            confidence=self.q,
            player_context=self.pc,
            aspects=[Aspect(category=value.c, subcategory=value.s,
                            sentiment=value.p, confidence=value.q) for value in self.a],
            complaints=statements(self.co),
            praises=statements(self.pr),
            feature_requests=statements(self.fr),
            technical_issues=statements(self.ti),
            monetization_comments=statements(self.mo),
            accessibility_comments=statements(self.ac),
            multiplayer_comments=statements(self.mu),
        )


class ReviewEnrichmentBatch(BaseModel):
    items: list[ReviewEnrichmentItem]


class GameClassification(BaseModel):
    primary_genre: str
    secondary_genres: list[str] = []
    mechanics: list[str] = []
    themes: list[str] = []
    modes: list[str] = []
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str
    proposed_labels: list[str] = []

    @field_validator("secondary_genres")
    @classmethod
    def unique_secondary(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


def review_band(total: int) -> str:
    if total < 20:
        return "unqualified"
    if total < 50:
        return "micro"
    if total < 100:
        return "small"
    if total < 500:
        return "traction"
    if total < 1_000:
        return "established"
    if total < 5_000:
        return "hit"
    if total < 20_000:
        return "big_hit"
    return "mega_hit"


def enrichment_eligibility(text: str, language: str, settings: object) -> tuple[bool, str]:
    languages = getattr(settings, "enrich_languages")
    if language.lower() not in languages:
        return False, "skipped_language"
    meaningful = [token for token in text.split() if any(ch.isalpha() for ch in token)]
    if len(text.strip()) < getattr(settings, "enrich_min_characters") or len(meaningful) < 4:
        return False, "skipped_low_information"
    return True, "pending"
