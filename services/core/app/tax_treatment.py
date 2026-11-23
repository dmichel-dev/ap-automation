"""How each tax on a supplier invoice is booked: a company policy, set in a JSON file.

  non_recoverable  the tax is part of the cost: added to the expense lines it belongs to
                   (default; e.g. US sales tax, or foreign VAT a US company can't reclaim)
  recoverable      the tax will be reclaimed: a separate line to a receivable account
                   (e.g. "VAT recoverable" for a VAT-registered entity)
  withholding      tax withheld at source (e.g. Spanish IRPF): always a separate line to a
                   liability account, whatever the file says; it is owed to the tax office

Rules are matched by (tax type, supplier country); the most specific rule wins, then the first one.
The default file is app/tax_treatment.json; TAX_TREATMENT_FILE points to another one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Treatment = Literal["non_recoverable", "recoverable", "withholding"]
DEFAULT_FILE = Path(__file__).with_name("tax_treatment.json")


class TaxRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    treatment: Literal["non_recoverable", "recoverable"]
    tax_type: Literal["vat", "gst", "sales_tax", "other", "none"] | None = None  # None = any
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")  # supplier country; None = any
    note: str = ""

    def matches(self, tax_type: str, country: str | None) -> bool:
        return self.tax_type in (None, tax_type) and self.country in (None, country)

    @property
    def specificity(self) -> int:
        return (self.tax_type is not None) * 2 + (self.country is not None)


class TaxTreatment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default: Literal["non_recoverable", "recoverable"] = "non_recoverable"
    rules: tuple[TaxRule, ...] = ()
    note: str = ""

    def treatment(self, tax_type: str, country: str | None) -> Treatment:
        if tax_type == "withholding":
            return "withholding"
        found = [r for r in self.rules if r.matches(tax_type, country)]
        if not found:
            return self.default
        best = max(r.specificity for r in found)
        return next(r for r in found if r.specificity == best).treatment

    @classmethod
    def load(cls, path: Path | None = None) -> TaxTreatment:
        return cls.model_validate(json.loads((path or DEFAULT_FILE).read_text()))
