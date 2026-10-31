import hashlib
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from invoices.evaluation import matches_gold
from invoices.models import Assessment, Draft, fields
from invoices.source import evidence_errors, read_source
from invoices.verification import result_for
from scripts.build_mock_dataset import build

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals/mock-v1"


@pytest.fixture(scope="module")
def dataset():
    cases = [
        case
        for split in ("dev", "test")
        for case in json.loads((DATA / f"{split}-cases.json").read_text())
    ]
    sources = {c["source"]: None for c in cases}
    for path in sources:
        sources[path] = read_source(ROOT / path)
    return cases, sources


def test_frozen_dataset_integrity_and_split_isolation(dataset):
    cases, sources = dataset
    manifest = json.loads((DATA / "manifest.json").read_text())
    assert len(cases) == 840 and len(sources) == 60
    assert sum(c["correct"] for c in cases) == 120
    assert len({c["id"] for c in cases}) == len(cases)
    assert len({s.hash for s in sources.values()}) == 60
    for path, digest in manifest["files_sha256"].items():
        assert hashlib.sha256((DATA / path).read_bytes()).hexdigest() == digest
    splits = defaultdict(set)
    for case in cases:
        for group in (case["family"], case["group"], case["source_hash"]):
            splits[group].add(case["split"])
    assert all(len(values) == 1 for values in splits.values())
    assert len({c["family"] for c in cases if c["split"] == "dev"}) == 8
    assert len({c["family"] for c in cases if c["split"] == "test"}) == 4


def test_every_gold_field_has_real_evidence_and_every_label_matches(dataset):
    cases, sources = dataset
    for case in cases:
        source = sources[case["source"]]
        gold, draft = Draft.model_validate(case["gold"]), Draft.model_validate(case["draft"])
        assert source.hash == case["source_hash"] == gold.source_hash == draft.source_hash
        assert set(gold.citations) == set(fields(gold.invoice))
        assert not evidence_errors(source, gold)
        assert matches_gold(draft, gold, source) == case["correct"], case["id"]
        assert bool(case["error_fields"]) != case["correct"]
    # Wrapped names span two rendered lines, with each authored fragment present.
    for path in (DATA / "truth").glob("*.json"):
        truth = json.loads(path.read_text())
        source = sources[str((DATA / "sources" / (path.stem + ".pdf")).relative_to(ROOT))]
        for anchors in truth["anchors"].values():
            for a in anchors:
                assert any(r.page == a["page"] and a["text"] in r.text for r in source.regions)
        for region in source.regions:
            width, height = source.page_sizes[region.page - 1]
            assert 0 <= region.bbox[0] < region.bbox[2] <= width
            assert 0 <= region.bbox[1] < region.bbox[3] <= height


def assert_arithmetic(invoice):
    def cents(value):
        return value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)

    for item in invoice.items:
        assert item.amount == cents(item.quantity * item.unit_price - (item.discount or 0))
    assert invoice.subtotal == sum(item.amount for item in invoice.items)
    assert invoice.tax == cents(invoice.subtotal * invoice.tax_rate)
    assert invoice.total == invoice.subtotal + invoice.tax + invoice.shipping - invoice.discount


def test_balanced_corruption_really_bypasses_arithmetic(dataset):
    cases, _ = dataset
    for case in cases:
        if case["kind"] in {"clean", "consistent_wrong_numbers"}:
            assert_arithmetic(Draft.model_validate(case["draft"]).invoice)
        if case["kind"] == "consistent_wrong_numbers":
            assert (
                case["draft"]["invoice"]["items"][0]["quantity"]
                != case["gold"]["invoice"]["items"][0]["quantity"]
            )
            assert {"items.0.quantity", "items.0.amount", "subtotal", "tax", "total"} <= set(
                case["error_fields"]
            )
        if case["kind"] == "wrong_citation":
            assert case["draft"]["invoice"] == case["gold"]["invoice"]
            assert case["error_fields"] == ["items.0.quantity"]


def test_generator_reproducible_and_refuses_overwrite(tmp_path):
    a = build(tmp_path / "first", per_family=1, seed=17)
    b = build(tmp_path / "second", per_family=1, seed=17)
    assert [d["source_hash"] for d in a["documents"]] == [d["source_hash"] for d in b["documents"]]
    for path, digest in a["files_sha256"].items():
        if path.startswith(("sources/", "truth/")):
            assert digest == b["files_sha256"][path]
    with pytest.raises(FileExistsError):
        build(tmp_path / "first", per_family=1)


def test_evaluation_accepts_new_cases_and_preserves_family(tmp_path, monkeypatch):
    from invoices import evaluation

    class AcceptClean:
        def verify(self, request):
            return result_for(
                request,
                "direct",
                Assessment(
                    verdict="verified",
                    findings=[],
                    checked_fields=list(fields(request.draft.invoice)),
                    covered_regions=[r.id for r in request.source.regions],
                    complete=True,
                ),
            )

    monkeypatch.setattr(evaluation, "components", lambda *_: (None, AcceptClean()))
    report = evaluation.evaluate(
        SimpleNamespace(
            cases=str(DATA / "dev-cases.json"),
            split="dev",
            limit=1,
            strategies=["direct"],
            repeats=1,
            with_revision=False,
            mode="offline",
            output=str(tmp_path / "result.json"),
        )
    )
    assert report["records"][0]["family"] == "standard_table"
    assert report["records"][0]["verdict"] == "verified"


def test_comparison_runner_supports_heldout_cases_without_implicit_call_cap(tmp_path, monkeypatch):
    import sys

    from scripts import compare_verifiers

    source_cases = json.loads((DATA / "test-cases.json").read_text())
    selected = [source_cases[0], source_cases[2]]
    inputs = tmp_path / "input.json"
    inputs.write_text(json.dumps(selected))
    output = tmp_path / "comparison"
    seen = []

    def fake_evaluate(args):
        assert args.split == "test"
        assert args.max_model_calls is None
        case = json.loads(Path(args.cases).read_text())[0]
        seen.append((args.strategies[0], case["id"]))
        return {
            "records": [
                {
                    **{
                        k: case[k]
                        for k in ("id", "group", "family", "kind", "correct", "error_fields")
                    },
                    "strategy": args.strategies[0],
                    "repeat": 0,
                    "seconds": 0,
                    "verdict": "verified" if case["correct"] else "incorrect",
                    "finding_fields": case["error_fields"],
                }
            ]
        }

    monkeypatch.setattr(compare_verifiers, "evaluate", fake_evaluate)
    monkeypatch.setenv("XAI_API_KEY", "test-placeholder-not-a-real-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_verifiers.py",
            "--cases",
            str(inputs),
            "--split",
            "test",
            "--providers",
            "grok",
            "--strategies",
            "direct",
            "blind_transcription",
            "--workers",
            "2",
            "--output",
            str(output),
        ],
    )
    compare_verifiers.main()
    assert len(seen) == 4
    summary = json.loads((output / "summary.json").read_text())
    assert summary["manifest"]["split"] == "test"
    assert summary["manifest"]["source_documents"] == 1
    assert summary["manifest"]["max_model_calls"] is None
    scores = summary["results"]["grok"]["strategies"]
    assert set(scores) == {"direct", "blind_transcription"}
    assert scores["direct"]["by_family"][selected[0]["family"]]["fpr"] == 0
    assert scores["direct"]["by_error_type"]["clean"]["tpr"] == 1


def test_parallel_pdf_rendering_survives_in_fresh_process():
    # A native renderer crash would kill pytest itself without process isolation.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from invoices.source import read_source
paths = sorted(Path("evals/mock-v1/sources").glob("*.pdf"))[:16]
def render(path):
    source = read_source(path, render=True)
    return len(source.images) == len(source.page_sizes)
with ThreadPoolExecutor(max_workers=8) as pool:
    assert all(pool.map(render, paths))
""",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
