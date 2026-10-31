from pathlib import Path

import pytest

from invoices.agents import OfflineExtractor
from invoices.models import Assessment, Evidence, Finding, fields
from invoices.source import read_source
from invoices.verification import (
    Claim,
    Coverage,
    LiveVerifier,
    Transcript,
    VerificationRequest,
    result_for,
    validate_result,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def request_data():
    source = read_source(ROOT / "data/invoices/invoice_1001.txt")
    return VerificationRequest(source, OfflineExtractor().extract(source))


class RecordingProvider:
    identity = "test:model"

    def __init__(self, req):
        self.req = req
        self.calls = []
        self.inputs = []

    def complete(self, instruction, payload, model, images=(), source=None):
        assert source is None, "Verification must not expose source-reading tools"
        self.inputs.append((instruction, payload, model))
        self.calls.append({"usage": {}})
        req = self.req
        if model is Transcript:
            assert "draft" not in payload
            assert "citations" not in payload
            return Transcript(
                text="Independent transcription",
                covered_regions=payload["transcribe_regions"],
                unclear_regions=[],
            )
        if model is Claim:
            return Claim(
                verdict="supported",
                reason="Supported",
                evidence=[
                    Evidence(region_id=req.source.regions[0].id, quote=req.source.regions[0].text)
                ],
            )
        if model is Coverage:
            return Coverage(
                complete=True, covered_regions=[r.id for r in req.source.regions], findings=[]
            )
        return Assessment(
            verdict="verified",
            findings=[],
            checked_fields=list(fields(req.draft.invoice)),
            covered_regions=[r.id for r in req.source.regions],
            complete=True,
        )


@pytest.mark.parametrize("strategy", ["direct", "field_by_field", "blind_transcription"])
def test_live_strategies_have_common_contract(request_data, strategy):
    provider = RecordingProvider(request_data)
    result = LiveVerifier(strategy, provider).verify(request_data)
    validate_result(request_data, result)
    assert result.verdict == "verified"
    if strategy == "direct":
        assert len(provider.inputs) == 1
        assert provider.inputs[0][1]["required_checked_fields"] == list(
            fields(request_data.draft.invoice)
        )
    if strategy == "field_by_field":
        questions = [p["question"] for _, p, m in provider.inputs if m is Claim]
        assert len(questions) == len(fields(request_data.draft.invoice))
        assert any("items.0.quantity" in q and "WidgetA" in q and "10" in q for q in questions)
        assert all("item None" not in q for q in questions)
        number = next(
            p for _, p, m in provider.inputs if m is Claim and p["field_path"] == "number"
        )
        assert number["question"].startswith("On the invoice,")
        assert number["proposed_value"] == request_data.draft.invoice.number
        assert provider.inputs[-1][2] is Coverage
    if strategy == "blind_transcription":
        assert all("draft" not in p for _, p, m in provider.inputs if m is Transcript)
        assert provider.inputs[-1][1]["draft"] == request_data.draft.model_dump(mode="json")
        assert provider.inputs[-1][1]["required_checked_fields"] == list(
            fields(request_data.draft.invoice)
        )


def test_contract_rejects_false_claim_of_coverage(request_data):
    assessment = Assessment(
        verdict="verified", findings=[], checked_fields=[], covered_regions=[], complete=True
    )
    with pytest.raises(ValueError, match="coverage"):
        validate_result(request_data, result_for(request_data, "direct", assessment))


def test_contract_rejects_stale_draft(request_data):
    provider = RecordingProvider(request_data)
    result = LiveVerifier("direct", provider).verify(request_data)
    request_data.draft.version = 2
    with pytest.raises(ValueError, match="version"):
        validate_result(request_data, result)


def test_invented_evidence_cannot_authorize_revision(request_data):
    assessment = Assessment(
        verdict="incorrect",
        findings=[
            Finding(
                field="items", reason="Wrong", evidence=[Evidence(region_id="p1:r999", quote="20")]
            )
        ],
        checked_fields=[],
        covered_regions=[],
        complete=False,
    )
    with pytest.raises(ValueError, match="evidence"):
        validate_result(request_data, result_for(request_data, "direct", assessment))


def test_supported_claims_with_incomplete_coverage_do_not_verify(request_data):
    class Incomplete(RecordingProvider):
        def complete(self, instruction, payload, model, **kwargs):
            if model is Coverage:
                return Coverage(complete=False, covered_regions=[], findings=[])
            return super().complete(instruction, payload, model, **kwargs)

    result = LiveVerifier("field_by_field", Incomplete(request_data)).verify(request_data)
    assert result.verdict == "uncertain"


def test_pdf_regions_have_geometry_and_image():
    source = read_source(ROOT / "data/invoices/invoice_1011.pdf", render=True)
    assert source.images[0].startswith("data:image/png;base64,")
    assert all(r.bbox and len(r.bbox) == 4 for r in source.regions)
    focused = source.focused_images([source.regions[4].id])
    assert len(focused) == 2
    assert focused[0] == source.images[0]
