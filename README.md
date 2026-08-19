# Games Analytics

A local-first Python CLI, MCP server, and agent plugin for mining and analyzing
public game reviews across:

- Steam
- Google Play
- Apple App Store

The pipeline prioritizes negative reviews to reveal unmet needs, uses positive
reviews as design constraints, labels feedback with a controlled structured
contract, compares games, and renders JSON plus self-contained HTML reports.
Labeling can run through the connected agent harness or a user-funded OpenRouter
batch job.

## Architecture

Store-specific behavior is isolated under `src/games_analytics/platforms/`:

- `steam.py` — Steam Store reviews, metadata, and SteamSpy catalog data
- `google_play.py` — public Google Play storefront metadata and review transport
- `app_store.py` — Apple lookup metadata and public storefront review feeds
- `base.py` — shared rate limiting, retries, and verified HTTP transport

All platforms feed normalized DuckDB tables and the `cross_platform_reviews`
view. The same [versioned input/output contracts](contracts/) are used by MCP,
agent harnesses, and OpenRouter provider batches.

The former `steam_market` Python namespace and `steam-market` commands remain as
deprecated compatibility aliases for existing users. New integrations should
use `games_analytics` and the `games-analytics*` commands.

## Quick start

```bash
uv sync
cp .env.example .env
uv run games-analytics init
```

Mine a bounded mobile corpus without API credentials:

```bash
uv run games-analytics mine-store \
  --platform google-play \
  --product-id com.pabloleban.IdleSlayer \
  --country us --language en --max-reviews 500

uv run games-analytics mine-store \
  --platform app-store \
  --product-id 1526599527 \
  --country us --max-reviews 500
```

Steam ingestion remains available:

```bash
uv run games-analytics ingest --appid 413150
```

See the complete [CLI and local-model guide](docs/CLI.md) for Steam discovery,
mobile mining, DuckDB queries, exports, and optional local-model enrichment.
Store-specific access modes and limits are documented in
[platform collection notes](docs/PLATFORMS.md).

## Agent plugin and MCP

The `games-analytics` plugin supports Codex, Claude Code, and Grok Build. Its MCP
offers separate ingestion tools for Steam and mobile storefronts, then a shared
resumable labeling and report workflow.

### Codex

```bash
codex plugin marketplace add olegtyshcneko/games_analytics
```

Install **Games Analytics** from the added marketplace.

### Claude Code

```bash
claude plugin marketplace add olegtyshcneko/games_analytics
claude plugin install games-analytics@games-analytics
```

### Run MCP directly

```bash
uv run games-analytics-mcp --transport stdio
```

For local HTTP development only:

```bash
uv run games-analytics-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

Do not expose the HTTP transport publicly before adding authentication,
authorization, quotas, and deployment-origin controls.

## Collection boundaries

The official Google Play Developer and App Store Connect review APIs require
developer authentication and are intended for apps you control. Competitor
research therefore uses public storefront transports. Those public surfaces are
best-effort and can change without notice; collectors use bounded rates, retries,
and fixture tests, but live smoke tests remain important.

Google Play collection supports continuation-token pagination. Apple public
feeds expose at most ten 50-review pages per country storefront, so a storefront
normally yields no more than roughly 500 currently visible reviews. Country and
language are explicit because mobile review visibility varies by storefront.

Reviewer names and profile images are not retained for mobile reviews. Steam IDs
are hashed and removed from stored raw payloads. See [PRIVACY.md](PRIVACY.md) and
[SECURITY.md](SECURITY.md).

## Execution modes

- **Harness:** the connected Codex, Claude, or Grok agent labels bounded review
  batches and checkpoints strict v2 output after every batch.
- **Provider batch:** a detached worker reads `OPENROUTER_API_KEY` from its local
  environment. The key is never accepted as an MCP argument.

## Tests

```bash
uv run pytest
```

Fixture tests require no storefront access or model. Live mobile smoke tests are
performed separately because public storefront responses can drift.

Existing example reports remain under [reports/](reports/), and provider model
benchmarks remain under [benchmarks/](benchmarks/).
