from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from pathlib import Path

import pdfplumber
from PIL import Image
from pydantic import Field

from .models import Model

# PDFium's native font/rendering state is shared across documents. Concurrent
# to_image calls can abort the process; serialize rendering, not API requests.
_PDF_RENDER_LOCK = threading.Lock()


class Region(Model):
    id: str
    page: int
    text: str
    bbox: list[float] | None = None


class Source(Model):
    path: str
    hash: str
    format: str
    regions: list[Region]
    render_pdf: bool = Field(default=False, exclude=True)
    page_sizes: list[list[float]] = Field(default_factory=list, exclude=True)

    def payload(self):
        return self.model_dump(exclude={"images", "page_sizes", "path"})

    def region(self, region_id):
        return next((r for r in self.regions if r.id == region_id), None)

    def view(self, region_ids):
        # Keep column headers/footnotes available; region IDs are focus hints, not a filter.
        return {**self.payload(), "focus_regions": region_ids}

    def subset(self, regions):
        return self.model_copy(update={"regions": list(regions)})

    @property
    def images(self):
        return self.page_images()

    def page_images(self, pages=None):
        # Render only the active request, never every page of a long PDF at ingestion.
        if not self.render_pdf:
            return []
        pages = sorted(pages if pages is not None else {r.page for r in self.regions})
        result = []
        raw = Path(self.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.hash:
            raise ValueError("Source changed after ingestion")
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for number in pages:
                page = pdf.pages[number - 1]
                resolution = min(130, 1024 * 72 / max(page.width, page.height))
                with _PDF_RENDER_LOCK:
                    bitmap = page.to_image(resolution=resolution).original
                output = io.BytesIO()
                bitmap.save(output, format="PNG")
                result.append(
                    "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
                )
        return result

    def focused_images(self, region_ids):
        if not self.render_pdf:
            return []
        selected = [r for r in self.regions if r.id in region_ids and r.bbox]
        if not selected:
            return self.images
        images = []
        for page in sorted({r.page for r in selected}):
            original = self.page_images([page])[0]
            images.append(original)
            regions = [r for r in selected if r.page == page]
            bitmap = Image.open(io.BytesIO(base64.b64decode(original.split(",", 1)[1])))
            scale = bitmap.height / self.page_sizes[page - 1][1]
            top = max(0, int((min(r.bbox[1] for r in regions) - 12) * scale))
            bottom = min(
                bitmap.height, max(top + 1, int((max(r.bbox[3] for r in regions) + 12) * scale))
            )
            output = io.BytesIO()
            bitmap.crop((0, top, bitmap.width, bottom)).save(output, format="PNG")
            images.append("data:image/png;base64," + base64.b64encode(output.getvalue()).decode())
        return images


def read_source(path: str | Path, render: bool = False) -> Source:
    path = Path(path)
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".txt", ".json", ".csv", ".xml"}:
        raise ValueError(f"Unsupported source format: {suffix}")
    regions, page_sizes = [], []
    if suffix == ".pdf":
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for p, page in enumerate(pdf.pages, 1):
                page_sizes.append([page.width, page.height])
                lines = page.extract_text_lines()
                if not lines:
                    raise ValueError(f"Page {p} has no usable text layer; OCR is not implemented")
                for i, line in enumerate(lines, 1):
                    regions.append(
                        Region(
                            id=f"p{p}:r{i}",
                            page=p,
                            text=line["text"],
                            bbox=[line[k] for k in ("x0", "top", "x1", "bottom")],
                        )
                    )
    else:
        text = raw.decode("utf-8-sig")
        # Pretty-print structured JSON to give each value a stable, small citation region.
        if suffix == ".json":
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip():
                regions.append(Region(id=f"p1:r{i}", page=1, text=line))
    if not regions:
        raise ValueError("Empty source")
    return Source(
        path=str(path),
        hash=hashlib.sha256(raw).hexdigest(),
        format=suffix[1:],
        regions=regions,
        render_pdf=render and suffix == ".pdf",
        page_sizes=page_sizes,
    )


def evidence_errors(source, draft):
    from .models import fields

    errors = []
    if source.hash != draft.source_hash:
        errors.append("Draft belongs to a different source")
    for field in fields(draft.invoice):
        citation = draft.citations.get(field)
        region = source.region(citation.region_id) if citation else None
        if not citation or not region or not citation.quote or citation.quote not in region.text:
            errors.append(f"{field}: missing or invalid source citation")
    return errors
