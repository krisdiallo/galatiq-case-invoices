# Small verifier strategy comparison — 2026-10-31

**Completed: 72 trials, 95 model calls, within the authorized 100-call cap.**
No network retries were needed. The held-out set was not used.

## Experiment

- Model: `grok-4.20-0309-non-reasoning`, same provider and inputs across strategies.
- 24 frozen development cases: eight one-page PDFs, one from each development family.
- Each PDF contributes one clean draft and two planted errors: **8 clean / 16 corrupted**.
  Four clean drafts use equivalent decimal spelling. Corruptions cover all 12 existing types;
  vendor, citation, omission and duplication errors appear twice.
- Sources are the first, three-item document in each family. This intentionally cheap sample
  does not measure long or dense invoices. Several sources were already seen in development.
- Selection preceded this run's outcomes. Prompts were held fixed throughout. One sample per
  case/strategy; no extractor revision and no outcome-based reruns.
- Planned calls: 48 blind transcription + 24 blind extraction + 24 batched claims = 96.
  One transcription failed coverage validation before comparison, giving 95 actual requests.

[Selected cases](small-strategy-cases.json) are directly consumable by the existing runner.

## Results

Positive means accepting the extraction. Errors remain in TPR/FPR denominators.

| Strategy | Clean accepted (TPR) | Corrupted accepted (FPR) | Errors | Calls | Median seconds |
|---|---:|---:|---:|---:|---:|
| Blind transcription | 7/8 (87.5%) | 2/16 (12.5%) | 1 | 47 | 4.60 |
| Blind structured extraction | 4/8 (50%) | 0/16 | 10 | 24 | 4.21 |
| Batched field claims | 0/8 | 0/16 | 1 | 24 | 8.89 |

Blind transcription remains the most usable of these implementations on this sample, but its
false accepts prevent a reliability claim. Neither new strategy is a replacement yet. Zero
false accepts does not establish quality when clean drafts are rejected or trials fail.

### What failed

**Blind transcription:** accepted a wrong vendor on `standard_table-001` and a wrong due date
on `totals_first-001`. One clean trial failed because the transcript did not declare complete
source coverage. It explicitly detected the other 14 planted corruptions.

**Blind structured extraction:** eight responses returned arrays of citations where the schema
requires one citation object per field; two other responses cited invalid source evidence.
The remaining trials accepted four clean drafts and detected ten corruptions. These are
output-contract failures of this implementation, not evidence that independent extraction is
inherently ineffective. Different citation locations with otherwise equal values deliberately
produce uncertainty because alternative valid mappings have not been resolved.

**Batched claims:** rejected seven clean drafts for alleged omissions such as titles, table
headers, repeated invoice headers, and purchase-order/account text outside the invoice schema.
The eighth clean draft failed exact field-coverage validation. All 16 corruptions were rejected,
but the clean results show that blanket rejection is driving the apparent zero FPR.

## Implementations and extension point

- `blind_extraction`: one source-only structured extraction request, followed by deterministic
  normalized field and citation-region comparisons. No candidate values, candidate citations,
  or candidate header context enter the independent reading request.
- `batched_claims`: one request returns a verdict and evidence for every candidate field plus
  a separate source-coverage assessment. Code validates exact field coverage and all evidence.
- Both register in `STRATEGY_METHODS` in `invoices/verification.py`, return the existing
  `VerificationResult`, and reuse chunking, revision routing, validation, approval and payment.
- Existing comparison defaults retain the original three strategies; experiments must explicitly
  select the new ones. The application still defaults to `field_by_field`.

The baseline retained prompt version `5-provider-capacity`; new strategies use
`6-small-strategies`. No prompt repairs were mixed into this comparison. This trial compares
whole implementations, so it cannot isolate a causal benefit of transcription versus a prompt
or response-schema difference. Future repairs should be evaluated as a separate version.

## Budget and artifacts

Reported usage totals 341,047 input tokens and 82,585 output tokens. Call count is not an exact
dollar budget; token usage and provider pricing determine charges. The shared limit reserves a
call atomically before network dispatch, across all trial workers and retries. No new model
requests have been made after this experiment completed.

- [Summary and paired metrics](results/small/summary.json)
- [All terminal outcomes](results/small/records.jsonl)
- [Frozen manifest](results/small/manifest.json)
- [Export provenance](results/provenance.json)

To reproduce into a new directory (this will incur new API usage):

```bash
uv run python scripts/compare_verifiers.py \
  --cases evals/small-strategy-cases.json --split dev --providers grok \
  --strategies blind_transcription blind_extraction batched_claims \
  --workers 4 --max-total-model-calls 100 --retry-network-errors \
  --output runs/small-strategy-reproduction
```

The exact evaluated code snapshot is retained locally in the ignored run directory. Published
manifests record its hashes. The current adapter subsequently removed unused source-tool handling;
that cleanup changes no active verification request behavior. With no retries, the published
terminal records contain every attempt from this experiment.
