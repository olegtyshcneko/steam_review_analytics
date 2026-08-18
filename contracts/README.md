# Structured contracts

These JSON documents make the prompt input and output shapes inspectable without
reading Python implementation code:

- `review-enrichment-v2.input.schema.json`
- `review-enrichment-v2.output.schema.json`
- `game-classification-v1.input.schema.json`
- `game-classification-v1.output.schema.json`
- `examples/review-enrichment-v2.json`

The review schemas are the same contract returned by the MCP
`get_analysis_contract` tool and used for provider structured output. Regenerate
all documents after changing a Python model or taxonomy:

```bash
uv run python scripts/export_contracts.py
```

Tests fail when committed generated documents drift from their Python source.
