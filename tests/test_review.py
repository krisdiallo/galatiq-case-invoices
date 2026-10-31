import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from invoices.agents import OfflineExtractor
from invoices.pipeline import Pipeline
from invoices.review import decide
from invoices.store import Store
from invoices.verification import OfflineVerifier
from scripts.setup_db import setup_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pending(tmp_path):
    inventory = tmp_path / "inventory.db"
    setup_database(inventory)
    store = Store(tmp_path / "state.db")
    source = tmp_path / "invoice.txt"
    source.write_bytes((ROOT / "data/invoices/invoice_1001.txt").read_bytes())
    result = Pipeline(
        OfflineExtractor(), OfflineVerifier("direct"), store, inventory, threshold="2500"
    ).run(source)
    assert result["status"] == "awaiting_approval"
    return store, inventory, source, result


def test_human_approval_pays_exact_pending_version(pending):
    store, inventory, _, old = pending
    result = decide(store, old["run_id"], "approve", inventory)
    assert result["status"] == "paid"
    assert result["draft"] == old["draft"]
    assert result["receipt"]["run_id"] == old["run_id"]
    assert any(e["stage"] == "human_decision" for e in result["events"])
    with pytest.raises(ValueError, match="awaiting"):
        decide(store, old["run_id"], "approve", inventory)


def test_manual_hold_reason_persists_and_cannot_be_approved(pending):
    store, inventory, _, result = pending
    held = decide(store, result["run_id"], "hold", inventory, "Waiting for PO confirmation")
    assert held["status"] == "hold"
    assert store.get_run(result["run_id"])[1]["reason"] == "Waiting for PO confirmation"
    with pytest.raises(ValueError, match="awaiting"):
        decide(store, result["run_id"], "approve", inventory)


def test_empty_hold_reason_does_not_claim_run(pending):
    store, inventory, _, result = pending
    with pytest.raises(ValueError, match="reason"):
        decide(store, result["run_id"], "hold", inventory, " ")
    assert store.get_run(result["run_id"])[1]["status"] == "awaiting_approval"


def test_decline_never_pays(pending):
    store, inventory, _, result = pending
    assert decide(store, result["run_id"], "decline", inventory)["status"] == "rejected"
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_changed_source_holds_on_approval(pending):
    store, inventory, source, result = pending
    source.write_text("Changed source")
    assert decide(store, result["run_id"], "approve", inventory)["status"] == "hold"


def test_stock_rechecked_on_approval(pending):
    store, inventory, _, result = pending
    with sqlite3.connect(inventory) as conn:
        conn.execute("UPDATE inventory SET stock=1 WHERE item='WidgetA'")
    rejected = decide(store, result["run_id"], "approve", inventory)
    assert rejected["status"] == "rejected"
    assert rejected["receipt"] is None
    assert "available 1" in rejected["reason"]


def test_concurrent_review_decisions_cannot_both_claim(pending):
    store, inventory, _, result = pending

    def approve(_):
        try:
            return decide(store, result["run_id"], "approve", inventory)["status"]
        except ValueError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, range(2)))
    assert sorted(outcomes) == ["blocked", "paid"]
