# Chunk budget investigation

Investigated 2026-10-31. The application had several unsupported capacity estimates. These
defaults are now removed; actual provider capacity responses trigger smaller requests.
Explicit batching and request-size overrides remain available.

## What changed

| Previous default | Finding | Current behavior |
|---|---|---|
| Four pages / 24,000 source bytes per chunk | Neither measures the provider's full request capacity | Start with the full source unless an override is set |
| 128,000 estimated context tokens | Both configured Grok models document 1,000,000-token context windows | Provider enforces context; no guessed local token budget |
| UTF-8 bytes treated as tokens; 4,096 tokens per image | Unvalidated estimates can split requests that fit | Removed both estimates |
| 16 images per request | xAI documents no fixed image-count limit | No default image-count ceiling |
| 16 MB serialized request budget | No verified basis for this universal ceiling | No default byte budget |
| 16-region blind-transcription windows, plus neighbors | A reading strategy imposed as a fixed window | Read each page whole; split on capacity errors |

Official sources checked: model pages for
[Grok 4.3](https://docs.x.ai/developers/models/grok-4.3) and
[Grok 4.20 non-reasoning](https://docs.x.ai/developers/models/grok-4.20-0309-non-reasoning)
both list 1,000,000-token context windows. The
[xAI image documentation](https://docs.x.ai/developers/model-capabilities/images/understanding)
lists a 20 MiB per-image limit and no fixed image-count limit. These facts do **not** establish
unlimited request capacity, or identical limits across providers. The
[xAI token accounting FAQ](https://docs.x.ai/developers/faq/billing) also distinguishes text
tokenization from inference accounting, which includes predefined tokens.

`MODEL_CONTEXT_TOKENS` is retired and produces an actionable configuration error if set.
Optional `MODEL_CHUNK_SOURCE_BYTES`, `MODEL_CHUNK_PAGES`, `MODEL_REQUEST_BYTES`, and
`MODEL_REQUEST_IMAGES` still work when deliberately configured. Output tokens remain uncapped
by the application unless explicitly configured; provider defaults and hard limits still apply.

## Functional capacity probe

Generated a sparse 50-page invoice: 50 item rows, 55 source regions, 7,925 bytes of serialized
source. Replaying the previous chunk policy plans **13 initial chunks**. The new default plans
**one**. This planning comparison is deterministic; the old policy was not rerun against live models.

Ran one live extraction and one direct-verifier request with no budget overrides:

| Measurement | Extraction | Direct verification |
|---|---:|---:|
| Model | grok-4.20-0309-non-reasoning | grok-4.3 |
| Requests / chunks | 1 / 1 | 1 / 1 |
| Page images supplied | 0 (text source) | 50 |
| Reported prompt tokens | 4,637 | 53,523 |
| Reported completion tokens | 10,103 | 2,212 |
| Elapsed seconds | 52.5 | 36.1 |
| Capacity error / truncation | None | None |

The extractor returned all 50 items. The verifier's prompt included 38,050 image tokens.
The old image heuristic alone would have estimated 204,800 tokens, exceeding its invented
128,000-token budget even before counting text. The provider accepted the actual request.

**Verification returned incorrect.** The extractor supplied correct header values but used
`invoice.number`-style citation keys where the contract expects `number`. Merge/projection
therefore lost header evidence and omitted those fields from the local verifier candidate.
Verification detected missing header fields, and citation validation also failed. This is a
separate output-contract issue; this probe did not establish successful
end-to-end invoice approval or payment.

Published capacity artifacts: [planning data](results/capacity/planning.json),
[live responses and usage](results/capacity/live-smoke.json), and
[generated PDF](results/capacity/invoice-50-pages.pdf).

This is a capacity smoke test, **not an accuracy benchmark**. Sparse repeated rows do not
represent dense, varied invoices or establish long-document accuracy. It exercises direct
verification, not a new live comparison of every strategy. Earlier accuracy results predate
current prompt version `5-provider-capacity` and require a fresh frozen evaluation.

## Remaining tradeoffs and checks

- Actual capacity errors and truncated output still cause splitting. Splits prefer page
  boundaries, then regions, then text within a region. An irreducible failure holds processing.
- The first oversized request can reach the provider. Failed attempts and truncation can
  increase latency and usage. Accurate provider-aware accounting could improve this later.
- Fewer chunks do not guarantee lower cost or better accuracy. Field-by-field verification
  can repeat larger source contexts, and cross-page interpretation still needs evaluation.
- The 60-second timeout, 1,024-pixel page rendering, and crop padding remain unvalidated
  application choices. Blind transcription's page-by-page reading remains a strategy choice.
- No accuracy tuning or citation-contract repair was folded into this budget change.

The functional suite covers default full-source planning, explicit overrides, adaptive
capacity splitting, output truncation, complete coverage, and failure preventing payment.
See [current assumptions](../README.md#assumptions-and-improvements) for functional and business constraints.
