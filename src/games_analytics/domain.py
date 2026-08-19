from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .taxonomy import AspectTaxonomy


ReviewIntent = Literal["recommend", "discourage", "mixed", "informational", "bug_report"]
NovelTopic = Annotated[
    str,
    Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){0,3}$",
        description="One to four lowercase snake_case words for a genuinely new topic.",
    ),
]

_ASPECT_TAXONOMY = AspectTaxonomy()
_CATEGORY_SCHEMA = {"enum": sorted(_ASPECT_TAXONOMY.categories)}
_SUBCATEGORY_SCHEMA = {"enum": sorted(set().union(*_ASPECT_TAXONOMY.categories.values()))}
_LABEL_SCHEMA = {"enum": sorted(_ASPECT_TAXONOMY.labels)}


def _validate_topic(category: str, subcategory: str, novel_topic: str | None) -> None:
    if not _ASPECT_TAXONOMY.validate(category, subcategory):
        raise ValueError(f"unknown canonical review topic: {category}.{subcategory}")
    if subcategory == "other" and novel_topic is None:
        raise ValueError("novel_topic is required when subcategory is 'other'")
    if subcategory != "other" and novel_topic is not None:
        raise ValueError("novel_topic is only allowed when subcategory is 'other'")


def _validate_statement(label: str, novel_topic: str | None) -> None:
    if not _ASPECT_TAXONOMY.validate_label(label):
        raise ValueError(f"unknown canonical review label: {label}")
    category, subcategory = label.split(".", 1)
    _validate_topic(category, subcategory, novel_topic)


def _validate_bucket(values: list[Statement], category: str, bucket: str) -> None:
    invalid = [value.label for value in values if not value.label.startswith(f"{category}.")]
    if invalid:
        raise ValueError(f"{bucket} labels must use the {category} category: {invalid}")


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


StorePlatform = Literal["google_play", "app_store"]


class StoreProduct(BaseModel):
    platform: StorePlatform
    product_id: str
    name: str
    developer: str | None = None
    description: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = {}

    @property
    def product_key(self) -> str:
        return f"{self.platform}:{self.product_id}"


class StoreReview(BaseModel):
    review_id: str
    text: str
    rating: int = Field(ge=1, le=5)
    title: str | None = None
    language: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    app_version: str | None = None
    votes_up: int | None = None
    developer_response: str | None = None
    raw_payload: dict[str, Any] = {}


class StoreReviewPage(BaseModel):
    reviews: list[StoreReview]
    next_cursor: str | None = None


class Aspect(BaseModel):
    category: str = Field(json_schema_extra=_CATEGORY_SCHEMA)
    subcategory: str = Field(json_schema_extra=_SUBCATEGORY_SCHEMA)
    novel_topic: NovelTopic | None = None
    sentiment: Literal["positive", "mixed", "negative", "neutral"]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def canonical_or_discovered(self) -> Aspect:
        _validate_topic(self.category, self.subcategory, self.novel_topic)
        return self


class Statement(BaseModel):
    label: str = Field(
        json_schema_extra=_LABEL_SCHEMA,
        description="Canonical category.topic label; use category.other only for discovery.",
    )
    novel_topic: NovelTopic | None = None
    statement: str

    @model_validator(mode="after")
    def canonical_or_discovered(self) -> Statement:
        _validate_statement(self.label, self.novel_topic)
        return self


class ReviewEnrichment(BaseModel):
    sentiment: Literal["positive", "mixed", "negative", "neutral"]
    review_intent: ReviewIntent
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

    @model_validator(mode="after")
    def bucket_categories_match(self) -> ReviewEnrichment:
        _validate_bucket(self.technical_issues, "technical", "technical_issues")
        _validate_bucket(self.monetization_comments, "product", "monetization_comments")
        _validate_bucket(self.accessibility_comments, "accessibility", "accessibility_comments")
        _validate_bucket(self.multiplayer_comments, "multiplayer", "multiplayer_comments")
        aspect_discoveries = {
            (value.category, value.novel_topic) for value in self.aspects if value.novel_topic is not None
        }
        statements = (
            self.complaints + self.praises + self.feature_requests + self.technical_issues
            + self.monetization_comments + self.accessibility_comments + self.multiplayer_comments
        )
        statement_discoveries = {
            (value.label.split(".", 1)[0], value.novel_topic)
            for value in statements if value.novel_topic is not None
        }
        if missing := statement_discoveries - aspect_discoveries:
            raise ValueError(f"statement discoveries must also appear in aspects: {sorted(missing)}")
        return self


class CompactAspect(BaseModel):
    c: str = Field(json_schema_extra=_CATEGORY_SCHEMA)
    s: str = Field(json_schema_extra=_SUBCATEGORY_SCHEMA)
    n: NovelTopic | None = None
    p: Literal["positive", "mixed", "negative", "neutral"]
    q: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def canonical_or_discovered(self) -> CompactAspect:
        _validate_topic(self.c, self.s, self.n)
        return self


class CompactStatement(BaseModel):
    l: str = Field(json_schema_extra=_LABEL_SCHEMA)
    n: NovelTopic | None = None
    t: str

    @model_validator(mode="after")
    def canonical_or_discovered(self) -> CompactStatement:
        _validate_statement(self.l, self.n)
        return self


class ReviewEnrichmentItem(BaseModel):
    id: str
    s: Literal["positive", "mixed", "negative", "neutral"]
    i: ReviewIntent
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

    @model_validator(mode="after")
    def bucket_categories_match(self) -> ReviewEnrichmentItem:
        def expanded(values: list[CompactStatement]) -> list[Statement]:
            return [Statement(label=value.l, novel_topic=value.n, statement=value.t) for value in values]

        _validate_bucket(expanded(self.ti), "technical", "technical_issues")
        _validate_bucket(expanded(self.mo), "product", "monetization_comments")
        _validate_bucket(expanded(self.ac), "accessibility", "accessibility_comments")
        _validate_bucket(expanded(self.mu), "multiplayer", "multiplayer_comments")
        aspect_discoveries = {(value.c, value.n) for value in self.a if value.n is not None}
        statements = self.co + self.pr + self.fr + self.ti + self.mo + self.ac + self.mu
        statement_discoveries = {
            (value.l.split(".", 1)[0], value.n) for value in statements if value.n is not None
        }
        if missing := statement_discoveries - aspect_discoveries:
            raise ValueError(f"statement discoveries must also appear in aspects: {sorted(missing)}")
        return self

    def normalized(self) -> ReviewEnrichment:
        def statements(values: list[CompactStatement]) -> list[Statement]:
            return [Statement(label=value.l, novel_topic=value.n, statement=value.t) for value in values]

        return ReviewEnrichment(
            sentiment=self.s,
            review_intent=self.i,
            confidence=self.q,
            player_context=self.pc,
            aspects=[Aspect(category=value.c, subcategory=value.s, novel_topic=value.n,
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
