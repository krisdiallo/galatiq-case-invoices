import json
from pathlib import Path

from .chunking import (
    ChunkPolicy,
    RequestCapacityError,
    bisect_source,
    merge_extractions,
    project_draft,
    source_chunks,
)
from .models import Draft, Evidence, Invoice, Model

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/invoices.json"


class Extraction(Model):
    invoice: Invoice
    citations: dict[str, Evidence]


class OfflineExtractor:
    identity = "offline:curated-fixtures"

    def extract(self, source, version=1, previous=None, findings=None):
        fixture = json.loads(FIXTURES.read_text()).get(source.hash)
        if not fixture:
            fixture = json.loads(FIXTURES.with_name("demo.json").read_text()).get(source.hash)
        if not fixture:
            raise ValueError(
                "No offline fixture for this exact source. Use live mode for new documents."
            )
        draft = Draft.model_validate(fixture["draft"])
        return draft.model_copy(update={"version": version}, deep=True)


class LiveExtractor:
    def __init__(self, provider):
        self.provider = provider
        self.identity = provider.identity
        self.on_progress = None

    def extract(self, source, version=1, previous=None, findings=None):
        parts = []
        self.chunks = []

        def process(chunk):
            def progress(status):
                if self.on_progress:
                    self.on_progress(
                        {
                            "stage": "extraction",
                            "status": status,
                            "pages": sorted({r.page for r in chunk.regions}),
                            "completed": len(parts),
                        }
                    )

            progress("started")
            local_previous, mapping = (
                project_draft(previous, chunk) if previous is not None else (None, {})
            )
            inverse = {global_field: local for local, global_field in mapping.items()}
            ids = {r.id for r in chunk.regions}
            local_findings = [
                f.model_copy(update={"field": inverse.get(f.field, f.field)})
                for f in (findings or [])
                if not f.evidence or any(e.region_id in ids for e in f.evidence)
            ]
            try:
                part = self._extract_chunk(chunk, version, local_previous, local_findings)
            except RequestCapacityError:
                progress("splitting")
                for smaller in bisect_source(chunk):
                    process(smaller)
                return
            parts.append(part)
            self.chunks.append({"regions": [r.id for r in chunk.regions], "status": "processed"})
            progress("completed")

        for chunk in source_chunks(source, getattr(self.provider, "chunk_policy", ChunkPolicy())):
            process(chunk)
        return merge_extractions(parts, source.hash, version)

    def _extract_chunk(self, source, version=1, previous=None, findings=None):
        payload = {"source": source.payload()}
        if previous is not None:
            payload.update(
                previous=previous.model_dump(mode="json"),
                findings=[f.model_dump() for f in findings],
            )
        result = self.provider.complete(
            "Extract this invoice segment faithfully. Other segments may contain headers or totals; "
            "leave fields absent from this segment null. Do not treat page subtotals as invoice totals. "
            "Preserve every line in order, including repeated items. "
            "Do not correct arithmetic, quantities or source mistakes. Use null for absent values. "
            "Normalize unambiguous dates to ISO and item spacing, preserve uncertain dates literally. "
            "Money must be decimal strings. Do not infer USD from a dollar sign alone; "
            "use null when the currency is not unambiguously identified by the source. "
            "Discount is an absolute amount, not a rate. "
            "Cite every non-null scalar field with its dotted field path (items.0.quantity etc.), "
            "a provided region_id, and an exact substring quote. Do not invent regions or source text. "
            "Do not compute missing monetary fields. A revision must address findings using the source.",
            payload,
            Extraction,
        )
        return Draft(source_hash=source.hash, version=version, **result.model_dump())
