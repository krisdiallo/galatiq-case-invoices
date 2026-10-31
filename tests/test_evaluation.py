import json

from invoices.evaluation import build_cases, metrics, paired_comparisons


def row(correct, verdict):
    return {"correct": correct, "verdict": verdict, "seconds": 1}


def test_metric_denominators_include_abstentions_and_errors():
    data = [
        row(True, "verified"),
        row(True, "uncertain"),
        row(False, "verified"),
        row(False, "incorrect"),
        row(False, "uncertain"),
        row(False, "error"),
    ]
    result = metrics(data)
    assert result["tpr"] == 0.5
    assert result["fpr"] == 0.25
    assert result["acceptance_precision"] == 0.5
    assert result["error_detection_rate"] == 0.25
    assert result["operational_failure_rate"] == 1 / 6
    assert result["confusion"]["incorrect"]["uncertain"] == 1


def test_empty_metrics_not_zero_accuracy():
    result = metrics([])
    assert result["tpr"] is None and result["fpr"] is None


def test_fixture_variants_do_not_leak_between_splits(tmp_path):
    path = tmp_path / "cases.json"
    build_cases(path)
    cases = json.loads(path.read_text())
    groups = {}
    for c in cases:
        groups.setdefault(c["group"], set()).add(c["split"])
        if c["kind"] == "clean":
            assert not c["error_fields"]
        else:
            assert c["error_fields"]
        if c["kind"] == "wrong_citation":
            assert (
                c["draft"]["citations"]["items.0.quantity"]["region_id"]
                != c["gold"]["citations"]["items.0.quantity"]["region_id"]
            )
    assert all(len(splits) == 1 for splits in groups.values())
    assert len(cases) == 120


def test_paired_comparison_direction():
    data = []
    for strategy in ("direct", "field_by_field"):
        for i, correct in enumerate((True, False)):
            verdict = "incorrect" if strategy == "field_by_field" and not correct else "verified"
            data.append(
                {
                    **row(correct, verdict),
                    "id": str(i),
                    "repeat": 0,
                    "group": str(i),
                    "strategy": strategy,
                }
            )
    comparison = paired_comparisons(data, ("direct", "field_by_field"))[0]
    assert comparison["delta"]["fpr"] == -1
    assert comparison["delta"]["tpr"] == 0


def test_trial_checkpoint_matches_completed_report(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from invoices.evaluation import evaluate

    output = tmp_path / "eval.json"
    report = evaluate(
        SimpleNamespace(
            cases=str(Path(__file__).resolve().parents[1] / "evals/cases.json"),
            split="dev",
            limit=1,
            strategies=["direct"],
            repeats=1,
            with_revision=False,
            mode="offline",
            output=str(output),
        )
    )
    saved = [json.loads(line) for line in output.with_suffix(".jsonl").read_text().splitlines()]
    assert saved == report["records"]
    assert saved[0]["verification"]["verdict"] == "verified"
    assert json.loads(output.read_text())["configuration"]["repeats"] == 1
