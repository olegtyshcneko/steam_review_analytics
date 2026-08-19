from __future__ import annotations

import pytest

from games_analytics.config import Settings
from games_analytics.database import Database


@pytest.fixture
def settings(tmp_path):
    return Settings(
        duckdb_path=tmp_path / "test.duckdb",
        steamid_hash_salt="fixture-secret",
        steam_requests_per_second=10_000,
        steamspy_requests_per_second=10_000,
        http_max_retries=1,
    )


@pytest.fixture
def db(settings):
    value = Database(settings.duckdb_path)
    value.initialize()
    yield value
    value.close()
