# Synthetic verifier dataset v1

**60 PDF sources, 104 pages, 840 frozen candidate drafts.** This dataset is ready for paired
verifier experiments. The full development run was stopped to limit spending. A smaller comparison completed; see
[the submission results](../../README.md#evaluation-and-results).

| Split | Layout families | PDFs | Clean drafts | Corrupted drafts | Total drafts |
|---|---:|---:|---:|---:|---:|
| Development | 8 | 40 | 80 | 480 | 560 |
| Held-out test | 4 | 20 | 40 | 240 | 280 |
| Total | 12 | 60 | 120 | 720 | 840 |

Each PDF has one canonical clean draft, one equivalent numeric-format clean draft, and twelve
corrupted drafts. These are correlated observations: **840 drafts are not 840 independent
invoices**, and the two clean drafts are not independent sources. Five documents per family
is an initial experiment size, configurable in the generator, not an application limit.

## Source coverage

The generated documents have 3, 5, 8, 14, or 24 line items and span 1–6 pages. They vary amounts,
dates, suppliers, explicit USD/EUR/GBP/CAD currencies, fractional quantities, thousands separators,
repeated product names, invoice discounts, shipping, and tax. The adjustments family additionally
uses line discounts. Buyer, payment-processor, purchase-order and account information provide
irrelevant nearby values in selected layouts.

| Development families | Distinguishing presentation |
|---|---|
| standard_table | Conventional item table |
| totals_first | Totals before the line items |
| item_cards | Separate labeled block for each item |
| reversed_columns | Quantity, amount, unit price, then description |
| wrapped_descriptions | Product names span two lines |
| repeated_headers | Item tables continue with repeated headings |
| compact_ledger | Denser rows |
| adjustments | Separate unit-price, line-discount and net-amount columns |

| Held-out families | Distinguishing presentation |
|---|---|
| landscape_manifest | Landscape geometry with reordered columns |
| remittance_parties | Buyer and payment processor appear before the supplier |
| continuation_summary | Separate final summary after paginated line items |
| service_blocks | Service descriptions presented as item blocks |

Family IDs, source hashes, and all variants of a document are confined to one split. The
renderer deliberately shares basic components across families (for example service blocks
and item cards). This is a held-out combination of layouts and content, **not proof of complete
independence from development templates**. Freeze prompts and model choices before running test;
if test results guide changes, treat that version as development data and create a fresh holdout.

## Planted errors

Each corruption changes only the candidate; its source PDF and reference remain unchanged.

| Kind | Change |
|---|---|
| quantity | Add one unit to the first item |
| decimal_shift | Multiply its unit price by ten |
| swapped_columns | Exchange quantity and unit price |
| swapped_row_quantities | Exchange quantities of the first two rows |
| omitted_line | Remove the final item and its citations |
| duplicated_line | Append a copy of the first item with reused source evidence |
| wrong_citation | Point the first quantity to the invoice-number region; values remain correct |
| vendor | Replace the supplier with a buyer name |
| due_date | Substitute the issue date |
| currency | Substitute a different ISO currency |
| total | Change the total by one cent |
| consistent_wrong_numbers | Change quantity and recompute line amount, subtotal, tax and total |

The last error intentionally passes arithmetic consistency. It requires checking the source.
`clean_numeric_format` changes decimal spelling, such as `2` to `2.0000`, without changing value.

## Provenance and integrity

1. Seeded code creates the structured invoice truth **before** rendering a PDF.
2. The renderer records coordinates and printed text for each field.
3. The application PDF reader locates those coordinates in the resulting source. Every printed
   fragment must be found, including both fragments of wrapped names. Citations quote actual
   extracted regions, not model-generated text. The schema supports one citation per field;
   a wrapped name cites its first line while its continuation remains in source context.
4. Candidate mutations are applied independently to a deep copy of the reference. Labels and
   affected paths come from the known mutation and validated differences.
5. [manifest.json](manifest.json) records the seed, generator hash, source identities, counts,
   split assignments, and hashes for sources, truth files and case files. No keys or live outputs
   are included. All 104 pages were rendered and inspected in contact sheets.

[truth/](truth/) contains the structured values, render anchors and reference drafts.
[sources/](sources/) contains the PDFs. [dev-cases.json](dev-cases.json) and
[test-cases.json](test-cases.json) use the existing evaluation format. Ground truth, mutation
labels and family metadata stay in the harness; verifier requests receive only the normal
source and candidate inputs for their strategy.

The unit suite checks all file hashes, disjoint splits, all reference evidence, every label,
balanced corruptions, page bounds, deterministic PDF regeneration, overwrite refusal, and
compatibility with the evaluation harness. These checks validate the dataset, not model accuracy.

## Run and reproduce

From the repository root, compare all three strategies on development data with Grok:

```bash
uv run python scripts/compare_verifiers.py \
  --cases evals/mock-v1/dev-cases.json --split dev --providers grok \
  --output runs/mock-v1-dev-grok
```

That is **1,680 verification trials** for one model; field-by-field trials make multiple calls.
`--strategies direct blind_transcription` selects a smaller comparison. `--limit` selects the
first N cases in file order and is useful for wiring checks, not representative accuracy sampling.
No application call cap is imposed unless `--max-model-calls` is explicitly supplied.

After freezing the configuration, run the held-out split (840 trials for one model):

```bash
uv run python scripts/compare_verifiers.py \
  --cases evals/mock-v1/test-cases.json --split test --providers grok \
  --output runs/mock-v1-test-grok
```

The runner records source/case/code hashes and reports TPR/FPR, failure/abstention rates,
latency and tokens, paired comparisons, error-type breakdowns and family breakdowns. It includes
both document- and family-clustered estimates. Four held-out families still provide weak
uncertainty estimates; zero observed false accepts does not prove zero risk.

Generate another dataset without overwriting this frozen version:

```bash
uv run python scripts/build_mock_dataset.py \
  --output runs/mock-regenerated --seed 20261031 --per-family 5
uv run pytest tests/test_mock_dataset.py -q
```

The same seed and dependency versions reproduce the PDF and truth bytes. Case source paths
reflect the chosen output directory; manifest case-file hashes therefore differ across output
locations. PDF rendering is deterministic through ReportLab's invariant mode. Dependencies are
pinned in the repository lockfile. Existing output directories are rejected.

## What this does not measure

This is a verifier-only dataset: extraction and the one-revision loop are not benchmarked by
the comparison runner. It does not seed inventory or measure payment eligibility. A correct
extraction of a non-USD invoice can still fail the separate business rules.

It contains text-layer PDFs with controlled labels, not scans, OCR failures, handwriting,
photographs, arbitrary vendor templates, ambiguous currencies, or real extractor-error
frequencies. All invoices share a simple accounting model and English labels. Synthetic
corruptions are intentionally balanced by type, so aggregate precision is not a deployment
estimate. Use real invoices and real mistakes before claiming production accuracy.

The existing offline verifier only knows the original demo fixtures. Running these PDFs through
that oracle will produce fixture errors; use dataset validation tests for local checks and live
mode for actual strategy comparisons. Do not interpret offline simulation as accuracy evidence.
