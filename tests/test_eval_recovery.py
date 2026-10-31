import json
import sys
from pathlib import Path

import pytest

from scripts import compare_verifiers as runner


@pytest.mark.parametrize(
    "error,retry",
    [
        ("RuntimeError: Model API connection failed", True),
        ("ConnectionResetError: connection reset", True),
        ("TimeoutError: timed out", True),
        ("ValueError: Field verdict cites invalid evidence", False),
        ("ValueError: Blind transcript lacks requested source coverage", False),
        ("RuntimeError: Model API returned HTTP 401", False),
    ],
)
def test_transport_retry_classification(error, retry):
    assert runner.transport_failure({"verdict": "error", "error": error}) == retry
    assert not runner.transport_failure({"verdict": "incorrect", "error": error})


def test_recovery_preserves_results_and_audits_attempts(tmp_path, monkeypatch):
    cases = json.loads((runner.ROOT / "evals/mock-v1/dev-cases.json").read_text())[:3]
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases))
    original = tmp_path / "original"
    recovered = tmp_path / "recovered"
    phase = ["original"]
    attempts = []
    checks = []
    monkeypatch.setenv("XAI_API_KEY", "test-placeholder")
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "check_connections", lambda providers: checks.append(providers))

    def evaluate(args):
        case = json.loads(Path(args.cases).read_text())[0]
        error = None
        if phase[0] == "original":
            if case["id"] == cases[1]["id"]:
                error = "RuntimeError: Model API connection failed"
            if case["id"] == cases[2]["id"]:
                error = "ValueError: Field verdict cites invalid evidence"
        else:
            attempts.append(case["id"])
            if len(attempts) == 1:
                error = "TimeoutError: timed out"
        row = {
            **{k: case[k] for k in ("id", "group", "family", "kind", "correct", "error_fields")},
            "strategy": "direct",
            "repeat": 0,
            "seconds": 1,
            "calls": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "verdict": "error" if error else "verified",
        }
        if error:
            row["error"] = error
        return {"records": [row]}

    monkeypatch.setattr(runner, "evaluate", evaluate)
    base_args = [
        "compare_verifiers.py",
        "--cases",
        str(case_file),
        "--providers",
        "grok",
        "--strategies",
        "direct",
        "--workers",
        "1",
    ]
    monkeypatch.setattr(sys, "argv", base_args + ["--output", str(original)])
    runner.main()
    original_bytes = (original / "records.jsonl").read_bytes()
    phase[0] = "recovery"
    monkeypatch.setattr(
        sys,
        "argv",
        base_args
        + ["--output", str(recovered), "--resume-from", str(original), "--retry-network-errors"],
    )
    runner.main()
    assert (original / "records.jsonl").read_bytes() == original_bytes
    assert attempts == [cases[1]["id"], cases[1]["id"]]
    assert checks == [["grok"]]
    rows = [json.loads(line) for line in (recovered / "records.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and len({r["id"] for r in rows}) == 3
    assert sum(r["verdict"] == "error" for r in rows) == 1
    assert sum("retained_from" in r for r in rows) == 2
    assert len((recovered / "attempts.jsonl").read_text().splitlines()) == 2
    summary = json.loads((recovered / "summary.json").read_text())
    assert summary["network_retry_overhead"] == {
        "attempts": 1,
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "seconds": 1,
    }
    manifest = summary["manifest"]
    manifest["code_sha256"]["invoices/verification.py"] = "changed"
    with pytest.raises(ValueError, match="changed verifier code"):
        runner.resume_records(original, manifest)


def test_network_gate_waits_until_provider_returns(monkeypatch):
    delays = []
    probes = []
    monkeypatch.setattr(runner.time, "sleep", delays.append)

    def check(_):
        probes.append(True)
        if len(probes) < 3:
            raise OSError("offline")

    monkeypatch.setattr(runner, "check_connections", check)
    gate = runner.NetworkGate(["grok"])
    gate.failed()
    gate.wait()
    assert delays == [10, 20, 40]
    assert gate.ready.is_set()
    gate.wait()
    assert len(probes) == 3
