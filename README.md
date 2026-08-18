# Steam Review Analytics

A local-first Python CLI and MCP server that downloads public Steam reviews,
extracts controlled structured feedback, compares player needs across games, and
generates JSON plus self-contained HTML reports. Analysis can run through the
connected agent harness or through a user-funded OpenRouter batch job.

The complete product contract is in [PRD.md](PRD.md). This repository implements
the durable dataset, structured review contract, reference analyses, public MCP
workflow, and installable Codex/Claude plugin package.

## Agent plugin and MCP

The `steam-review-intelligence` plugin is packaged once with shared skills and
separate Codex and Claude manifests. Grok Build can consume the Claude package.

The workflow is resumable:

1. Ingest one or more Steam app IDs into local DuckDB.
2. Create a negative-first analysis corpus with a deterministic positive sample.
3. Label reviews through the current agent or an OpenRouter native batch job.
4. Aggregate stable categories and constrained discovered topics.
5. Have the agent synthesize detailed findings and render an HTML report.

### Codex

```bash
codex plugin marketplace add olegtyshcneko/steam_review_analytics
```

Then install **Steam Review Intelligence** from that marketplace in the Codex
plugin directory. The repository marketplace is `.agents/plugins/marketplace.json`.

### Claude Code

```bash
claude plugin marketplace add olegtyshcneko/steam_review_analytics
claude plugin install steam-review-intelligence@steam-review-intelligence
```

Grok Build can add the same repository and load its Claude Code marketplace and
plugin metadata.

### Run the MCP directly

```bash
uv sync
uv run steam-review-mcp --transport stdio
```

For local HTTP development:

```bash
uv run steam-review-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The HTTP transport is not configured as a public hosted service. Do not expose it
to the internet before adding OAuth, authorization, quotas, and deployment origin
controls.

### Execution modes

- **Harness mode:** the connected Codex, Claude, or Grok agent requests bounded
  review batches and checkpoints strict v2 labels after each batch.
- **Provider batch mode:** a detached local worker reads `OPENROUTER_API_KEY` from
  its environment and submits the corpus through OpenRouter's batch API. The key
  is never accepted as an MCP tool argument.

Configure provider batch mode only in a local environment file or shell:

```bash
export OPENROUTER_API_KEY='your-key-from-openrouter'
```

See [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md) before distributing or
deploying the server.

## Prerequisites and install

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Internet access to Steam Store and SteamSpy
- For classification/enrichment: an OpenAI-compatible local server exposing
  `Qwen3.6-35B-A3B`
- Recommended inference hardware: NVIDIA RTX 5090 with 32 GB VRAM

```bash
uv sync
cp .env.example .env
# Replace STEAMID_HASH_SALT with a long random local value; never commit .env.
uv run steam-market init
uv run steam-market doctor
```

The database defaults to `data/steam_market.duckdb`. DuckDB is canonical and no
application server is needed.

## Local Qwen setup

The application deliberately does not download or launch model weights. Use a
current Qwen-compatible OpenAI HTTP server, such as a current `llama-server`
build with a reputable high-quality 4-bit GGUF quantization. A 4-bit class build
is recommended so the 35B-total/3B-active model, runtime, and KV cache fit within
32 GB. Exact VRAM use depends on quantization, context, and GPU offload.

Example conceptual launch (replace paths/options with those supported by the
installed current server build):

```bash
llama-server \
  --model /models/Qwen3.6-35B-A3B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8000 \
  --ctx-size 8192 --n-gpu-layers 999 \
  --alias Qwen3.6-35B-A3B
```

The model exposed by `GET /v1/models` must exactly match `LLM_MODEL`. Verify both
structured tasks before a real crawl:

```bash
curl http://127.0.0.1:8000/v1/models
uv run steam-market test-llm
```

If the installed runtime uses different flags or a different model filename,
keep the API contract and alias unchanged. The application sends no tools to the
model and treats all source text as untrusted content.

## Configuration

All settings use environment variables and can live in `.env`. See
[.env.example](.env.example) for the full list. Important controls include:

- `DUCKDB_PATH`, `MIN_REVIEWS`
- source-specific request rates and bounded retry count
- `STEAMSPY_CATALOG_PAGES` (one 1,000-entry broad page by default)
- local endpoint, exact model ID, timeout, temperature, bounded batch size, and concurrency
- `LLM_REASONING_EFFORT=none` for deterministic extraction without unnecessary hidden reasoning
- review enrichment v2 uses controlled intent values and canonical `category.topic` labels
- genuinely new topics use `category.other` plus a constrained `novel_topic`, keeping statistics stable while preserving discovery
- prompts omit empty optional arrays and constrain normalized statements to keep local inference efficient
- the OpenAI-compatible endpoint receives the full Pydantic JSON Schema through `response_format`
- bounded batches use a compact validated wire schema and expand to the full normalized database model
- languages and minimum review length eligible for enrichment
- taxonomy/prompt enrichment versions
- `STEAMID_HASH_SALT`, used before author IDs ever reach DuckDB

The live app-list endpoint formerly commonly used for public discovery returned
HTTP 404 during implementation. The isolated discovery adapter therefore uses
SteamSpy's paged `request=all` response. SteamSpy is an external supporting
source; broad pages should be refreshed sparingly. Steam's store metadata and
`appreviews` endpoint remain authoritative for type filtering and review totals.

## Dry run

Start the local model first, then run the exact phase-1 acceptance test:

```bash
uv run steam-market dry-run --games 5 --min-reviews 50 --seed 42
```

Candidates are shuffled from the configured broad catalog snapshot with the
given seed. Each candidate is checked against Steam's current review summary and
store type. Five qualifying games are then classified, fully cursor-paged,
enriched, and validated. The selected app IDs and seed are stored in the run
configuration. A repeat using the same upstream snapshot and seed aims to select
the same games.

Review downloads are upserted page by page, with the next cursor committed to
`ingestion_checkpoints`. Interrupting and rerunning safely resumes the page crawl.
The same review may be seen again around an update boundary, but its stable
`recommendation_id` prevents duplicates.

## Other workflows

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

Omit `--limit-games` to process the full configured catalog snapshot, or use
bounded repeatable batches. Production options include `--skip-llm`,
`--skip-enrichment`, `--metadata-only`, `--reviews-only`, `--appid`, and
`--resume`. Missing optional metadata does not invalidate preserved review data.

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

-- Candidate topics discovered under controlled category.other fallbacks
SELECT category, novel_topic, count(*) AS mentions
FROM review_discovered_topics
GROUP BY 1,2 ORDER BY mentions DESC;

-- Complaints from high-playtime players
SELECT g.name, r.playtime_at_review_minutes, e.complaints
FROM reviews r JOIN games g USING(appid)
JOIN latest_review_enrichment e USING(recommendation_id)
WHERE NOT r.voted_up AND r.playtime_at_review_minutes >= 1200
ORDER BY r.playtime_at_review_minutes DESC;

-- Games by review band and genre
SELECT primary_genre,
  CASE WHEN total_reviews>=20000 THEN '20k+' WHEN total_reviews>=5000 THEN '5k-20k'
       WHEN total_reviews>=1000 THEN '1k-5k' WHEN total_reviews>=500 THEN '500-1k'
       WHEN total_reviews>=100 THEN '100-500' WHEN total_reviews>=50 THEN '50-100'
       ELSE '<50' END review_band, count(*) games
FROM game_market_data GROUP BY 1,2 ORDER BY 1,2;
```

The useful views are `latest_game_classification`,
`latest_review_enrichment`, `qualified_games`, `game_market_data`, and
`review_analysis`.

## Enrichment model status

Paid model testing is currently paused. Gemini 3.7 Flash is provisionally
selected for the next run, whose purpose is to enrich and analyze a frozen game
corpus rather than conduct another model bake-off. See the
[model checkpoint](benchmarks/2026-08-16-enrichment-model-tombstone.md) for the
evidence, safeguards, and next-run objective.

## Backups and exports

Stop the active writer and copy the `.duckdb` file, or export the primary tables:

```bash
uv run steam-market export-parquet --directory data/export
```

This writes `games.parquet`, `reviews.parquet`, and `review_aspects.parquet`.

## Troubleshooting

- **LLM endpoint unavailable:** launch the local server and confirm `/v1/models`.
- **Model identity mismatch:** give the server the alias in `LLM_MODEL`, or set
  `LLM_MODEL` to the exact locally exposed Qwen identifier. Enrichment records the
  configured value.
- **Malformed LLM JSON:** the client supplies the Pydantic schema and parse error,
  retries up to `LLM_MAX_RETRIES`, then records an error and continues.
- **429/5xx responses:** clients back off with jitter, honor `Retry-After`, and
  stop after `HTTP_MAX_RETRIES`; lower the relevant requests-per-second setting.
- **Review count mismatch:** Steam's live summary and cursor corpus can legitimately
  differ. `validate` reports expected, actual, absolute, and percentage differences
  and warns when the gap exceeds 10%.
- **Interrupted crawl:** rerun the same ingestion. The saved cursor resumes and
  review upserts make repetition safe.
- **Large database:** run `db-info`, stop writers before copying, or export Parquet.

## Tests

Normal tests use fixtures and mocked HTTP/LLM behavior; they need neither Steam nor
a running model.

```bash
uv run pytest
```

## Provider benchmarks

The reproducible OpenRouter review-enrichment harness is
`scripts/benchmark_openrouter.py`. Raw samples and responses stay under ignored
`data/`; aggregate benchmark reports are committed under `benchmarks/`.

Current result: [Gemini 3.7 Flash Batch vs DeepSeek V4 Flash](benchmarks/2026-08-14-openrouter-review-enrichment.md).
