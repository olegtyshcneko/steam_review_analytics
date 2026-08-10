from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    duckdb_path: Path = Path("data/steam_market.duckdb")
    min_reviews: int = Field(50, ge=0)
    steam_language: str = "all"
    steam_reviews_per_page: int = Field(100, ge=1, le=100)
    steam_include_offtopic: bool = True
    steam_requests_per_second: float = Field(1.0, gt=0)
    steamspy_enabled: bool = True
    steamspy_requests_per_second: float = Field(1.0, gt=0)
    steamspy_catalog_pages: int = Field(1, ge=1, le=100)
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_api_key: str = "local"
    llm_model: str = "Qwen3.6-35B-A3B"
    llm_temperature: float = Field(0.1, ge=0, le=2)
    llm_reasoning_effort: str = "none"
    llm_timeout_seconds: float = Field(180, gt=0)
    llm_concurrency: int = Field(1, ge=1)
    llm_batch_size: int = Field(8, ge=1, le=32)
    llm_batch_max_characters: int = Field(12000, ge=1000, le=50000)
    enrich_languages: Annotated[list[str], NoDecode] = ["english"]
    enrich_min_characters: int = Field(40, ge=1)
    enrichment_version: str = "v1"
    genre_taxonomy_version: str = "v1"
    steamid_hash_salt: str = "change-me"
    http_max_retries: int = Field(5, ge=0)
    llm_max_retries: int = Field(3, ge=1)
    log_level: str = "INFO"

    @field_validator("enrich_languages", mode="before")
    @classmethod
    def parse_languages(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("llm_reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str) -> str:
        allowed = {"none", "low", "medium", "high", "max"}
        if value not in allowed:
            raise ValueError(f"must be one of {sorted(allowed)}")
        return value

    def ensure_directories(self) -> None:
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def llm_models_url(self) -> str:
        return f"{self.llm_base_url.rstrip('/')}/models"
