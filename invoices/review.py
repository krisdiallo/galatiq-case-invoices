"""Human decisions on persisted pending runs, without re-running model extraction."""

from .models import Draft, VerificationResult
from .rules import validate
from .source import read_source
from .store import load_inventory
from .verification import VerificationRequest, validate_result


def decide(store, run_id, decision, inventory_path, reason=""):
    if decision not in {"approve", "decline", "hold"}:
        raise ValueError("Unknown review decision")
    if decision == "hold" and not reason.strip():
        raise ValueError("A hold needs a reason")
    path, result = store.get_run(run_id)
    if result["status"] != "awaiting_approval":
        raise ValueError("Only invoices awaiting VP approval can receive a decision")
    store.claim_review(run_id, result)

    def emit(stage, data):
        result.setdefault("events", []).append(store.event(run_id, stage, data))

    def finish(status, message):
        result.update(status=status, reason=message)
        emit("result", {"status": status, "reason": message})
        store.finish(run_id, result)
        return result

    try:
        draft = Draft.model_validate(result["draft"])
        emit(
            "human_decision",
            {
                "decision": decision,
                "reason": reason.strip(),
                "invoice_hash": draft.digest,
                "actor": "local_tui_operator",
            },
        )
        if decision == "hold":
            return finish("hold", reason.strip())
        if decision == "decline":
            return finish("rejected", reason.strip() or "VP declined this invoice")
        source = read_source(path)
        verification = VerificationResult.model_validate(result["verification"])
        if verification.verdict != "verified":
            raise ValueError("Pending invoice has no verified extraction")
        validate_result(VerificationRequest(source, draft), verification)
        configs = [e["data"] for e in result["events"] if e["stage"] == "configuration"]
        default_currency = configs[-1].get("default_currency") if configs else None
        checks = validate(draft.invoice, load_inventory(inventory_path), default_currency)
        result["checks"] = [c.model_dump() for c in checks]
        emit("validation", {"checks": result["checks"], "context": "before human-approved payment"})
        failed = [c for c in checks if c.outcome in {"reject", "hold"}]
        if failed:
            return finish(
                "rejected" if any(c.outcome == "reject" for c in failed) else "hold",
                "; ".join(c.reason for c in failed),
            )
        emit(
            "approval",
            {
                "invoice_hash": draft.digest,
                "kind": "human_vp",
                "decision": "approve",
                "policy_version": "1",
            },
        )
        status, receipt = store.payment(
            draft.invoice, draft.invoice.currency or default_currency, run_id
        )
        result["receipt"] = receipt
        emit("payment", {"status": status, "receipt": receipt})
        return finish(status, receipt.get("reason", "VP approved · mock payment completed"))
    except Exception as exc:  # noqa: BLE001 -- a failed review cannot authorize payment
        emit("operational_error", {"type": type(exc).__name__, "reason": str(exc)[:1000]})
        return finish("hold", f"Approval could not complete: {str(exc)[:1000]}")
