import io
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from invoices.agents import OfflineExtractor
from invoices.models import Evidence
from invoices.providers import ChatProvider, RequestBudget
from invoices.source import read_source
from invoices.verification import (
    BatchedClaims,
    Coverage,
    FieldClaim,
    IndependentInvoice,
    LiveVerifier,
    VerificationRequest,
    validate_result,
)


@pytest.fixture
def request_data():
    source = read_source(Path(__file__).resolve().parents[1] / "data/invoices/invoice_1001.txt")
    return VerificationRequest(source, OfflineExtractor().extract(source))


class IndependentProvider:
    identity = "test:independent"

    def __init__(self, req):
        self.req = req
        self.calls = []

    def complete(self, instruction, payload, model, images=()):
        assert model is IndependentInvoice
        assert set(payload) == {"source"}
        assert "draft" not in payload and "document_header_context" not in payload
        self.calls.append({"usage": {}})
        return IndependentInvoice(
            invoice=self.req.draft.invoice,
            citations=self.req.draft.citations,
            covered_regions=[r.id for r in self.req.source.regions],
            unclear_regions=[],
        )


def test_blind_extraction_accepts_equal_values_and_detects_corruption(request_data):
    provider = IndependentProvider(request_data)
    clean = request_data.draft.model_copy(deep=True)
    clean.invoice.items[0].quantity = clean.invoice.items[0].quantity.quantize(Decimal(".0000"))
    req = VerificationRequest(request_data.source, clean)
    result = LiveVerifier("blind_extraction", provider).verify(req)
    validate_result(req, result)
    assert result.verdict == "verified" and len(provider.calls) == 1
    corrupt = clean.model_copy(deep=True)
    corrupt.invoice.items[0].quantity += 1
    req = VerificationRequest(request_data.source, corrupt)
    result = LiveVerifier("blind_extraction", provider).verify(req)
    assert result.verdict == "incorrect"
    assert "items.0.quantity" in [f.field for f in result.findings]
    validate_result(req, result)


def test_blind_extraction_requires_citation_mapping(request_data):
    corrupt = request_data.draft.model_copy(deep=True)
    corrupt.citations["items.0.quantity"] = corrupt.citations["number"]
    req = VerificationRequest(request_data.source, corrupt)
    result = LiveVerifier("blind_extraction", IndependentProvider(request_data)).verify(req)
    assert result.verdict == "uncertain"
    validate_result(req, result)


class ClaimsProvider:
    identity = "test:claims"

    def __init__(self, req, omit=False, contradiction=False):
        self.req, self.omit, self.contradiction = req, omit, contradiction
        self.calls = []

    def complete(self, instruction, payload, model, images=()):
        assert model is BatchedClaims
        assert "document_header_context" not in payload
        self.calls.append({"usage": {}})
        claims = [
            FieldClaim(field=k, verdict="supported", reason="matches", evidence=[v])
            for k, v in self.req.draft.citations.items()
        ]
        if self.omit:
            claims.pop()
        if self.contradiction:
            claims[0].verdict = "contradicted"
        return BatchedClaims(
            claims=claims,
            coverage=Coverage(
                complete=True, findings=[], covered_regions=[r.id for r in self.req.source.regions]
            ),
        )


def test_batched_claims_contract(request_data):
    for contradiction, expected in [(False, "verified"), (True, "incorrect")]:
        provider = ClaimsProvider(request_data, contradiction=contradiction)
        result = LiveVerifier("batched_claims", provider).verify(request_data)
        assert result.verdict == expected and len(provider.calls) == 1
        validate_result(request_data, result)
    with pytest.raises(ValueError, match="exactly once"):
        LiveVerifier("batched_claims", ClaimsProvider(request_data, omit=True)).verify(request_data)


def test_shared_budget_is_atomic_and_stops_before_network(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "placeholder")
    budget = RequestBudget(7)
    sent = []

    def send(*args, **kwargs):
        sent.append(True)
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps({"region_id": "p1:r1", "quote": "ok"})
                            },
                        }
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", send)

    def attempt(_):
        provider = ChatProvider("test", api_key_env="TEST_KEY", request_budget=budget)
        try:
            provider.complete("test", {}, Evidence)
            return True
        except ValueError as exc:
            assert "Shared model call budget exhausted" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(30))) == 7
    assert len(sent) == budget.used == 7
