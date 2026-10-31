import argparse
import json
import os
import sys
from pathlib import Path

from .agents import LiveExtractor, OfflineExtractor
from .pipeline import Pipeline
from .providers import ChatProvider
from .store import Store
from .verification import STRATEGIES, make_verifier

ROOT = Path(__file__).resolve().parents[1]


def model_arguments(parser):
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--extractor-model", default=os.getenv("EXTRACTOR_MODEL"))
    parser.add_argument("--verifier-model", default=os.getenv("VERIFIER_MODEL"))
    parser.add_argument(
        "--extractor-url", default=os.getenv("EXTRACTOR_URL", "https://api.x.ai/v1")
    )
    parser.add_argument("--verifier-url", default=os.getenv("VERIFIER_URL", "https://api.x.ai/v1"))
    parser.add_argument(
        "--extractor-key-env", default=os.getenv("EXTRACTOR_KEY_ENV", "XAI_API_KEY")
    )
    parser.add_argument("--verifier-key-env", default=os.getenv("VERIFIER_KEY_ENV", "XAI_API_KEY"))
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=None,
        help="Optional explicit per-model spend guard; no document-wide call cap by default",
    )


def components(args, strategy):
    if args.mode == "offline":
        return OfflineExtractor(), make_verifier(strategy)
    if not args.extractor_model or not args.verifier_model:
        raise ValueError(
            "Live mode requires EXTRACTOR_MODEL and VERIFIER_MODEL, "
            "or --extractor-model and --verifier-model. For the TUI, configure .env from .env.example."
        )
    extractor = ChatProvider(
        args.extractor_model,
        args.extractor_url,
        args.extractor_key_env,
        max_calls=args.max_model_calls,
        request_budget=getattr(args, "request_budget", None),
    )
    verifier = ChatProvider(
        args.verifier_model,
        args.verifier_url,
        args.verifier_key_env,
        max_calls=args.max_model_calls,
        request_budget=getattr(args, "request_budget", None),
    )
    if extractor.identity == verifier.identity:
        raise ValueError("Extractor and verifier must use different models or providers")
    return LiveExtractor(extractor), make_verifier(strategy, verifier)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Source-verified invoice processing. Offline by default; all payments are simulated."
    )
    parser.add_argument("--invoice_path", "--invoice-path", required=True)
    parser.add_argument("--strategy", choices=STRATEGIES, default="field_by_field")
    parser.add_argument("--inventory-db", default=str(ROOT / "inventory.db"))
    parser.add_argument("--state-db", default=str(ROOT / "runs/workflow.db"))
    parser.add_argument("--vp", choices=["pending", "approve", "decline"], default="pending")
    parser.add_argument("--approval-threshold", default="10000")
    parser.add_argument(
        "--default-currency",
        choices=["USD"],
        help="Explicit policy assumption for sources without currency",
    )
    parser.add_argument("--output", help="Also save the complete result JSON to this path")
    parser.add_argument("--quiet", action="store_true", help="Suppress stderr event summaries")
    model_arguments(parser)
    args = parser.parse_args(argv)
    try:
        extractor, verifier = components(args, args.strategy)

        def event(e):
            if not args.quiet:
                summary = {
                    k: v
                    for k, v in e["data"].items()
                    if k in {"status", "reason", "verdict", "decision"}
                }
                print(json.dumps({"stage": e["stage"], **summary}), file=sys.stderr)

        pipeline = Pipeline(
            extractor,
            verifier,
            Store(args.state_db),
            args.inventory_db,
            vp=args.vp,
            threshold=args.approval_threshold,
            default_currency=args.default_currency,
            render=args.mode == "live",
            on_event=event,
        )
        result = pipeline.run(args.invoice_path)
        text = json.dumps(result, indent=2)
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n")
        print(text)
        return 0 if result["status"] in {"paid", "duplicate"} else 2
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
