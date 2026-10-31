import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from invoices.agents import OfflineExtractor
from invoices.models import Assessment, Finding, Invoice, Item, fields
from invoices.pipeline import Pipeline
from invoices.rules import validate
from invoices.source import read_source
from invoices.store import Store
from invoices.verification import OfflineVerifier, result_for
from scripts.setup_db import setup_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "inventory.db"
    setup_database(db)
    return db, Store(tmp_path / "workflow.db")


def run(env, name="invoice_1001.txt", extractor=None, verifier=None, **kwargs):
    db, store = env
    return Pipeline(
        extractor or OfflineExtractor(), verifier or OfflineVerifier("direct"), store, db, **kwargs
    ).run(ROOT / "data/invoices" / name)


def test_clean_and_idempotent_payment(env):
    first = run(env)
    second = run(env)
    assert first["status"] == "paid"
    assert second["status"] == "duplicate"
    assert first["receipt"]["id"] == second["receipt"]["id"]
    with env[1].connect() as conn:
        assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] > 0


@pytest.mark.parametrize(
    "name,rule",
    [
        ("invoice_1002.txt", "inventory_stock"),
        ("invoice_1008.txt", "inventory_known"),
        ("invoice_1009.json", "quantity_positive"),
        ("invoice_1013.json", "total_arithmetic"),
        ("invoice_1014.xml", "currency"),
        ("invoice_1016.json", "inventory_known"),
    ],
)
def test_invalid_invoices_rejected(env, name, rule):
    result = run(env, name)
    assert result["status"] == "rejected"
    assert any(c["rule"] == rule and c["outcome"] == "reject" for c in result["checks"])
    assert result["receipt"] is None


@pytest.mark.parametrize("strategy", ["direct", "field_by_field", "blind_transcription"])
def test_all_offline_strategies_accept_clean(env, strategy):
    assert run(env, verifier=OfflineVerifier(strategy))["status"] == "paid"


class MisreadingExtractor(OfflineExtractor):
    def __init__(self, repeat=False):
        self.calls = 0
        self.repeat = repeat

    def extract(self, *args, **kwargs):
        self.calls += 1
        draft = super().extract(*args, **kwargs)
        if self.calls == 1 or self.repeat:
            draft.invoice.items[0].quantity = Decimal(2)
        return draft


def test_false_positive_quantity_is_corrected_then_rejected(env):
    extractor = MisreadingExtractor()
    result = run(env, "invoice_1002.txt", extractor=extractor)
    assert extractor.calls == 2
    assert result["draft"]["version"] == 2
    assert result["draft"]["invoice"]["items"][0]["quantity"] == "20"
    assert result["status"] == "rejected"


def test_one_revision_limit(env):
    extractor = MisreadingExtractor(repeat=True)
    result = run(env, "invoice_1002.txt", extractor=extractor)
    assert extractor.calls == 2
    assert result["status"] == "hold"
    assert result["checks"] == []


class UncertainVerifier:
    def verify(self, req):
        return result_for(
            req,
            "test",
            Assessment(
                verdict="uncertain",
                findings=[Finding(field="items", reason="Unreadable")],
                checked_fields=[],
                covered_regions=[],
                complete=False,
            ),
        )


def test_uncertainty_holds_without_revision(env):
    extractor = MisreadingExtractor()
    result = run(env, extractor=extractor, verifier=UncertainVerifier())
    assert result["status"] == "hold"
    assert extractor.calls == 1


def test_inventory_invalid_rejects(env):
    with sqlite3.connect(env[0]) as conn:
        conn.execute("UPDATE inventory SET stock=-1 WHERE item='WidgetA'")
    result = run(env)
    assert result["status"] == "rejected"
    assert any(c["rule"] == "inventory_integrity" for c in result["checks"])


def test_inventory_missing_holds_without_creating_database(env):
    env[0].unlink()
    assert run(env)["status"] == "hold"
    assert not env[0].exists()


def test_changed_paid_invoice_held(env):
    assert run(env, "invoice_1004.json")["status"] == "paid"
    result = run(env, "invoice_1004_revised.json")
    assert result["status"] == "hold"
    assert "already paid" in result["reason"]


def test_aggregate_repeated_lines_and_decimal_rounding():
    invoice = Invoice(
        number="x",
        vendor="v",
        due_date="2026-01-01",
        currency="USD",
        items=[
            Item(name="WidgetA", quantity=10, unit_price=".335"),
            Item(name="WidgetA", quantity=6, unit_price=".335"),
        ],
        total="5.36",
    )
    checks = validate(invoice, {"WidgetA": 15})
    assert any(c.rule == "inventory_stock" and c.outcome == "reject" for c in checks)
    assert any(c.rule == "total_arithmetic" and c.outcome == "pass" for c in checks)


def test_concurrent_payment_only_once(env):
    invoice = (
        OfflineExtractor().extract(read_source(ROOT / "data/invoices/invoice_1001.txt")).invoice
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: env[1].payment(invoice, "USD", "test"), range(8)))
    assert [r[0] for r in results].count("paid") == 1
    assert len({r[1]["id"] for r in results}) == 1


class PassingVerifier:
    def verify(self, req):
        return result_for(
            req,
            "test",
            Assessment(
                verdict="verified",
                findings=[],
                checked_fields=list(fields(req.draft.invoice)),
                covered_regions=[r.id for r in req.source.regions],
                complete=True,
            ),
        )


class HighValueExtractor(OfflineExtractor):
    def extract(self, *args, **kwargs):
        draft = super().extract(*args, **kwargs)
        draft.invoice.items[0].unit_price = Decimal(1000)
        draft.invoice.total = draft.invoice.subtotal = Decimal(12500)
        return draft


@pytest.mark.parametrize(
    "decision,status",
    [("pending", "awaiting_approval"), ("approve", "paid"), ("decline", "rejected")],
)
def test_vp_policy_gate(env, decision, status):
    # Synthetic verifier isolates the approval gate from source fidelity.
    result = run(env, extractor=HighValueExtractor(), verifier=PassingVerifier(), vp=decision)
    assert result["status"] == status
    assert bool(result["receipt"]) == (status == "paid")


def test_unknown_offline_document_holds(env, tmp_path):
    file = tmp_path / "new.txt"
    file.write_text("An unseen invoice")
    result = Pipeline(OfflineExtractor(), OfflineVerifier("direct"), env[1], env[0]).run(file)
    assert result["status"] == "hold"
    assert "No offline fixture" in result["reason"]
