from decimal import Decimal

from .rules import validate
from .source import read_source
from .store import load_inventory
from .verification import VerificationRequest, validate_result


class Pipeline:
    def __init__(
        self,
        extractor,
        verifier,
        store,
        inventory_path,
        *,
        vp="pending",
        threshold="10000",
        default_currency=None,
        render=False,
        on_event=None,
    ):
        self.extractor = extractor
        self.verifier = verifier
        self.store = store
        self.inventory_path = inventory_path
        self.vp = vp
        self.threshold = Decimal(threshold)
        if not self.threshold.is_finite() or self.threshold < 0:
            raise ValueError("Invalid approval threshold")
        if vp not in {"pending", "approve", "decline"}:
            raise ValueError("Invalid VP simulation decision")
        self.default_currency = default_currency
        self.render = render
        self.on_event = on_event

    def run(self, path):
        run_id = self.store.start(path)
        events = []
        result = {
            "run_id": run_id,
            "status": "hold",
            "reason": None,
            "draft": None,
            "verification": None,
            "checks": [],
            "receipt": None,
        }

        def emit(stage, data):
            event = self.store.event(run_id, stage, data)
            events.append(event)
            if self.on_event:
                self.on_event(event)

        def finish(status, reason):
            result.update(status=status, reason=reason)
            emit("result", {"status": status, "reason": reason})
            result["events"] = events
            self.store.finish(run_id, result)
            return result

        try:
            emit(
                "configuration",
                {
                    "strategy": getattr(self.verifier, "strategy", "custom"),
                    "extractor": getattr(self.extractor, "identity", "custom"),
                    "max_revisions": 1,
                    "default_currency": self.default_currency,
                    "approval_threshold": str(self.threshold),
                    "vp_simulation": self.vp,
                },
            )
            source = read_source(path, render=self.render)
            emit("source", source.payload())
            for component in (self.extractor, self.verifier):
                if hasattr(component, "on_progress"):
                    component.on_progress = lambda data: emit("chunk_progress", data)
            emit("stage_started", {"stage": "extraction"})
            draft = self.extractor.extract(source)
            for revision in range(2):
                if draft.version != revision + 1 or draft.source_hash != source.hash:
                    raise ValueError("Extractor returned an invalid version or source identity")
                result["draft"] = draft.model_dump(mode="json")
                emit("extraction", result["draft"])
                if hasattr(self.extractor, "provider"):
                    emit(
                        "extraction_usage",
                        {
                            "prompt_version": "2-chunked",
                            "chunks": getattr(self.extractor, "chunks", []),
                            "request_policy": getattr(
                                self.extractor.provider, "request_policy", {}
                            ),
                            "requests": self.extractor.provider.calls,
                        },
                    )
                req = VerificationRequest(source, draft)
                emit("stage_started", {"stage": "verification"})
                verification = self.verifier.verify(req)
                validate_result(req, verification)
                result["verification"] = verification.model_dump(mode="json")
                emit("verification", result["verification"])
                if verification.verdict == "verified":
                    break
                if verification.verdict == "uncertain":
                    return finish("hold", "Source verification is uncertain")
                if revision == 1:
                    return finish("hold", "Extraction still incorrect after one revision")
                emit("stage_started", {"stage": "extraction", "revision": 2})
                draft = self.extractor.extract(
                    source, version=2, previous=draft, findings=verification.findings
                )
            emit("stage_started", {"stage": "validation"})
            inventory = load_inventory(self.inventory_path)
            checks = validate(draft.invoice, inventory, self.default_currency)
            result["checks"] = [c.model_dump() for c in checks]
            emit("validation", {"checks": result["checks"]})
            rejects = [c for c in checks if c.outcome == "reject"]
            holds = [c for c in checks if c.outcome == "hold"]
            if rejects:
                return finish("rejected", "; ".join(c.reason for c in rejects))
            if holds:
                return finish("hold", "; ".join(c.reason for c in holds))
            needs_vp = draft.invoice.total > self.threshold
            decision = self.vp if needs_vp else "approve"
            emit(
                "approval",
                {
                    "invoice_hash": draft.digest,
                    "policy_version": "1",
                    "threshold": str(self.threshold),
                    "kind": "simulated_vp" if needs_vp else "automatic",
                    "decision": decision,
                },
            )
            if decision == "pending":
                return finish(
                    "awaiting_approval",
                    "Simulated VP decision required; rerun with --vp approve or --vp decline",
                )
            if decision == "decline":
                return finish("rejected", "Simulated VP declined")
            # Last gate ties approval and verification to this exact current draft.
            validate_result(VerificationRequest(source, draft), verification)
            status, receipt = self.store.payment(
                draft.invoice, draft.invoice.currency or self.default_currency, run_id
            )
            result["receipt"] = receipt
            emit("payment", {"status": status, "receipt": receipt})
            return finish(
                status,
                receipt.get(
                    "reason",
                    "Existing receipt returned"
                    if status == "duplicate"
                    else "Mock payment completed",
                ),
            )
        except Exception as exc:  # noqa: BLE001 -- orchestration boundary must fail closed
            # A model/reader/database failure must never become approval.
            emit("operational_error", {"type": type(exc).__name__, "reason": str(exc)[:1000]})
            return finish("hold", f"{type(exc).__name__}: {str(exc)[:1000]}")
