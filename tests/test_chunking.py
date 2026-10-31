"""Functional tests with deterministic model doubles, NOT long-document accuracy evals."""

import sqlite3
from decimal import Decimal

import pytest
from fpdf import FPDF

from invoices.agents import Extraction, LiveExtractor
from invoices.chunking import ChunkPolicy, RequestCapacityError
from invoices.models import Assessment, Draft, Evidence, Finding, Invoice, Item, fields
from invoices.pipeline import Pipeline
from invoices.providers import ChatProvider
from invoices.source import read_source
from invoices.store import Store
from invoices.verification import (
    Claim,
    Coverage,
    LiveVerifier,
    Transcript,
    VerificationRequest,
    validate_result,
)


def make_pdf(path, pages):
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    for i in range(pages):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        lines = (
            ["Invoice: LONG", "Vendor: Test Vendor", "Due: 2026-12-01", "Currency: USD"]
            if i == 0
            else []
        )
        lines += ["Item WidgetA quantity 1 unit 2 amount 2"]
        if i == pages - 1:
            lines += [f"Total: {2 * pages}"]
        for n, line in enumerate(lines):
            pdf.text(20, 20 + n * 10, line)
    pdf.output(str(path))


class FunctionalProvider:
    identity = "test:functional-only"

    def __init__(self, capacity_pages=2, fail_page=None):
        self.chunk_policy = ChunkPolicy()
        self.capacity_pages = capacity_pages
        self.fail_page = fail_page
        self.calls = []
        self.payloads = []
        self.capacity_retries = 0

    def complete(self, instruction, payload, model, images=(), source=None):
        assert source is None
        regions = payload.get("source", {}).get("regions", payload.get("regions", []))
        pages = {r["page"] for r in regions}
        if len(pages) > self.capacity_pages:
            self.capacity_retries += 1
            raise RequestCapacityError("Simulated provider context limit")
        if self.fail_page in pages:
            raise RuntimeError("Simulated provider unavailable midway through document")
        self.calls.append({"usage": {}})
        self.payloads.append(payload)
        if model is Extraction:
            invoice, citations = Invoice(), {}
            for r in regions:
                text = r["text"]
                ev = Evidence(region_id=r["id"], quote=text)
                for label, field in [
                    ("Invoice:", "number"),
                    ("Vendor:", "vendor"),
                    ("Due:", "due_date"),
                    ("Currency:", "currency"),
                    ("Total:", "total"),
                ]:
                    if text.startswith(label):
                        value = text.split(":", 1)[1].strip()
                        setattr(invoice, field, Decimal(value) if field == "total" else value)
                        citations[field] = ev
                if text.startswith("Item "):
                    i = len(invoice.items)
                    invoice.items.append(Item(name="WidgetA", quantity=1, unit_price=2, amount=2))
                    for name in ["name", "quantity", "unit_price", "amount"]:
                        citations[f"items.{i}.{name}"] = ev
            return Extraction(invoice=invoice, citations=citations)
        if model is Transcript:
            assert "draft" not in payload and "document_header_context" not in payload
            return Transcript(
                text="\n".join(r["text"] for r in regions),
                covered_regions=payload["transcribe_regions"],
                unclear_regions=[],
            )
        if model is Claim:
            return Claim(
                verdict="supported",
                reason="functional double",
                evidence=[Evidence.model_validate(payload["citation"])],
            )
        if model is Coverage:
            return Coverage(complete=True, findings=[], covered_regions=[r["id"] for r in regions])
        return Assessment(
            verdict="verified",
            findings=[],
            complete=True,
            checked_fields=list(fields(Draft.model_validate(payload["draft"]).invoice)),
            covered_regions=[r["id"] for r in regions],
        )


@pytest.mark.parametrize("pages", [20, 50])
@pytest.mark.parametrize("strategy", ["direct", "field_by_field", "blind_transcription"])
def test_long_pdf_extract_verify_and_global_checks(tmp_path, pages, strategy):
    path = tmp_path / "long.pdf"
    make_pdf(path, pages)
    provider = FunctionalProvider()
    source = read_source(path)
    draft = LiveExtractor(provider).extract(source)
    assert len(draft.invoice.items) == pages  # identical values on different rows are preserved
    assert draft.invoice.total == 2 * pages
    assert draft.citations[f"items.{pages - 1}.quantity"].region_id.startswith(f"p{pages}:")
    verifier = LiveVerifier(strategy, provider)
    result = verifier.verify(VerificationRequest(source, draft))
    validate_result(VerificationRequest(source, draft), result)
    assert result.verdict == "verified"
    assert set(result.covered_regions) == {r.id for r in source.regions}
    assert set(result.checked_fields) == set(fields(draft.invoice))
    assert provider.capacity_retries > 0
    assert result.metadata["chunks_processed"] > 1
    assert result.metadata["long_document_accuracy"] == "not_established"
    assert len(provider.calls) > 0


def test_fifty_page_pipeline_pays_only_after_all_chunks(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, 50)
    db = tmp_path / "inventory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
        conn.execute("INSERT INTO inventory VALUES ('WidgetA', 100)")
    result = Pipeline(
        LiveExtractor(FunctionalProvider()),
        LiveVerifier("direct", FunctionalProvider()),
        Store(tmp_path / "state.db"),
        db,
        render=True,
    ).run(path)
    assert result["status"] == "paid"
    assert result["verification"]["metadata"]["chunks_processed"] > 1
    assert len(result["draft"]["invoice"]["items"]) == 50
    assert result["receipt"]


def test_mid_document_failure_never_pays(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, 20)
    result = Pipeline(
        LiveExtractor(FunctionalProvider()),
        LiveVerifier("direct", FunctionalProvider(fail_page=12)),
        Store(tmp_path / "state.db"),
        tmp_path / "unused.db",
    ).run(path)
    assert result["status"] == "hold"
    assert result["receipt"] is None
    assert not result["checks"]
    assert "midway" in result["reason"]


def test_large_file_is_not_rejected_by_size(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("x" * 10_000_001)
    assert len(read_source(path).regions[0].text) == 10_000_001


def test_large_physical_page_render_is_bounded_and_lazy(tmp_path):
    path = tmp_path / "wide.pdf"
    pdf = FPDF(format=(3000, 3000))
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.text(20, 20, "Invoice on a very large physical page")
    pdf.output(str(path))
    source = read_source(path, render=True)
    assert source.render_pdf
    import base64
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(base64.b64decode(source.images[0].split(",")[1])))
    assert max(image.size) <= 1025


def test_provider_preflight_splits_before_network(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "test")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: pytest.fail("network must not be called")
    )
    provider = ChatProvider("test", api_key_env="TEST_KEY", request_bytes=100)
    with pytest.raises(RequestCapacityError):
        provider.complete("x", {"data": "long"}, Transcript)
    assert not provider.calls


@pytest.mark.parametrize(
    "status,body,capacity",
    [
        (413, b"private message", True),
        (400, b'{"error":{"code":"context_length_exceeded"}}', True),
        (401, b"context window private key", False),
        (400, b"bad schema", False),
    ],
)
def test_provider_capacity_error_classification(monkeypatch, status, body, capacity):
    import io
    from urllib.error import HTTPError

    monkeypatch.setenv("TEST_KEY", "test")

    def send(*args, **kwargs):
        raise HTTPError("https://invalid", status, "error", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(RequestCapacityError if capacity else RuntimeError) as exc:
        ChatProvider("test", api_key_env="TEST_KEY").complete("x", {}, Transcript)
    assert "private" not in str(exc.value)


def test_late_chunk_finding_maps_to_original_item(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, 20)
    source = read_source(path)
    draft = LiveExtractor(FunctionalProvider()).extract(source)
    draft.invoice.items[19].quantity = Decimal(2)

    class DetectQuantity(FunctionalProvider):
        def complete(self, instruction, payload, model, **kwargs):
            result = super().complete(instruction, payload, model, **kwargs)
            if model is Assessment:
                local = Draft.model_validate(payload["draft"])
                bad = [
                    p
                    for p, v in fields(local.invoice).items()
                    if p.endswith(".quantity") and v == "2"
                ]
                if bad:
                    result.verdict = "incorrect"
                    result.findings = [
                        Finding(field=p, reason="wrong quantity", evidence=[local.citations[p]])
                        for p in bad
                    ]
            return result

    result = LiveVerifier("direct", DetectQuantity()).verify(VerificationRequest(source, draft))
    validate_result(VerificationRequest(source, draft), result)
    assert result.verdict == "incorrect"
    assert result.findings[0].field == "items.19.quantity"
    assert result.findings[0].evidence[0].region_id.startswith("p20:")


def test_conflicting_chunk_headers_hold_instead_of_picking_one(tmp_path):
    from invoices.chunking import merge_extractions

    parts = [
        Draft(source_hash="s", version=1, invoice=Invoice(number=n), citations={})
        for n in ["A", "B"]
    ]
    with pytest.raises(ValueError, match="Conflicting number"):
        merge_extractions(parts, "s", 1)


def test_output_overflow_is_retried_on_smaller_chunks(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, 20)

    class OutputLimited(FunctionalProvider):
        def complete(self, instruction, payload, model, **kwargs):
            if len(payload.get("source", {}).get("regions", [])) > 2:
                raise RequestCapacityError("output capacity exceeded")
            return super().complete(instruction, payload, model, **kwargs)

    source = read_source(path)
    draft = LiveExtractor(OutputLimited()).extract(source)
    assert len(draft.invoice.items) == 20
    result = LiveVerifier("direct", OutputLimited()).verify(VerificationRequest(source, draft))
    validate_result(VerificationRequest(source, draft), result)
    assert result.verdict == "verified"


def test_provider_has_no_implicit_document_call_limit():
    assert ChatProvider("test").max_calls is None


def test_original_source_change_detected_before_lazy_render(tmp_path):
    path = tmp_path / "changed.pdf"
    make_pdf(path, 1)
    source = read_source(path, render=True)
    make_pdf(path, 2)
    with pytest.raises(ValueError, match="Source changed"):
        _ = source.images


def test_default_planner_keeps_long_source_whole(tmp_path):
    from invoices.chunking import source_chunks

    path = tmp_path / "long.pdf"
    make_pdf(path, 50)
    source = read_source(path)
    chunks = list(source_chunks(source, ChunkPolicy()))
    assert len(chunks) == 1
    assert chunks[0].regions == source.regions
    # Explicit batching is still possible, but never a document rejection.
    explicit = list(source_chunks(source, ChunkPolicy(pages=4)))
    assert len(explicit) == 13
    assert [r.id for chunk in explicit for r in chunk.regions] == [r.id for r in source.regions]


def test_default_source_byte_target_is_unset(tmp_path):
    from invoices.chunking import source_chunks

    path = tmp_path / "large.txt"
    path.write_text(("long source region " * 100 + "\n") * 100)
    source = read_source(path)
    assert len(list(source_chunks(source, ChunkPolicy()))) == 1
    assert len(list(source_chunks(source, ChunkPolicy(source_bytes=24000)))) > 1


def test_no_guessed_request_or_token_ceilings(monkeypatch):
    for name in [
        "MODEL_REQUEST_BYTES",
        "MODEL_REQUEST_IMAGES",
        "MODEL_CONTEXT_TOKENS",
        "MODEL_CHUNK_PAGES",
        "MODEL_CHUNK_SOURCE_BYTES",
    ]:
        monkeypatch.delenv(name, raising=False)
    provider = ChatProvider("test")
    assert provider.request_bytes is None and provider.max_images is None
    assert provider.chunk_policy == ChunkPolicy()
    # More than the previous 16 MB and 16 images: neither is a default rejection.
    provider.check_capacity(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "x" * 16_000_001},
                        *[
                            {"type": "image_url", "image_url": {"url": "test-image"}}
                            for _ in range(50)
                        ],
                    ],
                }
            ]
        }
    )
    assert provider.request_policy["context_enforcement"] == "provider"


def test_explicit_image_budget_is_honored():
    provider = ChatProvider("test", max_images=1)
    with pytest.raises(RequestCapacityError, match="explicit image budget"):
        provider.check_capacity(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image_url"}, {"type": "image_url"}]}
                ]
            }
        )


def test_retired_token_estimate_setting_is_not_silently_used(monkeypatch):
    monkeypatch.setenv("MODEL_CONTEXT_TOKENS", "128000")
    with pytest.raises(ValueError, match="retired"):
        ChatProvider("test")


def test_capacity_bisection_keeps_pages_together(tmp_path):
    from invoices.chunking import bisect_source

    path = tmp_path / "long.pdf"
    make_pdf(path, 3)
    source = read_source(path)
    left, right = bisect_source(source)
    assert {r.page for r in left.regions}.isdisjoint({r.page for r in right.regions})
    assert left.regions + right.regions == source.regions


def test_blind_reader_does_not_split_at_sixteen_regions(tmp_path):
    path = tmp_path / "dense.txt"
    path.write_text(
        "\n".join(
            ["Invoice: LONG", "Vendor: Test Vendor", "Due: 2026-12-01", "Currency: USD"]
            + ["Item WidgetA quantity 1 unit 2 amount 2"] * 50
            + ["Total: 100"]
        )
    )
    source = read_source(path)
    provider = FunctionalProvider()
    provider.chunk_policy = ChunkPolicy()
    draft = LiveExtractor(provider).extract(source)
    provider.payloads.clear()
    result = LiveVerifier("blind_transcription", provider).verify(
        VerificationRequest(source, draft)
    )
    validate_result(VerificationRequest(source, draft), result)
    transcripts = [p for p in provider.payloads if "transcribe_regions" in p]
    assert len(transcripts) == 1
    assert len(transcripts[0]["transcribe_regions"]) == 55
