"""Run a bounded, paired live comparison. Credentials are loaded only from .env/env."""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from invoices.evaluation import evaluate, grouped_intervals, metrics, paired_comparisons
from invoices.providers import RequestBudget
from invoices.verification import BASELINE_STRATEGIES, STRATEGIES

PROVIDERS = {
    "grok": ("grok-4.20-0309-non-reasoning", "https://api.x.ai/v1", "XAI_API_KEY"),
    "openai": ("gpt-4.1-mini-2025-04-14", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "claude": ("claude-haiku-4-5-20251001", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
}


TRANSPORT_ERRORS = (
    "RuntimeError: Model API connection failed",
    "ConnectionResetError:",
    "TimeoutError:",
)


def transport_failure(row):
    return row.get("verdict") == "error" and row.get("error", "").startswith(TRANSPORT_ERRORS)


def trial_key(row):
    return row["provider"], row["strategy"], row["id"]


def resume_records(directory, manifest):
    """Reuse terminal outcomes, never hide validation failures or change the case population."""
    directory = Path(directory)
    original = json.loads((directory / "manifest.json").read_text())
    for key in (
        "case_file_sha256",
        "case_ids",
        "split",
        "strategies",
        "providers",
        "max_model_calls",
    ):
        if json.dumps(original[key], sort_keys=True) != json.dumps(manifest[key], sort_keys=True):
            raise ValueError(f"Cannot resume with changed {key}")
    # Demo-only edits to agents.py/tui.py do not affect this verifier-only experiment.
    verifier_files = (
        "providers.py",
        "verification.py",
        "chunking.py",
        "source.py",
        "models.py",
        "evaluation.py",
        "cli.py",
    )
    for name in verifier_files:
        key = "invoices/" + name
        if original["code_sha256"][key] != manifest["code_sha256"][key]:
            raise ValueError(f"Cannot resume with changed verifier code: {key}")
    rows = [json.loads(line) for line in (directory / "records.jsonl").read_text().splitlines()]
    seen = set()
    for row in rows:
        key = trial_key(row)
        if key in seen:
            raise ValueError(f"Duplicate trial in resume input: {key}")
        if (
            row["id"] not in manifest["case_ids"]
            or row["strategy"] not in manifest["strategies"]
            or row["provider"] not in manifest["providers"]
        ):
            raise ValueError(f"Unexpected trial in resume input: {key}")
        seen.add(key)
    return [dict(row, retained_from=str(directory)) for row in rows if not transport_failure(row)]


def check_connections(providers):
    """Authenticated read-only probe; never send model prompts while waiting for connectivity."""
    for name in providers:
        _, url, key_env = PROVIDERS[name]
        request = urllib.request.Request(
            url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + os.environ[key_env]},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise OSError("Provider connectivity probe failed")


class NetworkGate:
    def __init__(self, providers):
        self.providers = providers
        self.ready = threading.Event()
        self.ready.set()
        self.lock = threading.Lock()

    def failed(self):
        self.ready.clear()

    def wait(self):
        if self.ready.is_set():
            return
        with self.lock:
            if self.ready.is_set():
                return
            delay = 10
            while True:
                print(f"Network recovery: queue paused; checking again in {delay}s", flush=True)
                time.sleep(delay)
                try:
                    check_connections(self.providers)
                except OSError:
                    delay = min(delay * 2, 60)
                    continue
                self.ready.set()
                print(
                    "Network recovery: provider reachable; resuming interrupted trials", flush=True
                )
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", nargs="+", choices=PROVIDERS, default=list(PROVIDERS))
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/live-dev-cases.json")
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument(
        "--strategies", nargs="+", choices=STRATEGIES, default=list(BASELINE_STRATEGIES)
    )
    parser.add_argument("--max-model-calls", type=int, default=None)
    parser.add_argument(
        "--max-total-model-calls",
        type=int,
        default=None,
        help="Shared hard cap across all trials and retries in this invocation",
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Recover network-interrupted trials into a new run directory",
    )
    parser.add_argument(
        "--retry-network-errors",
        action="store_true",
        help="Pause the queue and retain/retry transport failures",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=ROOT / "runs/live-comparison")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("workers must be between 1 and 8")
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    if args.max_model_calls is not None and args.max_model_calls < 1:
        parser.error("max-model-calls must be positive")
    if args.max_total_model_calls is not None and args.max_total_model_calls < 1:
        parser.error("max-total-model-calls must be positive")
    request_budget = (
        RequestBudget(args.max_total_model_calls) if args.max_total_model_calls else None
    )
    load_dotenv(ROOT / ".env", override=False)

    for provider in args.providers:
        if not os.getenv(PROVIDERS[provider][2]):
            parser.error(f"Missing {PROVIDERS[provider][2]}")
    cases = json.loads(args.cases.read_text())
    cases = [c for c in cases if c["split"] == args.split]
    cases = cases[: args.limit] if args.limit else cases
    if not cases:
        parser.error("No cases selected")
    for case in cases:
        if hashlib.sha256((ROOT / case["source"]).read_bytes()).hexdigest() != case["source_hash"]:
            parser.error(f"Evaluation source changed: {case['source']}")
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "cases"
    inputs.mkdir()
    for i, case in enumerate(cases):
        (inputs / f"{i}.json").write_text(json.dumps([case], indent=2))
    manifest = {
        "started_at": datetime.now(UTC).isoformat(),
        "providers": {p: PROVIDERS[p][:2] for p in args.providers},
        "case_ids": [c["id"] for c in cases],
        "case_file_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "code_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "invoices").glob("*.py"))
        },
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "resume_from": str(args.resume_from) if args.resume_from else None,
        "retry_network_errors": args.retry_network_errors,
        "workers": args.workers,
        "split": args.split,
        "strategies": args.strategies,
        "source_documents": len({c["source_hash"] for c in cases}),
        "families": sorted({c["family"] for c in cases if "family" in c}),
        "source_hashes": {c["source"]: c["source_hash"] for c in cases},
        "max_model_calls": args.max_model_calls,
        "max_total_model_calls": args.max_total_model_calls,
        "repeats": 1,
        "with_revision": False,
        "positive_definition": "verified extraction",
        "label_statuses": sorted({c["label_status"] for c in cases}),
    }
    records = resume_records(args.resume_from, manifest) if args.resume_from else []
    retained = {trial_key(r) for r in records}
    manifest["retained_trials"] = len(records)
    if args.resume_from:
        manifest["original_manifest_sha256"] = hashlib.sha256(
            (args.resume_from / "manifest.json").read_bytes()
        ).hexdigest()
        manifest["original_records_sha256"] = hashlib.sha256(
            (args.resume_from / "records.jsonl").read_bytes()
        ).hexdigest()
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with (args.output / "records.jsonl").open("w") as handle:
        for row in records:
            handle.write(json.dumps(row) + "\n")
    gate = NetworkGate(args.providers)
    attempts_lock = threading.Lock()
    retry_overhead = {
        "attempts": 0,
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "seconds": 0,
    }
    print(
        f"Retaining {len(records)} terminal trials; network recovery enabled: {args.retry_network_errors}",
        flush=True,
    )

    def trial(provider, strategy, index):
        model, url, key_env = PROVIDERS[provider]
        config = SimpleNamespace(
            cases=str(inputs / f"{index}.json"),
            split=args.split,
            limit=None,
            strategies=[strategy],
            repeats=1,
            with_revision=False,
            mode="live",
            extractor_model="grok-4.3",
            extractor_url="https://api.x.ai/v1",
            extractor_key_env="XAI_API_KEY",
            verifier_model=model,
            verifier_url=url,
            verifier_key_env=key_env,
            max_model_calls=args.max_model_calls,
            request_budget=request_budget,
            output=str(args.output / "trials" / f"{provider}-{strategy}-{index}.json"),
        )
        attempt = 0
        while True:
            gate.wait()
            attempt += 1
            config.output = str(
                args.output / "trials" / f"{provider}-{strategy}-{index}-attempt-{attempt}.json"
            )
            report = evaluate(config)
            row = {"provider": provider, **report["records"][0], "attempt": attempt}
            retry = args.retry_network_errors and transport_failure(row)
            if retry:
                gate.failed()
            with attempts_lock:
                with (args.output / "attempts.jsonl").open("a") as handle:
                    handle.write(json.dumps(row) + "\n")
                if retry:
                    retry_overhead["attempts"] += 1
                    for key in ("calls", "input_tokens", "output_tokens", "seconds"):
                        retry_overhead[key] += row.get(key, 0)
            if not retry:
                return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # Interleave providers/strategies/cases instead of finishing one provider first.
        jobs = [
            pool.submit(trial, p, s, i)
            for i in range(len(cases))
            for s in args.strategies
            for p in args.providers
            if (p, s, cases[i]["id"]) not in retained
        ]
        total = len(records) + len(jobs)
        for future in as_completed(jobs):
            row = future.result()
            records.append(row)
            with (args.output / "records.jsonl").open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            print(
                f"[{len(records)}/{total}] {row['provider']} {row['strategy']} "
                f"{row['id']}: {row['verdict']}",
                flush=True,
            )
    summary = {}
    for provider in args.providers:
        rows = [r for r in records if r["provider"] == provider]
        summary[provider] = {
            "strategies": {
                s: {
                    **metrics([r for r in rows if r["strategy"] == s]),
                    "cluster_bootstrap_95": grouped_intervals(
                        [r for r in rows if r["strategy"] == s]
                    ),
                    "by_error_type": {
                        kind: metrics([r for r in rows if r["strategy"] == s and r["kind"] == kind])
                        for kind in sorted({r["kind"] for r in rows})
                    },
                    "by_family": {
                        family: metrics(
                            [r for r in rows if r["strategy"] == s and r.get("family") == family]
                        )
                        for family in manifest["families"]
                    },
                    "family_cluster_bootstrap_95": grouped_intervals(
                        [
                            {**r, "group": r.get("family", r["group"])}
                            for r in rows
                            if r["strategy"] == s
                        ]
                    ),
                }
                for s in args.strategies
            },
            "paired_comparisons": paired_comparisons(rows, args.strategies),
            "family_paired_comparisons": paired_comparisons(
                [{**r, "group": r.get("family", r["group"])} for r in rows], args.strategies
            ),
        }
    (args.output / "summary.json").write_text(
        json.dumps(
            {
                "manifest": manifest,
                "completed_at": datetime.now(UTC).isoformat(),
                "results": summary,
                "network_retry_overhead": retry_overhead,
                "total_model_calls_used": request_budget.used if request_budget else None,
                "limitations": [
                    f"{manifest['source_documents']} source documents; variants and shared template components are correlated.",
                    "Labels follow the input dataset's provenance; synthetic labels do not establish real-world accuracy.",
                    "One sample per model/strategy/case; no revision in this experiment.",
                    "Non-transport errors remain in denominators. With explicit network recovery, transport failures are preserved in original runs/attempts.jsonl and retried; these are recovered outcomes, not first-attempt reliability scores.",
                    "Zero observed false accepts is not proof of zero deployment risk.",
                ],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Summary: {args.output / 'summary.json'}")


if __name__ == "__main__":
    main()
