# Mock v1: partial development comparison

Stopped on 2026-10-31 to limit API spending. **289 of 1,680 planned outcomes saved.**
The held-out split was not run. No comparison process remains active.

The original run suffered mass connection failures. Recovery retained its completed verdicts
and non-transport validation failures, and retried connection/reset/timeout failures separately.
All original and recovery attempts are preserved. A second connection switch triggered the
recovery pause; the runner resumed successfully before being stopped for budget reasons.

## Matched outcomes only

94 cases have outcomes from all three strategies, spanning **seven PDFs from two families**
(standard table and totals first). The runner processes cases in file order, so this is an
unbalanced development subset, not a representative sample of the 12-family corpus.

| Strategy | Correct drafts accepted | Corrupted drafts accepted | Operational/validation errors |
|---|---:|---:|---:|
| Direct | 13/13 | 36/81 | 0 |
| Field-by-field | 5/13 | 0/81 | 11 |
| Blind transcription | 13/13 | 19/81 | 4 |

Errors remain in the acceptance denominators. These recovered outcomes do not measure
first-attempt reliability. Field-by-field's zero false accepts must be considered alongside
its clean rejections, errors and call usage; this does not establish a winning strategy.

## Recorded usage

Across the original records and completed recovery attempts: **6,659 recorded API attempts**,
18,715,553 reported input tokens and 637,203 reported output tokens. These include failed and
retried trials; they are not all billable successes. Interrupted in-flight trials may have
incurred additional usage not yet written to the record. Provider billing is authoritative;
no dollar charge is inferred here.

A verification trial is not one API call. Field-by-field checks each populated field plus
coverage, so long drafts multiply the request count. Retrying a transport-interrupted trial
can repeat earlier successful field calls. A cheaper next experiment should select a small,
explicitly balanced set before dispatch, then expand only to resolve specific uncertainty.
No additional model calls are needed to inspect the existing findings.

## Published evidence

- [Recovered outcomes, findings and recorded usage](results/partial/records.jsonl)
- [Partial metrics](results/partial/summary.json)
- [Recovery configuration and source hashes](results/partial/manifest.json)
- [Export provenance](results/provenance.json)

The public partial records omit verbose request logs and partial transcripts. Original network
failures and all recovery attempts remain in the local ignored run directories; they are not
required to reproduce the matched outcome table. The report's total-attempt usage includes
those original/retried requests, so it is larger than usage in the public terminal outcomes.
