# Privacy

Steam Review Analytics is currently a local-first tool and does not operate a
hosted service. Its default MCP plugin stores its database and analysis jobs on
the user's machine.

## Data processed

The application downloads public Steam game metadata and public review content.
It retains review text and review metadata needed for aggregate analysis. Steam
author IDs are hashed before storage and are removed from the retained source
payload. Generated public reports contain aggregate statistics and short,
model-normalized evidence rather than author identities or complete raw reviews.

## Model processing

Harness mode sends selected review text to the model provider used by the user's
agent. Provider batch mode sends selected review text to OpenRouter and the model
selected for that job. Those providers' policies apply to data they process.

## Credentials

Provider batch mode reads `OPENROUTER_API_KEY` from the local process environment.
The MCP tools do not accept API keys as arguments, and generated artifacts must
not contain credentials. Users should configure provider spending limits and
rotate any key that has been disclosed in chat or committed to source control.

## Deletion

Local data can be deleted by removing the configured DuckDB file and
`ANALYSIS_JOBS_PATH`. Stop MCP and CLI processes before removing an active
database.
