"""Generate reproducible PDF verifier cases from structured truth; no models or credentials.

Family-level dev/test separation is fixed. The seed controls data, not split assignment.
PDF coordinates establish evidence mappings; labels never come from an extractor.
"""

import argparse
import copy
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from invoices.models import Draft, Invoice, fields
from invoices.source import read_source

FAMILIES = {
    "standard_table": "dev",
    "totals_first": "dev",
    "item_cards": "dev",
    "reversed_columns": "dev",
    "wrapped_descriptions": "dev",
    "repeated_headers": "dev",
    "compact_ledger": "dev",
    "adjustments": "dev",
    "landscape_manifest": "test",
    "remittance_parties": "test",
    "continuation_summary": "test",
    "service_blocks": "test",
}
LABEL = "synthetic_structured_truth_not_independent_human_gold"
NUMERIC = {
    "quantity",
    "unit_price",
    "amount",
    "discount",
    "subtotal",
    "tax",
    "tax_rate",
    "shipping",
    "total",
}


def money(value):
    return Decimal(value).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def make_truth(family, index, rng):
    """Choose values before selecting any rendering or evidence locations."""
    count = (3, 5, 8, 14, 24)[index % 5]
    names = [
        "Valve assembly",
        "Copper fitting",
        "Calibration service",
        "Filter cartridge",
        "Steel bracket",
    ]
    items = []
    for i in range(count):
        name = names[rng.randrange(len(names))]
        if family == "wrapped_descriptions":
            name += " industrial replacement with extended service warranty"
        if family == "service_blocks":
            name = f"Technical support phase {i + 1}"
        quantity = Decimal(rng.choice([2, 3, 7, 12, 20, 40]))
        if index % 2:
            quantity += Decimal(".25")
        price = Decimal(rng.choice(["1.25", "12.40", "48.75", "125.00", "1200.50"]))
        discount = Decimal("2.50") if family == "adjustments" and i % 2 == 0 else None
        items.append(
            {
                "name": name,
                "quantity": str(quantity),
                "unit_price": str(price),
                "amount": str(money(quantity * price - (discount or 0))),
                "discount": str(discount) if discount is not None else None,
            }
        )
    # Force a genuine repeated product with a different quantity; never deduplicate by name.
    items[1]["name"] = items[0]["name"]
    items[1]["quantity"] = str(Decimal(items[0]["quantity"]) + 3)
    items[1]["amount"] = str(
        money(
            Decimal(items[1]["quantity"]) * Decimal(items[1]["unit_price"])
            - Decimal(items[1]["discount"] or 0)
        )
    )
    subtotal = sum(Decimal(it["amount"]) for it in items)
    tax = money(subtotal * Decimal(".075"))
    shipping = Decimal("18.50") if index % 2 else Decimal(0)
    discount = Decimal("5.00") if index % 3 == 0 else Decimal(0)
    issued = date(2026, 1, 1) + timedelta(days=rng.randrange(240))
    return Invoice.model_validate(
        {
            "number": f"MOCK-{list(FAMILIES).index(family) + 1:02}-{index + 1:03}",
            "vendor": f"{family.replace('_', ' ').title()} Supply {index + 1:02} Ltd.",
            "date": issued.isoformat(),
            "due_date": (issued + timedelta(days=30)).isoformat(),
            "currency": ("USD", "EUR", "GBP", "CAD", "USD")[index % 5],
            "items": items,
            "subtotal": str(subtotal),
            "tax": str(tax),
            "tax_rate": ".075",
            "shipping": str(shipping),
            "discount": str(discount),
            "total": str(subtotal + tax + shipping - discount),
        }
    ).model_dump(mode="json")


class Renderer:
    """Record field anchors while drawing; recover citations from the resulting PDF."""

    def __init__(self, path, truth, family, index):
        self.truth, self.family, self.index = truth, family, index
        self.width, self.height = landscape(A4) if family == "landscape_manifest" else A4
        self.canvas = Canvas(str(path), pagesize=(self.width, self.height), invariant=1)
        self.canvas.setTitle(truth["number"])
        self.anchors = {}
        self.page = 0
        self.new_page()

    def text(self, x, y, text, field=None, size=10, bold=False):
        text = str(text)
        self.canvas.setFillColor(colors.HexColor("#18283b"))
        self.canvas.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        self.canvas.drawString(x, y, text)
        if field:
            self.anchors.setdefault(field, []).append(
                {"page": self.page, "x": x, "top": self.height - y, "text": text}
            )

    def new_page(self):
        if self.page:
            self.canvas.showPage()
        self.page += 1
        self.text(
            40,
            self.height - 35,
            "INVOICE" if self.page == 1 else "INVOICE CONTINUED",
            size=19,
            bold=True,
        )
        self.text(self.width - 160, self.height - 35, self.truth["number"], size=9)
        self.text(40, 24, f"Synthetic evaluation document | Page {self.page}", size=8)
        self.y = self.height - 65

    def room(self, amount):
        if self.y - amount < 65:
            self.new_page()

    def header(self):
        labels = {
            "number": "Invoice no.",
            "vendor": "Supplier",
            "date": "Issue date",
            "due_date": "Payment due",
            "currency": "Invoice currency (ISO)",
        }
        keys = list(labels)
        if self.family == "remittance_parties":
            self.text(
                40, self.y, "Bill to: Example Buyer LLC | Ship to: Receiving Depot", bold=True
            )
            self.y -= 19
            self.text(40, self.y, "Remit via: Example Payment Processor (not the supplier)", size=9)
            self.y -= 24
        if self.family in {"totals_first", "reversed_columns"}:
            keys = ["vendor", "currency", "number", "due_date", "date"]
        for key in keys:
            self.text(40, self.y, labels[key] + ":", size=9)
            self.text(175, self.y, self.truth[key], key)
            self.y -= 18
        self.text(
            40,
            self.y,
            f"Purchase order: PO-{10000 + self.index} | Customer account: AC-{300 + self.index}",
            size=9,
        )
        self.y -= 25

    def fmt(self, key, value):
        if key in {"quantity", "tax_rate"}:
            return str(value)
        return f"{Decimal(value):,.2f}" if self.index % 2 == 0 else f"{Decimal(value):.2f}"

    def totals(self):
        self.room(155)
        self.text(40, self.y, "INVOICE TOTALS", bold=True)
        self.y -= 20
        for key, label in [
            ("subtotal", "Subtotal after line discounts"),
            ("discount", "Invoice discount"),
            ("shipping", "Shipping"),
            ("tax_rate", "Tax rate (decimal fraction)"),
            ("tax", "Tax amount"),
            ("total", "Grand total"),
        ]:
            self.text(40, self.y, label + ":", size=10)
            self.text(260, self.y, self.fmt(key, self.truth[key]), key, bold=key == "total")
            self.y -= 18
        self.y -= 15

    def draw(self):
        self.header()
        if self.family == "totals_first":
            self.totals()
        if self.family in {"item_cards", "service_blocks"}:
            self.cards()
        else:
            self.table()
        if self.family == "continuation_summary":
            self.new_page()
            self.text(40, self.y, "Final summary - amounts apply to all preceding pages", bold=True)
            self.y -= 28
        if self.family != "totals_first":
            self.totals()
        self.canvas.save()
        return self.anchors

    def cards(self):
        for i, item in enumerate(self.truth["items"]):
            self.room(102)
            self.text(40, self.y, f"Line {i + 1}", bold=True)
            self.y -= 18
            self.text(55, self.y, item["name"], f"items.{i}.name")
            self.y -= 18
            for x, key, label in [
                (55, "quantity", "Quantity"),
                (230, "unit_price", "Rate"),
                (400, "amount", "Amount"),
            ]:
                self.text(x, self.y, label, size=9)
                self.text(x, self.y - 16, self.fmt(key, item[key]), f"items.{i}.{key}")
            self.y -= 48

    def table(self):
        reverse = self.family in {"reversed_columns", "landscape_manifest"}
        columns = [
            ("name", "Description", 40),
            ("quantity", "Qty", 300),
            ("unit_price", "Unit price", 355),
            ("amount", "Amount", 460),
        ]
        if reverse:
            columns = [
                ("quantity", "Qty", 40),
                ("amount", "Amount", 100),
                ("unit_price", "Unit price", 200),
                ("name", "Description", 295),
            ]
        if self.family == "adjustments":
            columns = [
                ("name", "Description", 40),
                ("quantity", "Qty", 255),
                ("unit_price", "Unit price", 300),
                ("discount", "Discount", 380),
                ("amount", "Net amount", 460),
            ]
        if self.family == "landscape_manifest":
            columns = [(k, label, x + (80 if k == "name" else 0)) for k, label, x in columns]

        def heading():
            for _, label, x in columns:
                self.text(x, self.y, label, size=9, bold=True)
            self.y -= 22

        heading()
        per_page = 5 if self.family in {"repeated_headers", "continuation_summary"} else None
        for i, item in enumerate(self.truth["items"]):
            row_height = (
                40
                if self.family == "wrapped_descriptions"
                else (18 if self.family == "compact_ledger" else 27)
            )
            if self.y - row_height < 80 or (per_page and i and i % per_page == 0):
                self.new_page()
                heading()
            for key, _, x in columns:
                if item[key] is None:
                    self.text(x, self.y, "-", size=9)
                    continue
                value = item[key] if key == "name" else self.fmt(key, item[key])
                if key == "name" and self.family == "wrapped_descriptions":
                    cut = value.rfind(" ", 0, 39)
                    self.text(x, self.y, value[:cut], f"items.{i}.{key}", size=9)
                    self.text(x, self.y - 13, value[cut + 1 :], f"items.{i}.{key}", size=9)
                else:
                    self.text(x, self.y, value, f"items.{i}.{key}", size=9)
            self.y -= row_height
        self.y -= 20


def ground(source, truth, anchors):
    citations = {}
    for key in fields(Invoice.model_validate(truth)):
        refs = []
        for anchor in anchors[key]:
            candidates = [
                r
                for r in source.regions
                if r.page == anchor["page"]
                and r.bbox[1] <= anchor["top"] <= r.bbox[3] + 2
                and r.bbox[0] - 2 <= anchor["x"] <= r.bbox[2] + 2
            ]
            candidates = [r for r in candidates if anchor["text"] in r.text]
            if len(candidates) != 1:
                raise ValueError(f"Unresolved source anchor: {key} {anchor}: {candidates}")
            refs.append(candidates[0])
        citations[key] = {"region_id": refs[0].id, "quote": refs[0].text}
    return Draft.model_validate(
        {"source_hash": source.hash, "version": 1, "invoice": truth, "citations": citations}
    ).model_dump(mode="json")


def variants(gold):
    output = [("clean", copy.deepcopy(gold), True)]
    equivalent = copy.deepcopy(gold)
    for key, value in fields(Invoice.model_validate(equivalent["invoice"])).items():
        if key.split(".")[-1] in NUMERIC:
            set_field(equivalent, key, format(Decimal(value), ".4f"))
    output.append(("clean_numeric_format", equivalent, True))
    for kind in [
        "quantity",
        "decimal_shift",
        "swapped_columns",
        "swapped_row_quantities",
        "omitted_line",
        "duplicated_line",
        "wrong_citation",
        "vendor",
        "due_date",
        "currency",
        "total",
        "consistent_wrong_numbers",
    ]:
        draft = copy.deepcopy(gold)
        inv = draft["invoice"]
        first = inv["items"][0]
        if kind == "quantity":
            first["quantity"] = str(Decimal(first["quantity"]) + 1)
        elif kind == "decimal_shift":
            first["unit_price"] = str(Decimal(first["unit_price"]) * 10)
        elif kind == "swapped_columns":
            first["quantity"], first["unit_price"] = first["unit_price"], first["quantity"]
        elif kind == "swapped_row_quantities":
            first["quantity"], inv["items"][1]["quantity"] = (
                inv["items"][1]["quantity"],
                first["quantity"],
            )
        elif kind == "omitted_line":
            i = len(inv["items"]) - 1
            inv["items"].pop()
            draft["citations"] = {
                k: v for k, v in draft["citations"].items() if not k.startswith(f"items.{i}.")
            }
        elif kind == "duplicated_line":
            i = len(inv["items"])
            inv["items"].append(copy.deepcopy(first))
            for key, value in gold["citations"].items():
                if key.startswith("items.0."):
                    draft["citations"][key.replace("items.0.", f"items.{i}.")] = copy.deepcopy(
                        value
                    )
        elif kind == "wrong_citation":
            draft["citations"]["items.0.quantity"] = copy.deepcopy(gold["citations"]["number"])
        elif kind == "vendor":
            inv["vendor"] = "Example Buyer LLC"
        elif kind == "due_date":
            inv["due_date"] = inv["date"]
        elif kind == "currency":
            inv["currency"] = "EUR" if inv["currency"] == "USD" else "USD"
        elif kind == "total":
            inv["total"] = str(Decimal(inv["total"]) + Decimal(".01"))
        elif kind == "consistent_wrong_numbers":
            first["quantity"] = str(Decimal(first["quantity"]) + 1)
            first["amount"] = str(
                money(
                    Decimal(first["quantity"]) * Decimal(first["unit_price"])
                    - Decimal(first["discount"] or 0)
                )
            )
            subtotal = sum(Decimal(it["amount"]) for it in inv["items"])
            inv["subtotal"] = str(subtotal)
            inv["tax"] = str(money(subtotal * Decimal(inv["tax_rate"])))
            inv["total"] = str(
                subtotal + Decimal(inv["tax"]) + Decimal(inv["shipping"]) - Decimal(inv["discount"])
            )
        output.append((kind, draft, False))
    return output


def set_field(draft, key, value):
    parts = key.split(".")
    if parts[0] == "items":
        draft["invoice"]["items"][int(parts[1])][parts[2]] = value
    else:
        draft["invoice"][key] = value


def error_fields(gold, draft):
    expected = fields(Invoice.model_validate(gold["invoice"]))
    actual = fields(Invoice.model_validate(draft["invoice"]))
    errors = []
    for key in expected.keys() | actual.keys():
        left, right = expected.get(key), actual.get(key)
        if left is not None and right is not None and key.split(".")[-1] in NUMERIC:
            left, right = Decimal(left), Decimal(right)
        if left != right or gold["citations"].get(key) != draft["citations"].get(key):
            errors.append(key)
    return sorted(errors)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def build(output, per_family=5, seed=20261031):
    output = Path(output).resolve()
    # Refuse overwriting a frozen dataset: use a new directory/version for revisions.
    output.mkdir(parents=True, exist_ok=False)
    (output / "sources").mkdir()
    (output / "truth").mkdir()
    cases, documents = [], []
    for family, split in FAMILIES.items():
        for index in range(per_family):
            rng = random.Random(f"{seed}:{family}:{index}")
            truth = make_truth(family, index, rng)
            doc_id = f"{family}-{index + 1:03}"
            path = output / "sources" / f"{doc_id}.pdf"
            anchors = Renderer(path, truth, family, index).draw()
            source = read_source(path)
            gold = ground(source, truth, anchors)
            write_json(
                output / "truth" / f"{doc_id}.json",
                {"invoice": truth, "anchors": anchors, "gold": gold},
            )
            try:
                source_path = str(path.relative_to(ROOT))
            except ValueError:
                source_path = str(path)
            documents.append(
                {
                    "id": doc_id,
                    "family": family,
                    "split": split,
                    "source": source_path,
                    "source_hash": source.hash,
                    "pages": len(source.page_sizes),
                    "items": len(truth["items"]),
                }
            )
            for kind, draft, correct in variants(gold):
                errors = error_fields(gold, draft)
                assert correct == (not errors), (doc_id, kind, errors)
                Draft.model_validate(draft)
                cases.append(
                    {
                        "id": f"{doc_id}:{kind}",
                        "source": source_path,
                        "source_hash": source.hash,
                        "group": doc_id,
                        "family": family,
                        "split": split,
                        "kind": kind,
                        "correct": correct,
                        "label_status": LABEL,
                        "gold": gold,
                        "draft": draft,
                        "error_fields": errors,
                    }
                )
    for split in ("dev", "test"):
        write_json(output / f"{split}-cases.json", [c for c in cases if c["split"] == split])
    manifest = {
        "version": 1,
        "seed": seed,
        "per_family": per_family,
        "label_status": LABEL,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "families": FAMILIES,
        "documents": documents,
        "counts": {
            "sources": len(documents),
            "cases": len(cases),
            "clean": sum(c["correct"] for c in cases),
            "corrupted": sum(not c["correct"] for c in cases),
            "by_split": dict(Counter(c["split"] for c in cases)),
            "by_kind": dict(Counter(c["kind"] for c in cases)),
        },
        "files_sha256": {
            str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.rglob("*"))
            if p.is_file()
        },
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/mock-v1")
    parser.add_argument("--per-family", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261031)
    args = parser.parse_args()
    if args.per_family < 1:
        parser.error("--per-family must be positive")
    print(json.dumps(build(args.output, args.per_family, args.seed)["counts"], indent=2))


if __name__ == "__main__":
    main()
