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

## CLI and local-model pipeline

The standalone `steam-market` CLI is an advanced workflow separate from the MCP
plugin. It covers broad catalog discovery, direct DuckDB inspection, exports,
and optional classification/enrichment through an OpenAI-compatible local
server. Local-model support applies to this CLI pipeline, not to the MCP
provider-batch mode.

See the complete [CLI and local-model guide](docs/CLI.md) for installation,
configuration, commands, local Qwen setup, analytical queries, backups, and
troubleshooting.

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
