from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Evidence(Model):
    region_id: str
    quote: str


class Item(Model):
    name: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    discount: Decimal | None = None


class Invoice(Model):
    number: str | None = None
    vendor: str | None = None
    date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    items: list[Item] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    tax_rate: Decimal | None = None
    discount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None


def fingerprint(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fields(invoice: Invoice) -> dict:
    result = {}
    for key, value in invoice.model_dump(mode="json").items():
        if key == "items":
            for i, item in enumerate(value):
                for name, val in item.items():
                    if val is not None:
                        result[f"items.{i}.{name}"] = val
        elif value is not None:
            result[key] = value
    return result


class Draft(Model):
    source_hash: str
    version: int = Field(ge=1, le=2)
    invoice: Invoice
    citations: dict[str, Evidence]

    @property
    def digest(self):
        return fingerprint(self)


class Finding(Model):
    field: str
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class Assessment(Model):
    verdict: Literal["verified", "incorrect", "uncertain"]
    findings: list[Finding]
    checked_fields: list[str]
    covered_regions: list[str]
    complete: bool


class VerificationResult(Assessment):
    source_hash: str
    draft_hash: str
    strategy: str
    metadata: dict = Field(default_factory=dict)


class Check(Model):
    rule: str
    outcome: Literal["pass", "reject", "hold", "unavailable"]
    reason: str
    field: str | None = None
    expected: str | None = None
    observed: str | None = None
