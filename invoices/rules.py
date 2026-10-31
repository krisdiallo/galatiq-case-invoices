from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .models import Check

CENT = Decimal(".01")


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def validate(invoice, inventory, default_currency=None):
    checks = []

    def add(rule, outcome, reason, field=None, expected=None, observed=None):
        checks.append(
            Check(
                rule=rule,
                outcome=outcome,
                reason=reason,
                field=field,
                expected=str(expected) if expected is not None else None,
                observed=str(observed) if observed is not None else None,
            )
        )

    def compare(rule, expected, observed, field):
        if observed is None:
            add(rule, "unavailable", "No independently stated comparison value", field, expected)
        elif money(expected) != money(observed):
            add(
                rule,
                "reject",
                "Stated amount does not reconcile with source components",
                field,
                money(expected),
                observed,
            )
        else:
            add(rule, "pass", "Amounts reconcile", field, money(expected), observed)

    for key in ("number", "vendor", "due_date"):
        if not getattr(invoice, key):
            add("required", "hold", "Required source information is missing", key)
    if invoice.due_date:
        try:
            date.fromisoformat(invoice.due_date)
        except ValueError:
            add(
                "due_date",
                "hold",
                "Due date is ambiguous or invalid",
                "due_date",
                observed=invoice.due_date,
            )
    if invoice.date:
        try:
            date.fromisoformat(invoice.date)
        except ValueError:
            add(
                "invoice_date",
                "hold",
                "Invoice date is ambiguous or invalid",
                "date",
                observed=invoice.date,
            )
    currency = invoice.currency or default_currency
    if not currency:
        add(
            "currency",
            "hold",
            "Currency is absent; configure a documented default or clarify",
            "currency",
        )
    elif currency != "USD":
        add("currency", "reject", "Only USD payments are supported", "currency", "USD", currency)
    else:
        add(
            "currency",
            "pass",
            "USD policy satisfied"
            if invoice.currency
            else "Explicit configured USD default applied",
            "currency",
            "USD",
            currency,
        )
    if not invoice.items:
        add("items", "hold", "No item lines were established", "items")
    quantities = defaultdict(Decimal)
    amounts = []
    for i, item in enumerate(invoice.items):
        prefix = f"items.{i}"
        if not item.name or item.quantity is None or item.unit_price is None:
            add("item_required", "hold", "Item name, quantity and unit price are required", prefix)
            continue
        if item.quantity <= 0:
            add(
                "quantity_positive",
                "reject",
                "Quantity must be positive",
                prefix + ".quantity",
                "> 0",
                item.quantity,
            )
        if item.unit_price < 0:
            add(
                "price_nonnegative",
                "reject",
                "Unit price cannot be negative",
                prefix + ".unit_price",
                ">= 0",
                item.unit_price,
            )
        if item.discount is not None and item.discount < 0:
            add(
                "discount_nonnegative",
                "reject",
                "Discount cannot be negative",
                prefix + ".discount",
            )
        amount = money(item.quantity * item.unit_price - (item.discount or 0))
        if amount < 0:
            add("line_nonnegative", "reject", "Computed line amount is negative", prefix)
        compare("line_arithmetic", amount, item.amount, prefix + ".amount")
        amounts.append(amount)
        quantities[item.name] += item.quantity
    for name, quantity in quantities.items():
        if name not in inventory:
            add("inventory_known", "reject", f"Unknown item: {name}", "items", observed=name)
            continue
        stock = inventory[name]
        if type(stock) is not int or stock < 0:
            add(
                "inventory_integrity",
                "reject",
                f"Invalid inventory stock for {name}",
                "items",
                "nonnegative integer",
                stock,
            )
        elif quantity > stock:
            add(
                "inventory_stock",
                "reject",
                f"{name}: requested {quantity}, available {stock}",
                "items",
                stock,
                quantity,
            )
        else:
            add(
                "inventory_stock",
                "pass",
                f"{name}: aggregate quantity fits stock",
                "items",
                stock,
                quantity,
            )
    for field in ("subtotal", "tax", "tax_rate", "discount", "shipping", "total"):
        value = getattr(invoice, field)
        if value is not None and value < 0:
            add("amount_nonnegative", "reject", f"{field} cannot be negative", field, ">= 0", value)
    if invoice.total is None:
        add("total_required", "hold", "Invoice total is missing", "total")
    elif invoice.total == 0:
        add("total_positive", "reject", "Invoice total must be positive", "total", "> 0", 0)
    if amounts and len(amounts) == len(invoice.items):
        subtotal = sum(amounts, Decimal(0))
        compare("subtotal_arithmetic", subtotal, invoice.subtotal, "subtotal")
        # Prototype tax policy: explicit rate applies to subtotal less invoice-level discount.
        if invoice.tax_rate is not None:
            if invoice.discount or invoice.shipping:
                add(
                    "tax_base",
                    "hold",
                    "Taxable base with discounts or shipping requires clarification",
                    "tax_rate",
                )
            else:
                compare("tax_arithmetic", subtotal * invoice.tax_rate, invoice.tax, "tax")
        else:
            add(
                "tax_arithmetic",
                "unavailable",
                "No explicit tax rate; retain stated tax",
                "tax_rate",
            )
        total = subtotal - (invoice.discount or 0) + (invoice.tax or 0) + (invoice.shipping or 0)
        compare("total_arithmetic", total, invoice.total, "total")
    return checks
