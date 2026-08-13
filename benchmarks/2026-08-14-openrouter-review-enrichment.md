# OpenRouter review-enrichment benchmark — 2026-08-14

## Decision

Use **Gemini 3.7 Flash through OpenRouter Batch** for review enrichment.

It cost about three times as much as the successful portion of DeepSeek V4 Flash,
but it completed every sampled review, completed the job 2.85 times faster, produced
far fewer taxonomy and duplication defects, and won 69 of 100 blinded pairwise
quality judgments.

## Method

- Game: **Killer is Dead - Nightmare Edition** (`appid=261110`)
- Population in the local corpus: 8,721 reviews
- Benchmark sample: 1,500 eligible English reviews
- Deterministic sample seed: `42`
- Sample hash: `168056d253680586480e5149cc09d59c7ec48edf42b59a0e81c028b39c3b00fd`
- Review text: 770,692 characters
- Identical semantic batching: 188 requests, at most 8 reviews or 12,000 review
  characters per request
- DeepSeek: `deepseek/deepseek-v4-flash-0731`, no reasoning, 20 concurrent requests,
  two bounded attempts
- Gemini: `google/gemini-3.7-flash`, low reasoning, OpenRouter asynchronous Batch API
- Both used the same fully inlined strict JSON schema and the same taxonomy/prompt
- Blind quality judge: `anthropic/claude-opus-5`, 100 deterministic common-success
  reviews, candidate order deterministically randomized, enumerated 1–5 rubric

The benchmark was isolated from the production enrichment tables. Raw reviews,
provider responses, and individual judge explanations remain in ignored `data/`.
Only aggregate results are committed.

## Results

| Metric | DeepSeek V4 Flash | Gemini 3.7 Flash Batch |
|---|---:|---:|
| Valid reviews | 1,316 / 1,500 (87.73%) | 1,500 / 1,500 (100%) |
| Failed request batches | 23 / 188 | 0 / 188 |
| Wall time | 715.8 s (11m 56s) | 251.1 s (4m 11s) |
| Reported provider cost | $0.1346 | $0.3993 |
| Prompt tokens | 456,477 | 687,061 |
| Completion tokens | 337,080 | 288,469 |
| Vote/sentiment agreement | 97.40% of 883 comparable | 99.64% of 1,388 comparable |
| Empty extraction rate | 3.95% | 2.67% |
| Aspects per valid review | 3.43 | 3.26 |
| Statements per valid review | 4.26 | 3.35 |
| Invalid taxonomy aspects | 137 / 4,511 (3.04%) | 24 / 4,893 (0.49%) |
| Duplicate statements | 135 | 2 |

On the 1,316 reviews completed by both models:

- Overall-sentiment agreement was 70.67%.
- Mean exact aspect/category/sentiment Jaccard overlap was 0.488.

The large disagreement confirms that successful JSON parsing is not a sufficient
quality metric for this task.

## Blind quality evaluation

| Metric (1–5) | DeepSeek | Gemini |
|---|---:|---:|
| Faithfulness | 4.10 | 4.55 |
| Completeness | 4.03 | 4.07 |
| Taxonomy fit | 3.69 | 4.29 |
| Precision | 3.54 | 4.24 |
| Pairwise wins | 31 | 69 |

All 100 judgments validated after tightening the judge schema to an enumerated
five-point scale. Gemini's clearest advantages were taxonomy fit, faithfulness,
and non-duplicative precision. DeepSeek was competitive on completeness but tended
to over-extract, repeat statements, invent unsupported taxonomy pairs, reorder or
drop review IDs, and occasionally emit truncated or malformed JSON.

The judge cost $1.9342. This cost is intentionally separate from the provider
comparison; production enrichment does not require a judge.

## Cost projection

At the observed sample rate, Gemini Batch projects to approximately **$10.02** for
the existing 37,642 eligible-review corpus. A practical budget should be **$12** to
cover retries and corpus/tokenization variation.

DeepSeek's observed successful-output cost projects to about **$3.85**, but that
figure excludes the work needed to recover its 12.27% missing reviews and clean up
its substantially higher taxonomy/duplication defect rate. It is therefore not an
equivalent production-cost estimate.

## Caveats

- This is one game and one genre; repeat on another game before treating the exact
  quality margins as universal.
- `source_voted_up` is only a coarse sentiment sanity check, not a gold label.
- The blinded evaluator is still a model. Candidate order was randomized and both
  systems were judged under the same rubric, but human annotation would be stronger.
- Gemini 3.7 was released the same day as this benchmark. Recheck provider stability
  before a very large submission.
- OpenRouter Batch initially rejected the Pydantic schema containing `$defs`/`$ref`.
  A fully inlined equivalent strict schema succeeded and is used by the harness.

## Reproduce

Keep the OpenRouter key only in ignored `.env` as `LLM_API_KEY` or export
`OPENROUTER_API_KEY`, then run:

```bash
uv run python scripts/benchmark_openrouter.py \
  --appid 261110 \
  --sample-size 1500 \
  --seed 42 \
  --concurrency 20 \
  --judge-size 100 \
  --judge-concurrency 10 \
  --output-dir data/benchmarks/openrouter-2026-08-14
```

To rerun only aggregate analysis and the blind judge from saved provider outputs:

```bash
uv run python scripts/benchmark_openrouter.py \
  --appid 261110 --sample-size 1500 --seed 42 \
  --judge-size 100 \
  --output-dir data/benchmarks/openrouter-2026-08-14 \
  --resume
```
