# Steam Market CLI and Local Models

The `steam-market` CLI builds and inspects the underlying Steam review dataset.
It is an advanced workflow separate from the public MCP plugin. The MCP supports
agent harness and OpenRouter provider-batch execution; direct local-model support
belongs to this CLI pipeline.

## Prerequisites and installation

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Internet access to Steam Store and SteamSpy
- Optionally, an OpenAI-compatible server for classification and enrichment

```bash
uv sync
cp .env.example .env
# Replace STEAMID_HASH_SALT with a long random local value; never commit .env.
uv run steam-market init
uv run steam-market doctor
```

The database defaults to `data/steam_market.duckdb`. DuckDB is canonical and no
application server is needed.

## Local OpenAI-compatible model setup

The CLI does not download or launch model weights. It calls a separately managed
OpenAI-compatible HTTP server using `GET /v1/models` and
`POST /v1/chat/completions`.

The endpoint must accept strict JSON Schema in `response_format`. It must also
accept the configured `reasoning_effort` field or ignore unsupported request
fields. Compatibility therefore depends on the serving runtime, not merely on
exposing an endpoint named `/chat/completions`.

Qwen3.6-35B-A3B is the current default configuration, not a requirement. A
conceptual `llama-server` launch for that model is:

```bash
llama-server \
  --model /models/Qwen3.6-35B-A3B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8000 \
  --ctx-size 8192 --n-gpu-layers 999 \
  --alias Qwen3.6-35B-A3B
```

Replace paths and options with those supported by the installed runtime. A
high-quality 4-bit quantization was originally recommended for fitting this
35B-total/3B-active model, runtime, and KV cache into 32 GB VRAM. Actual memory
use depends on quantization, context size, and GPU offload.

Set `LLM_BASE_URL` and `LLM_MODEL` for any other compatible endpoint. The model
returned by `/v1/models` must exactly match `LLM_MODEL`.

```bash
curl http://127.0.0.1:8000/v1/models
uv run steam-market test-llm
```

`test-llm` checks both structured tasks before a real crawl. The application
sends no tools to the model and treats source review text as untrusted content.

## Configuration

All settings use environment variables and can live in `.env`. See
[`.env.example`](../.env.example) for the full list. Important controls include:

- `DUCKDB_PATH`, `MIN_REVIEWS`
- source-specific request rates and bounded retry count
- `STEAMSPY_CATALOG_PAGES` (one 1,000-entry broad page by default)
- `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`
- local-model timeout, temperature, batch size, and concurrency
- `LLM_REASONING_EFFORT=none` for deterministic extraction
- enrichment languages and minimum review length
- taxonomy and prompt versions
- `STEAMID_HASH_SALT`, applied before author IDs reach DuckDB

Review enrichment v2 uses controlled intent values and canonical
`category.topic` labels. New topics use `category.other` with a constrained
`novel_topic`, preserving discovery without fragmenting aggregate statistics.
The client sends the complete Pydantic JSON Schema, validates every response,
and retries malformed output with validation feedback.

Discovery uses SteamSpy's paged `request=all` response because the formerly used
public app-list endpoint returned HTTP 404 during implementation. SteamSpy is a
supporting source; Steam store metadata and `appreviews` remain authoritative
for type filtering and review totals.

## Acceptance dry run

Start the local model, then run:

```bash
uv run steam-market dry-run --games 5 --min-reviews 50 --seed 42
```

Candidates are shuffled from the configured catalog snapshot using the seed.
Each candidate is checked against current Steam review summaries and store type.
Qualifying games are classified, cursor-paged, enriched, and validated.

Review pages and cursors are checkpointed. Rerunning resumes an interrupted
crawl, while stable recommendation IDs prevent duplicate review records.

## Commands

```bash
uv run steam-market discover
uv run steam-market qualify --min-reviews 50 --limit 100
uv run steam-market ingest --appid 413150
uv run steam-market classify-games --min-reviews 50 --limit 10
uv run steam-market enrich-reviews --appid 413150
uv run steam-market run --min-reviews 50 --limit-games 5
uv run steam-market status
uv run steam-market validate
uv run steam-market db-info
uv run steam-market export-parquet
```

Omit `--limit-games` to process the complete configured catalog snapshot, or use
bounded repeatable batches. Pipeline options include `--skip-llm`,
`--skip-enrichment`, `--metadata-only`, `--reviews-only`, `--appid`, and
`--resume`. Missing optional metadata does not invalidate preserved reviews.

## Inspecting and querying data

Run a read-only query through the CLI:

```bash
uv run steam-market sql "SELECT appid,name,total_reviews,primary_genre FROM game_market_data ORDER BY total_reviews DESC LIMIT 20"
```

Or query DuckDB directly:

```python
import duckdb

con = duckdb.connect("data/steam_market.duckdb")
print(con.sql("SELECT count(*) FROM reviews").fetchone())
```

Useful views include `latest_game_classification`,
`latest_review_enrichment`, `qualified_games`, `game_market_data`, and
`review_analysis`.

Example analytical queries:

```sql
-- Top negative aspects by market genre
SELECT c.primary_genre, a.subcategory, count(*) AS mentions
FROM review_aspects a
JOIN latest_game_classification c USING (appid)
WHERE a.sentiment = 'negative'
GROUP BY 1,2 ORDER BY mentions DESC;

-- Positive and negative aspect mentions
SELECT subcategory,
       sum(sentiment='positive') AS positive,
       sum(sentiment='negative') AS negative
FROM review_aspects GROUP BY 1 ORDER BY negative DESC;

-- Candidate topics discovered under controlled fallbacks
SELECT category, novel_topic, count(*) AS mentions
FROM review_discovered_topics
GROUP BY 1,2 ORDER BY mentions DESC;

-- Complaints from high-playtime players
SELECT g.name, r.playtime_at_review_minutes, e.complaints
FROM reviews r JOIN games g USING(appid)
JOIN latest_review_enrichment e USING(recommendation_id)
WHERE NOT r.voted_up AND r.playtime_at_review_minutes >= 1200
ORDER BY r.playtime_at_review_minutes DESC;
```

## Backups and exports

Stop the active writer before copying the DuckDB file, or export primary tables:

```bash
uv run steam-market export-parquet --directory data/export
```

This writes `games.parquet`, `reviews.parquet`, and `review_aspects.parquet`.

## Troubleshooting

- **LLM endpoint unavailable:** launch the local server and confirm `/v1/models`.
- **Model identity mismatch:** set `LLM_MODEL` to the exact exposed identifier.
- **Malformed LLM JSON:** the client retries with schema-validation feedback up
  to `LLM_MAX_RETRIES`, then records an error and continues.
- **Unsupported structured output:** use a server that implements strict JSON
  Schema response formatting, or adapt its compatibility settings.
- **429/5xx responses:** lower the applicable request rate; clients use bounded
  retries, jitter, and `Retry-After` where available.
- **Review count mismatch:** the live summary and cursor corpus can legitimately
  differ. `validate` warns when the difference exceeds 10%.
- **Interrupted crawl:** rerun the command; saved cursors resume ingestion.
- **Large database:** run `db-info`, stop writers before copying, or export Parquet.

## Model checkpoint

Paid model testing is currently paused. Gemini 3.7 Flash is provisionally
selected for the next frozen-corpus analysis run. See the
[model checkpoint](../benchmarks/2026-08-16-enrichment-model-tombstone.md) for
the evidence and safeguards.
