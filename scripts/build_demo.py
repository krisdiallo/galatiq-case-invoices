"""Build a demo approval PDF under the same $10,000 policy as normal processing.

Keep these fixtures separate from the original, frozen evaluation references.
"""

import json
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from invoices.models import Draft, Evidence, Invoice, fields
from invoices.source import read_source


def main():
    invoice = Invoice.model_validate(
        {
            "number": "DEMO-APPROVAL",
            "vendor": "Summit Manufacturing Co.",
            "date": "2026-01-20",
            "due_date": "2026-02-20",
            "currency": "USD",
            "items": [
                {"name": "WidgetA", "quantity": 15, "unit_price": 250, "amount": 3750},
                {"name": "WidgetB", "quantity": 10, "unit_price": 500, "amount": 5000},
                {"name": "GadgetX", "quantity": 3, "unit_price": 750, "amount": 2250},
            ],
            "subtotal": 11000,
            "tax": 0,
            "total": 11000,
        }
    )
    path = ROOT / "data/demo/approval.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = Canvas(str(path), pagesize=A4, invariant=1)
    pdf.setTitle("Demo invoice - VP approval")
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(48, 785, "INVOICE")
    pdf.setFont("Helvetica", 11)
    expected = {}
    y = 744
    for key, label in [
        ("number", "Invoice number"),
        ("vendor", "Vendor"),
        ("date", "Issued"),
        ("due_date", "Due date"),
        ("currency", "Currency"),
    ]:
        line = f"{label}: {getattr(invoice, key)}"
        pdf.drawString(48, y, line)
        expected[key] = line
        y -= 22
    y -= 24
    columns = [
        ("name", "Item", 48),
        ("quantity", "Quantity", 220),
        ("unit_price", "Unit price", 310),
        ("amount", "Amount", 440),
    ]
    pdf.setFont("Helvetica-Bold", 11)
    for _, label, x in columns:
        pdf.drawString(x, y, label)
    pdf.setFont("Helvetica", 11)
    for i, item in enumerate(invoice.items):
        y -= 28
        values = []
        for key, _, x in columns:
            value = getattr(item, key)
            text = f"{value:,.2f}" if key in {"unit_price", "amount"} else str(value)
            pdf.drawString(x, y, text)
            values.append(text)
        for key, _, _ in columns:
            expected[f"items.{i}.{key}"] = " ".join(values)
    y -= 44
    for key, label in [("subtotal", "Subtotal"), ("tax", "Tax"), ("total", "Total")]:
        line = f"{label}: {getattr(invoice, key):,.2f} USD"
        pdf.drawString(48, y, line)
        expected[key] = line
        y -= 24
    pdf.save()
    source = read_source(path)
    citations = {}
    for key in fields(invoice):
        matches = [r for r in source.regions if r.text == expected[key]]
        if len(matches) != 1:
            raise ValueError(f"Expected one source row for {key}: {expected[key]}")
        citations[key] = Evidence(region_id=matches[0].id, quote=matches[0].text)
    draft = Draft(source_hash=source.hash, version=1, invoice=invoice, citations=citations)
    (ROOT / "data/fixtures/demo.json").write_text(
        json.dumps(
            {
                source.hash: {
                    "file": str(path.relative_to(ROOT)),
                    "draft": draft.model_dump(mode="json"),
                }
            },
            indent=2,
        )
        + "\n"
    )
    print("Built approval demo: USD 11,000; quantities fit the standard inventory.")


if __name__ == "__main__":
    main()
