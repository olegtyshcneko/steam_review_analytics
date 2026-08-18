# Steam Review Intelligence plugin

This plugin connects Codex, Claude Code, or Grok Build to the local-first Steam
Review Intelligence MCP server. It supports two execution modes:

- **Harness:** the connected agent labels bounded, checkpointed review batches.
- **Provider batch:** a background worker uses `OPENROUTER_API_KEY` from the local
  environment and submits a native OpenRouter batch job.

The API key is never accepted as a skill argument or MCP tool input.

## Install from this repository

Codex uses `.agents/plugins/marketplace.json` and the `.codex-plugin` manifest.
Claude Code uses `.claude-plugin/marketplace.json` and the `.claude-plugin`
manifest. Grok Build can load the Claude Code package directly.

The bundled MCP configuration launches the server with `uvx` from the public Git
repository. Install `uv`, then add this repository as a marketplace in the target
agent and install `steam-review-intelligence`.

For provider batch mode, set the key in the environment that launches the agent:

```bash
export OPENROUTER_API_KEY='your-key-from-openrouter'
```

Do not put that key in a prompt, plugin manifest, or committed file.
