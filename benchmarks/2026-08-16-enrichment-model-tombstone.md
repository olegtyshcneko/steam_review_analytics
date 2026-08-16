# Review-enrichment model checkpoint

Date: 2026-08-16

## Status

Further paid model experimentation is paused. `google/gemini-3.7-flash` is the
provisional model for the next corpus-enrichment run, but it must not be invoked
again merely for another bake-off. The next paid run should have the concrete
goal of enriching a selected game corpus and producing game-analysis outputs.

This is a pause checkpoint, not removal of Gemini support. Raw provider outputs
remain local and ignored by Git.

## Decision evidence

The v2 contract uses controlled review intent values, canonical
`category.topic` labels, and a constrained `category.other + novel_topic`
discovery lane.

On the same 100-review sample from *Killer is Dead*:

| Result | Gemini 3.7 Flash | DeepSeek v4 Flash |
|---|---:|---:|
| First-pass valid reviews | 97/100 | 73/100 |
| Valid after one bounded retry | 100/100 | 80/100 |
| Reported enrichment cost | $0.0611 | $0.0148 |
| Duplicate statements | 0 | 10 |
| Vote/sentiment agreement | 98.9% | 96.3% |

DeepSeek remained unreliable after explicit correction feedback: it fabricated
recommendation IDs, returned empty or malformed responses, used invalid
category/topic pairs, and mishandled discovery fields. Its lower nominal cost
does not compensate for incomplete corpus coverage and repair complexity.

Gemini required per-item validation and one bounded retry. The v2 schema also
increased prompt overhead, so cost and batch size should be optimized before the
full corpus run.

## Next-run objective

The next run should:

1. Select and freeze the target game corpus and review eligibility rules.
2. Estimate request count and cost before submission.
3. Enrich the corpus with Gemini 3.7 Flash using v2, per-item salvage, resumable
   batches, and bounded retries.
4. Persist results under `enrichment_version=v2` without overwriting v1 data.
5. Produce analysis across games: sentiment by canonical topic, recurring
   complaints and praises, player-context segments, and candidate novel topics.
6. Report coverage, failures, retries, actual cost, and a manually inspected
   quality sample before treating the corpus as analysis-ready.

Until that objective is started, the selected model remains paused and no more
paid benchmark runs are warranted.
