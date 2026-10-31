"""Paired, frozen-draft evaluation. Offline results test plumbing, not model accuracy."""

import argparse
import copy
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from .agents import FIXTURES
from .cli import ROOT, components, model_arguments
from .models import Draft, fields
from .source import read_source
from .verification import BASELINE_STRATEGIES, STRATEGIES, VerificationRequest, validate_result


def build_cases(output):
    fixtures = json.loads(FIXTURES.read_text())
    cases = []
    for digest, fixture in fixtures.items():
        gold = fixture["draft"]
        group = gold["invoice"]["number"]
        split = "dev" if group in {"INV-1001", "INV-1004", "INV-1011"} else "test"
        variants = [("clean", copy.deepcopy(gold))]
        changed = copy.deepcopy(gold)
        changed["invoice"]["items"][0]["quantity"] = (
            "2" if str(gold["invoice"]["items"][0]["quantity"]) != "2" else "20"
        )
        variants.append(("quantity", changed))
        changed = copy.deepcopy(gold)
        changed["invoice"]["items"].pop()
        changed["citations"] = {
            k: v
            for k, v in changed["citations"].items()
            if not k.startswith(f"items.{len(changed['invoice']['items'])}.")
        }
        variants.append(("omitted_line", changed))
        changed = copy.deepcopy(gold)
        changed["citations"]["items.0.quantity"] = copy.deepcopy(gold["citations"]["total"])
        variants.append(("wrong_citation", changed))
        changed = copy.deepcopy(gold)
        changed["invoice"]["vendor"] = "Incorrect Vendor Ltd."
        variants.append(("vendor", changed))
        changed = copy.deepcopy(gold)
        from decimal import Decimal

        invoice = changed["invoice"]
        invoice["items"][0]["quantity"] = str(Decimal(invoice["items"][0]["quantity"]) + 1)
        subtotal = Decimal(0)
        for item in invoice["items"]:
            amount = Decimal(item["quantity"]) * Decimal(item["unit_price"])
            if item["amount"] is not None:
                item["amount"] = str(amount)
            subtotal += amount
        if invoice["subtotal"] is not None:
            invoice["subtotal"] = str(subtotal)
        invoice["total"] = str(
            subtotal
            + Decimal(invoice["tax"] or 0)
            + Decimal(invoice["shipping"] or 0)
            - Decimal(invoice["discount"] or 0)
        )
        variants.append(("consistent_wrong_numbers", changed))
        for kind, draft in variants:
            gold_fields = fields(Draft.model_validate(gold).invoice)
            candidate_fields = fields(Draft.model_validate(draft).invoice)
            errors = [
                k
                for k in set(gold_fields) | set(candidate_fields)
                if gold_fields.get(k) != candidate_fields.get(k)
            ]
            if kind == "wrong_citation":
                errors = ["items.0.quantity"]
            cases.append(
                {
                    "id": fixture["file"] + ":" + kind,
                    "source": str(Path("data/invoices") / fixture["file"]),
                    "source_hash": digest,
                    "group": group,
                    "split": split,
                    "kind": kind,
                    "correct": kind == "clean",
                    "label_status": "curated_demo_not_independently_reviewed",
                    "gold": gold,
                    "draft": draft,
                    "error_fields": sorted(errors),
                }
            )
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases, indent=2) + "\n")
    return cases


def ratio(num, den):
    return num / den if den else None


def metrics(rows):
    correct = [r for r in rows if r["correct"] is True]
    incorrect = [r for r in rows if r["correct"] is False]
    accepted = [r for r in correct + incorrect if r["verdict"] == "verified"]
    tp = sum(r["verdict"] == "verified" for r in correct)
    fp = sum(r["verdict"] == "verified" for r in incorrect)
    times = sorted(r["seconds"] for r in rows)
    confusion = {}
    for label, subset in [
        ("correct", correct),
        ("incorrect", incorrect),
        ("ambiguous", [r for r in rows if r["correct"] is None]),
    ]:
        confusion[label] = {
            verdict: sum(r["verdict"] == verdict for r in subset)
            for verdict in ("verified", "incorrect", "uncertain", "error")
        }
    predicted = sum(len(set(r.get("finding_fields", []))) for r in incorrect + correct)
    true_fields = sum(len(set(r.get("error_fields", []))) for r in incorrect)
    matched = sum(
        len(set(r.get("finding_fields", [])) & set(r.get("error_fields", []))) for r in incorrect
    )
    revised = [r for r in rows if r.get("revision_attempted")]
    revised_correct = [r for r in revised if r["correct"] is True]
    return {
        "cases": len(rows),
        "confusion": confusion,
        "tpr": ratio(tp, len(correct)),
        "fpr": ratio(fp, len(incorrect)),
        "acceptance_precision": ratio(tp, len(accepted)),
        "error_detection_rate": ratio(
            sum(r["verdict"] == "incorrect" for r in incorrect), len(incorrect)
        ),
        "false_accusation_rate": ratio(
            sum(r["verdict"] == "incorrect" for r in correct), len(correct)
        ),
        "abstention_rate": ratio(sum(r["verdict"] == "uncertain" for r in rows), len(rows)),
        "operational_failure_rate": ratio(sum(r["verdict"] == "error" for r in rows), len(rows)),
        "decision_coverage": ratio(
            sum(r["verdict"] in {"verified", "incorrect"} for r in rows), len(rows)
        ),
        "field_precision": ratio(matched, predicted),
        "field_recall": ratio(matched, true_fields),
        "revision_success_all_incorrect": ratio(
            sum(
                r.get("final_correct") is True
                and r.get("final_verdict") == "verified"
                and r.get("revision_attempted")
                for r in incorrect
            ),
            len(incorrect),
        ),
        "revision_damage": ratio(
            sum(r.get("final_correct") is False for r in revised_correct), len(revised_correct)
        ),
        "final_escaped_error_rate": ratio(
            sum(
                r.get("final_correct") is False and r.get("final_verdict") == "verified"
                for r in rows
            ),
            len(rows),
        ),
        "latency_p50": statistics.median(times) if times else None,
        "latency_p95": times[max(0, math.ceil(len(times) * 0.95) - 1)] if times else None,
        "model_calls": sum(r.get("calls", 0) for r in rows),
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
    }


def grouped_intervals(rows, samples=500):
    groups = defaultdict(list)
    for row in rows:
        groups[row["group"]].append(row)
    if len(groups) < 2:
        return {"tpr": None, "fpr": None}
    rng = random.Random(31)
    keys = list(groups)
    values = {"tpr": [], "fpr": []}
    for _ in range(samples):
        subset = [r for key in rng.choices(keys, k=len(keys)) for r in groups[key]]
        for metric, value in metrics(subset).items():
            if metric in values and value is not None:
                values[metric].append(value)
    result = {}
    for key, items in values.items():
        items.sort()
        result[key] = (
            [items[int(0.025 * len(items))], items[min(len(items) - 1, int(0.975 * len(items)))]]
            if items
            else None
        )
    return result


def matches_gold(draft, gold, source):
    from decimal import Decimal, InvalidOperation

    actual, expected = fields(draft.invoice), fields(gold.invoice)
    if actual.keys() != expected.keys():
        return False
    for key in actual:
        try:
            equal = Decimal(str(actual[key])) == Decimal(str(expected[key]))
        except InvalidOperation:
            equal = actual[key] == expected[key]
        if not equal:
            return False
        ev = draft.citations.get(key)
        ref = gold.citations.get(key)
        if not ev or not ref or ev.region_id != ref.region_id:
            return False
        region = source.region(ev.region_id)
        if not region or not ev.quote or ev.quote not in region.text:
            return False
    return True


def paired_comparisons(records, strategies):
    """Compare the same cases/repeats, resampling whole source-invoice groups."""
    comparisons = []
    for left, right in combinations(strategies, 2):
        a = {(r["id"], r["repeat"]): r for r in records if r["strategy"] == left}
        b = {(r["id"], r["repeat"]): r for r in records if r["strategy"] == right}
        keys = sorted(a.keys() & b.keys())
        groups = defaultdict(list)
        for key in keys:
            groups[a[key]["group"]].append(key)
        points, intervals = {}, {}
        rng = random.Random(31)
        for metric in ("tpr", "fpr"):
            ma, mb = metrics([a[k] for k in keys])[metric], metrics([b[k] for k in keys])[metric]
            points[metric] = mb - ma if ma is not None and mb is not None else None
            samples = []
            if len(groups) > 1:
                for _ in range(500):
                    sampled = [
                        k for g in rng.choices(list(groups), k=len(groups)) for k in groups[g]
                    ]
                    va, vb = (
                        metrics([a[k] for k in sampled])[metric],
                        metrics([b[k] for k in sampled])[metric],
                    )
                    if va is not None and vb is not None:
                        samples.append(vb - va)
            samples.sort()
            intervals[metric] = (
                [
                    samples[int(len(samples) * 0.025)],
                    samples[min(len(samples) - 1, int(len(samples) * 0.975))],
                ]
                if samples
                else None
            )
        comparisons.append(
            {
                "left": left,
                "right": right,
                "direction": "right minus left",
                "matched_trials": len(keys),
                "delta": points,
                "cluster_bootstrap_95": intervals,
            }
        )
    return comparisons


def evaluate(args):
    cases = json.loads(Path(args.cases).read_text())
    cases = [c for c in cases if args.split == "all" or c["split"] == args.split]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("No evaluation cases selected")
    records = []
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = path.with_suffix(".jsonl")
    checkpoint.write_text("")
    for strategy in args.strategies:
        for repeat in range(args.repeats):
            for case in cases:
                extractor, verifier = components(args, strategy)
                start = time.monotonic()
                row = {k: case[k] for k in ("id", "group", "kind", "correct", "error_fields")}
                if "family" in case:
                    row["family"] = case["family"]
                row.update(strategy=strategy, repeat=repeat, revision_attempted=False)
                try:
                    source = read_source(ROOT / case["source"], render=args.mode == "live")
                    if source.hash != case["source_hash"]:
                        raise ValueError("Evaluation source changed")
                    draft = Draft.model_validate(case["draft"])
                    req = VerificationRequest(source, draft)
                    result = verifier.verify(req)
                    row["verification"] = result.model_dump(mode="json")
                    validate_result(req, result)
                    row.update(
                        verdict=result.verdict,
                        verification=result.model_dump(mode="json"),
                        finding_fields=[f.field for f in result.findings],
                        calls=result.metadata.get("calls", 0),
                        final_correct=case["correct"],
                        final_verdict=result.verdict,
                    )
                    if args.with_revision and result.verdict == "incorrect":
                        row["revision_attempted"] = True
                        revision = extractor.extract(source, 2, draft, result.findings)
                        revised = verifier.verify(VerificationRequest(source, revision))
                        validate_result(VerificationRequest(source, revision), revised)
                        row.update(
                            final_correct=matches_gold(
                                revision, Draft.model_validate(case["gold"]), source
                            ),
                            final_verdict=revised.verdict,
                            revision=revision.model_dump(mode="json"),
                            revised_verification=revised.model_dump(mode="json"),
                        )
                except Exception as exc:  # noqa: BLE001 -- record failed trials in metric denominators
                    if "verdict" not in row:
                        row["verdict"] = "error"
                    row.update(error=f"{type(exc).__name__}: {exc}", final_verdict="error")
                if args.mode == "live":
                    calls = verifier.provider.calls + extractor.provider.calls
                    row.update(
                        calls=len(calls),
                        input_tokens=sum(c["usage"].get("prompt_tokens", 0) for c in calls),
                        output_tokens=sum(c["usage"].get("completion_tokens", 0) for c in calls),
                        requests=calls,
                        partial_artifacts=verifier.artifacts,
                    )
                row["seconds"] = time.monotonic() - start
                records.append(row)
                with checkpoint.open("a") as handle:
                    handle.write(json.dumps(row) + "\n")
                print(
                    f"{strategy} {case['id']}: {row['verdict']} ({row['seconds']:.1f}s)",
                    file=sys.stderr,
                    flush=True,
                )
    summaries = {}
    for strategy in args.strategies:
        rows = [r for r in records if r["strategy"] == strategy]
        summaries[strategy] = {
            **metrics(rows),
            "cluster_bootstrap_95": grouped_intervals(rows),
            "by_error_type": {
                kind: metrics([r for r in rows if r["kind"] == kind])
                for kind in sorted({r["kind"] for r in rows})
            },
        }
    report = {
        "mode": args.mode,
        "claim": "OFFLINE SIMULATION — NOT MODEL ACCURACY"
        if args.mode == "offline"
        else "Live evaluation on provided labels; review label provenance before drawing conclusions",
        "positive_definition": "verified extraction",
        "label_statuses": sorted({c.get("label_status", "unspecified") for c in cases}),
        "split": args.split,
        "with_revision": args.with_revision,
        "configuration": {
            key: getattr(args, key, None)
            for key in (
                "extractor_model",
                "verifier_model",
                "extractor_url",
                "verifier_url",
                "max_model_calls",
                "repeats",
                "cases",
            )
        },
        "summaries": summaries,
        "paired_comparisons": paired_comparisons(records, args.strategies),
        "records": records,
        "limitations": [
            "Seed labels are curated demonstration references, not independent human gold.",
            "Revision scoring uses exact normalized fields and reference citation regions; alternative valid mappings require adjudication.",
            "Cluster bootstrap may collapse at zero observed errors; zero observed FPR is not proof of zero risk.",
            "Cost is unavailable without provider-specific pricing. No default confidence score or ROC curve is fabricated.",
        ],
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(ROOT / "evals/cases.json"))
    parser.add_argument("--build-cases", action="store_true")
    parser.add_argument(
        "--strategies", nargs="+", choices=STRATEGIES, default=list(BASELINE_STRATEGIES)
    )
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--with-revision", action="store_true")
    parser.add_argument("--output", default=str(ROOT / "runs/evaluation.json"))
    model_arguments(parser)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.build_cases:
        build_cases(args.cases)
    report = evaluate(args)
    print(
        json.dumps(
            {
                "report": args.output,
                "claim": report["claim"],
                "summaries": {
                    k: {m: v for m, v in s.items() if m not in {"by_error_type"}}
                    for k, s in report["summaries"].items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
