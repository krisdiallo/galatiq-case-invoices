"""Build curated demonstration fixtures; these are not independently human-labeled eval data."""

import csv
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from invoices.models import Draft, Evidence, Invoice, fields
from invoices.source import read_source

ROOT = Path(__file__).resolve().parents[1]


def item(name, quantity, price, amount=None):
    return {"name": name, "quantity": quantity, "unit_price": price, "amount": amount}


TEXT = {
    "1001": {
        "number": "INV-1001",
        "vendor": "Widgets Inc.",
        "date": "2026-01-15",
        "due_date": "2026-02-01",
        "currency": "USD",
        "items": [item("WidgetA", 10, 250), item("WidgetB", 5, 500)],
        "subtotal": 5000,
        "tax": 0,
        "tax_rate": 0,
        "total": 5000,
    },
    "1002": {
        "number": "INV-1002",
        "vendor": "Gadgets Co.",
        "date": "2026-01-30",
        "due_date": "2026-01-30",
        "currency": "USD",
        "items": [item("GadgetX", 20, 750)],
        "total": 15000,
    },
    "1003": {
        "number": "INV-1003",
        "vendor": "Fraudster LLC",
        "date": "2026-01-20",
        "due_date": "yesterday",
        "currency": "USD",
        "items": [item("FakeItem", 100, 1000)],
        "total": 100000,
    },
    "1008": {
        "number": "INV-1008",
        "vendor": "NoProd Industries",
        "date": "2026-01-10",
        "due_date": "2026-01-20",
        "currency": "USD",
        "items": [item("SuperGizmo", 12, 400), item("MegaSprocket", 6, 850)],
        "total": 9900,
    },
    "1010": {
        "number": "INV-1010",
        "vendor": "Consolidated Materials Group",
        "date": "2026-01-27",
        "due_date": "2026-02-26",
        "currency": "USD",
        "items": [
            item("WidgetA", 8, 250, 2000),
            item("WidgetB", 4, 500, 2000),
            item("GadgetX", 2, 750, 1500),
            item("WidgetA", 4, 300, 1200),
        ],
        "subtotal": 6700,
        "tax": 335,
        "shipping": 150,
        "total": 7185,
    },
    "1011": {
        "number": "INV-1011",
        "vendor": "Summit Manufacturing Co.",
        "date": "2026-01-20",
        "due_date": "2026-02-20",
        "currency": "USD",
        "items": [item("WidgetA", 6, 250, 1500), item("WidgetB", 3, 500, 1500)],
        "subtotal": 3000,
        "tax": 0,
        "tax_rate": 0,
        "total": 3000,
    },
    "1012": {
        "number": "INV-1012",
        "vendor": "QuickShip Distributers",
        "date": "2026-01-26",
        "due_date": "2026-02-25",
        "currency": "USD",
        "items": [
            item("WidgetA", 12, 250, 3000),
            item("WidgetB", 7, 500, 3500),
            item("GadgetX", 4, 750, 3000),
        ],
        "subtotal": 9500,
        "tax": 475,
        "tax_rate": ".05",
        "total": 9975,
    },
}


def from_json(data):
    return {
        "number": data["invoice_number"],
        "vendor": data["vendor"]["name"],
        "date": data["date"],
        "due_date": data["due_date"],
        "currency": data.get("currency"),
        "items": [
            item(x["item"], x["quantity"], x["unit_price"], x.get("amount"))
            for x in data["line_items"]
        ],
        "subtotal": data.get("subtotal"),
        "tax": data.get("tax_amount"),
        "tax_rate": data.get("tax_rate"),
        "total": data["total"],
    }


def from_csv(path):
    rows = list(csv.reader(path.open()))
    if rows[0] == ["field", "value"]:
        data, items = {}, []
        for key, value in rows[1:]:
            if key == "item":
                items.append({"name": value})
            elif key in {"quantity", "unit_price"}:
                items[-1][key] = value
            else:
                data[key] = value
        return {
            "number": data["invoice_number"],
            "vendor": data["vendor"],
            "date": data["date"],
            "due_date": data["due_date"],
            "items": items,
            "subtotal": data["subtotal"],
            "tax": data["tax"],
            "total": data["total"],
        }
    first = rows[1]
    totals = rows[-3:]
    from datetime import datetime

    def date(v):
        return datetime.strptime(v, "%m/%d/%Y").date().isoformat() if "/" in v else v

    return {
        "number": first[0],
        "vendor": first[1],
        "date": date(first[2]),
        "due_date": date(first[3]),
        "items": [item(r[4], r[5], r[6], r[7]) for r in rows[1:] if r[0]],
        "subtotal": totals[0][-1],
        "tax": totals[1][-1],
        "tax_rate": ".06" if "6%" in totals[1][-2] else "0",
        "total": totals[2][-1],
    }


def citations(source, invoice):
    result = {}
    # Fixture references are source locations, not model-generated reasoning.
    normalized = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    for field, value in fields(invoice).items():
        candidates = []
        if field.startswith("items."):
            index = int(field.split(".")[1])
            name = invoice.items[index].name
            matches = [r for r in source.regions if normalized(name) in normalized(r.text)]
            occurrence = sum(1 for x in invoice.items[:index] if x.name == name)
            if matches:
                region = matches[min(occurrence, len(matches) - 1)]
                # In pretty JSON quantity/price/amount follow the item name.
                if source.format == "json":
                    start = source.regions.index(region)
                    key = {"name": "item"}.get(field.split(".")[-1], field.split(".")[-1])
                    region = next(
                        (r for r in source.regions[start : start + 5] if f'"{key}"' in r.text),
                        region,
                    )
                elif source.format == "xml":
                    start = source.regions.index(region)
                    key = field.split(".")[-1]
                    region = next(
                        (r for r in source.regions[start : start + 4] if f"<{key}>" in r.text),
                        region,
                    )
                elif source.format == "csv" and source.regions[0].text == "field,value":
                    start = source.regions.index(region)
                    key = field.split(".")[-1]
                    region = next(
                        (
                            r
                            for r in source.regions[start : start + 3]
                            if r.text.startswith(key + ",")
                        ),
                        region,
                    )
                candidates = [region]
        else:
            patterns = {
                "number": r"invoice_number|invoice number|inv #|invoice[: #]|inv no",
                "vendor": r'"name"|<vendor>|^vendor,|vendor:|vndr:|from:',
                "date": r'^\s*("date"|<date>|date[:,]|dt:)',
                "due_date": r"due",
                "currency": r"currency|\$",
                "subtotal": r"subtotal",
                "tax": r"tax_amount|tax \(|sales tax|^tax,",
                "tax_rate": r"tax_rate|tax \(",
                "shipping": r"shipping",
                "total": r'"total"|<total>|^total,|total amount|^\s*total:|grand total|^amt:|,,,,,,total:',
            }
            candidates = [
                r
                for r in source.regions
                if re.search(patterns.get(field, field), r.text, re.IGNORECASE)
            ]
            if not candidates:
                candidates = [r for r in source.regions if str(value) in r.text]
        if (
            source.format == "csv"
            and source.regions[0].text != "field,value"
            and field in {"number", "vendor", "date", "due_date"}
        ):
            candidates = [source.regions[1]]
        if not candidates:
            raise ValueError((source.path, field, value))
        result[field] = Evidence(region_id=candidates[0].id, quote=candidates[0].text)
    return result


def main():
    fixtures = {}
    for path in sorted((ROOT / "data/invoices").iterdir()):
        source = read_source(path)
        number = path.stem.split("_")[1]
        if path.suffix == ".json":
            data = from_json(json.loads(path.read_text()))
        elif path.suffix == ".csv":
            data = from_csv(path)
        elif path.suffix == ".xml":
            tree = ElementTree.fromstring(path.read_text())
            data = {
                "number": tree.findtext("header/invoice_number"),
                "vendor": tree.findtext("header/vendor"),
                "date": tree.findtext("header/date"),
                "due_date": tree.findtext("header/due_date"),
                "currency": tree.findtext("header/currency"),
                "items": [
                    item(x.findtext("name"), x.findtext("quantity"), x.findtext("unit_price"))
                    for x in tree.findall("line_items/item")
                ],
                "subtotal": tree.findtext("totals/subtotal"),
                "tax": tree.findtext("totals/tax_amount"),
                "tax_rate": tree.findtext("totals/tax_rate"),
                "total": tree.findtext("totals/total"),
            }
        elif number == "1013":
            data = from_json(json.loads((ROOT / "data/invoices/invoice_1013.json").read_text()))
            # Both supplied variants contain the same intentional $50 total discrepancy.
            data.update(subtotal=21040, tax=1472.8, total=22562.8)
        else:
            data = dict(TEXT[number])
        if path.suffix == ".pdf" and number == "1011":
            data.update(subtotal=None, tax=None, tax_rate=None)
        inv = Invoice.model_validate(data)
        draft = Draft(
            source_hash=source.hash, version=1, invoice=inv, citations=citations(source, inv)
        )
        fixtures[source.hash] = {"file": path.name, "draft": draft.model_dump(mode="json")}
    (ROOT / "data/fixtures/invoices.json").write_text(json.dumps(fixtures, indent=2) + "\n")
    print(f"Wrote {len(fixtures)} hash-bound offline fixtures")


if __name__ == "__main__":
    main()
