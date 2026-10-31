# Live verifier evaluation — 2026-10-31

Completed 162 live trials: three models × three strategies × 18 frozen drafts. No corrupted draft was accepted. Blind transcription accepted every clean draft in this small development experiment; field-by-field checking produced extra false rejections and substantially more calls. This is not sufficient evidence to select a production winner.

## Combined results across models

Positive means an extraction is accepted. TPR is clean drafts accepted; FPR is corrupted drafts accepted. Errors and uncertainty remain in denominators. Each strategy has nine clean trials and 45 corrupted trials, drawn from only three source documents.

| Strategy | TPR | FPR | Explicit error detection | False accusations | Uncertain / errors | Median seconds | API calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| Direct | 7/9 (77.8%) | 0/45 | 43/45 | 0/9 | 2 / 2 | 2.6 | 54 |
| Field-by-field | 6/9 (66.7%) | 0/45 | 43/45 | 3/9 | 0 / 2 | 18.0 | 770 |
| Blind transcription | 9/9 (100.0%) | 0/45 | 44/45 | 0/9 | 1 / 0 | 5.4 | 126 |

## Results by model

Each row uses the same three clean drafts and 15 corruptions. Latency is per invoice verification under six concurrent trial workers; it is not isolated provider latency.

| Model | Strategy | Clean accepted | Incorrect accepted | Explicit error detection | Errors | Median seconds | Input / output tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| Grok 4.20 non-reasoning | Direct | 2/3 | 0/15 | 15/15 | 1 | 1.7 | 47,539 / 5,178 |
| Grok 4.20 non-reasoning | Field-by-field | 1/3 | 0/15 | 15/15 | 0 | 15.5 | 495,574 / 22,503 |
| Grok 4.20 non-reasoning | Blind transcription | 3/3 | 0/15 | 15/15 | 0 | 3.6 | 89,561 / 10,539 |
| GPT-4.1 mini | Direct | 2/3 | 0/15 | 13/15 | 1 | 2.6 | 50,053 / 6,714 |
| GPT-4.1 mini | Field-by-field | 3/3 | 0/15 | 13/15 | 2 | 18.0 | 509,669 / 25,612 |
| GPT-4.1 mini | Blind transcription | 3/3 | 0/15 | 14/15 | 0 | 4.9 | 94,877 / 10,912 |
| Claude Haiku 4.5 | Direct | 3/3 | 0/15 | 15/15 | 0 | 3.5 | 58,733 / 5,783 |
| Claude Haiku 4.5 | Field-by-field | 2/3 | 0/15 | 15/15 | 0 | 30.9 | 568,055 / 30,521 |
| Claude Haiku 4.5 | Blind transcription | 3/3 | 0/15 | 15/15 | 0 | 6.4 | 105,045 / 11,272 |

## What failed

- **Grok, field-by-field:** rejected clean TXT quantities because their citations quoted the full source line; rejected clean JSON quantities because the draft used decimal strings while the source used numeric literals. Both are valid representations in this system.
- **Claude, field-by-field:** rejected the clean PDF even though its findings described the numeric normalization as acceptable. A contradictory explanation is still a negative result; no reviewer silently overrode it.
- **Direct verification:** Grok omitted required field coverage on the clean PDF; OpenAI omitted source coverage on the clean JSON. Both raw positive verdicts were blocked by application validation.
- **OpenAI:** direct verification abstained on the TXT and JSON omitted-line variants; blind transcription also abstained on the JSON omitted-line variant. These were held, not accepted.
- **OpenAI, field-by-field:** one API connection failure and one invalid-evidence response caused failed trials. No retries were substituted into the scores.

## Experiment and provenance

- Sources: `invoice_1001.txt`, `invoice_1004.json`, and `invoice_1011.pdf`. Each has a clean draft and five planted corruptions: quantity, omitted line, wrong citation, vendor, and mutually consistent wrong numbers.
- Development references were checked against source text. The TXT/PDF currency was changed from assumed USD to null because `$` alone does not establish USD. Labels are not independent human gold; the original fixture and held-out test sets were not changed.
- One sample per combination. Extraction and the one-revision loop were **not measured**; every strategy received the same frozen candidate drafts.
- Final verifier prompt version **3**: explicit field checklist, correct invoice-versus-item question wording, and **no verifier tools**. PDF verification includes rendered page images; field and blind calls also receive focused crops.
- Source-reading pilots exposed an eight-call batch cap and ambiguous field paths. Superseded pilot runs were interrupted and later removed during repository cleanup. Their scores are excluded from this final comparison; the completed comparison artifacts below are retained.
- Anthropic used its [official Chat Completions compatibility endpoint](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk) with strict JSON schema; xAI/OpenAI used JSON mode. All outputs had the same local Pydantic and coverage validation. This transport difference is another experimental variable.
- Models: `grok-4.20-0309-non-reasoning`, `gpt-4.1-mini-2025-04-14`, `claude-haiku-4-5-20251001`. See the manifest for provider URLs and source-code hashes.

## Limits on conclusions

The 162 trials do not represent 162 independent invoices: there are only three source documents. The corruptions are synthetic, no scanned PDFs were tested, and repeated sampling was not performed. Zero observed false accepts does not establish zero deployment risk. Clustered intervals and paired comparisons are saved, but three clusters provide very weak uncertainty estimates. Field-localization scores also depend on exact path granularity.

Next evaluation: independently label a larger, unseen collection of real extractor mistakes, including multi-page invoices, ambiguous columns, repeated items, OCR problems, and subtle value substitutions. Freeze prompts before using the held-out split. Keep the requested field-by-field default while evaluating; this experiment alone does not justify switching it.

## Artifacts and reproduction

- [Full summary and paired comparisons](results/initial/summary.json)
- [Every trial and raw model response](results/initial/records.jsonl)
- [Frozen configuration and code hashes](results/initial/manifest.json)
- [Development cases](live-dev-cases.json)
- [Prototype limits and assumptions](../README.md#assumptions-and-improvements)

```bash
uv run python scripts/compare_verifiers.py --workers 6 --output runs/another-comparison
```

Credentials are read from ignored `.env` or the environment. xAI, OpenAI, and Anthropic keys were validated; no key values are included in artifacts. Final-run usage excludes earlier pilots, interrupted requests, and any provider-side charges not returned in API usage. Dollar cost was not estimated.
