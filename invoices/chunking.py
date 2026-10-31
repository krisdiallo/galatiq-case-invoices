"""Request-sized work units. These targets never limit document eligibility."""

import json
from dataclasses import dataclass

from .models import Draft, Invoice, Item, fields


class RequestCapacityError(ValueError):
    """Retry on smaller work units, not as a business rejection."""


@dataclass(frozen=True)
class ChunkPolicy:
    source_bytes: int | None = None
    pages: int | None = None

    def __post_init__(self):
        if any(value is not None and value < 1 for value in (self.source_bytes, self.pages)):
            raise ValueError("Chunk targets must be positive")


def source_chunks(source, policy):
    """Partition without dropping regions; oversized individual regions split on retry."""
    batch, size, pages = [], 0, set()
    for region in source.regions:
        weight = len(json.dumps(region.model_dump()).encode())
        if batch and (
            (policy.source_bytes is not None and size + weight > policy.source_bytes)
            or (policy.pages is not None and len(pages | {region.page}) > policy.pages)
        ):
            yield source.subset(batch)
            batch, size, pages = [], 0, set()
        batch.append(region)
        size += weight
        pages.add(region.page)
    if batch:
        yield source.subset(batch)


def bisect_source(source):
    """Strictly smaller source units ensure capacity retries terminate."""
    pages = sorted({r.page for r in source.regions})
    if len(pages) > 1:
        boundary = pages[len(pages) // 2]
        return (
            source.subset([r for r in source.regions if r.page < boundary]),
            source.subset([r for r in source.regions if r.page >= boundary]),
        )
    if len(source.regions) > 1:
        middle = len(source.regions) // 2
        return source.subset(source.regions[:middle]), source.subset(source.regions[middle:])
    region = source.regions[0]
    if len(region.text) > 1:
        middle = len(region.text) // 2
        # Preserve the original ID: quotes remain substrings of the original region.
        return tuple(
            source.subset([region.model_copy(update={"text": part})])
            for part in (region.text[:middle], region.text[middle:])
        )
    raise RequestCapacityError(
        "Even a single source character exceeds request capacity; check provider settings"
    )


def project_draft(draft, source):
    """Route fields by citation, preserving a reversible local/global item mapping."""
    values, citations, mapping, items = {}, {}, {}, {}
    for path, value in fields(draft.invoice).items():
        ev = draft.citations.get(path)
        region = source.region(ev.region_id) if ev else None
        if not region or ev.quote not in region.text:
            continue
        if path.startswith("items."):
            _, index, key = path.split(".")
            items.setdefault(int(index), {})[key] = value
        else:
            values[path] = value
            citations[path] = ev
            mapping[path] = path
    values["items"] = []
    for local, (index, item) in enumerate(sorted(items.items())):
        values["items"].append(Item.model_validate(item))
        for key in item:
            old, new = f"items.{index}.{key}", f"items.{local}.{key}"
            mapping[new] = old
            citations[new] = draft.citations[old]
        mapping[f"items.{local}"] = f"items.{index}"
    return Draft(
        source_hash=draft.source_hash,
        version=draft.version,
        invoice=Invoice.model_validate(values),
        citations=citations,
    ), mapping


def global_path(path, mapping):
    if path in mapping:
        return mapping[path]
    if path.startswith("items."):
        prefix = ".".join(path.split(".")[:2])
        if prefix in mapping:
            return mapping[prefix] + path[len(prefix) :]
    return path


def merge_extractions(parts, source_hash, version):
    """Merge in source order. Never silently choose between conflicting headers."""
    invoice, citations, seen = Invoice(), {}, set()
    for part in parts:
        for key in Invoice.model_fields:
            if key == "items":
                continue
            value = getattr(part.invoice, key)
            if value is None:
                continue
            old = getattr(invoice, key)
            if old is not None and old != value:
                raise ValueError(
                    f"Conflicting {key} across extraction chunks; reconciliation required"
                )
            if old is None:
                setattr(invoice, key, value)
                if key in part.citations:
                    citations[key] = part.citations[key]
        for i, item in enumerate(part.invoice.items):
            evs = {
                k.split(".", 2)[2]: v
                for k, v in part.citations.items()
                if k.startswith(f"items.{i}.")
            }
            identity = json.dumps(
                {
                    "item": item.model_dump(mode="json"),
                    "evidence": {k: v.model_dump() for k, v in evs.items()},
                },
                sort_keys=True,
            )
            # Equal values on different physical source rows remain separate items.
            if evs and identity in seen:
                continue
            seen.add(identity)
            index = len(invoice.items)
            invoice.items.append(item)
            citations.update({f"items.{index}.{k}": ev for k, ev in evs.items()})
    return Draft(source_hash=source_hash, version=version, invoice=invoice, citations=citations)
