"""Review-page form fields → the core's decision models (review.py). No rule lives here: the
reviewer comes from the session, never from the form, and review.py checks everything else."""

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData

from .. import review
from .accounts import Viewer

CORRECTABLE = tuple(review.Corrections.model_fields)
AMOUNTS = frozenset({"subtotal", "tax_total", "total"})
# "keep": save as the default only if the supplier has none yet; "update": replace it; "dont": never save
DEFAULT_CHOICES: dict[str, bool | None] = {"keep": None, "update": True, "dont": False}


class FormError(ValueError):
    """The form can't become a decision; the message is shown on the page."""


def text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _build[M: BaseModel](model: type[M], **values: Any) -> M:
    try:
        return model.model_validate(values)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise FormError(problems) from e


def decision(form: FormData, viewer: Viewer) -> review.Decision:
    choice = text(form, "default") or "keep"
    if choice not in DEFAULT_CHOICES:
        raise FormError("unknown choice for the supplier's default account")
    return _build(
        review.Decision,
        reviewer=viewer.reviewer,
        account_id=text(form, "account_id") or None,
        set_default=DEFAULT_CHOICES[choice],
        accept_suggested_dates=text(form, "accept_suggested_dates") == "1",
        note=text(form, "note") or None,
    )


def rejection(form: FormData, viewer: Viewer) -> review.Rejection:
    return _build(review.Rejection, reviewer=viewer.reviewer, note=text(form, "note"))


def _same(name: str, new: str | None, current: Any) -> bool:
    if new is None or current is None:
        return new is None and current is None
    if name in AMOUNTS:
        try:
            return Decimal(new) == Decimal(str(current))
        except InvalidOperation:
            return False  # not a number: sent on, and refused with a clear message
    return new == str(current)


def correction(form: FormData, viewer: Viewer, invoice: dict[str, Any]) -> review.Correction:
    """Only the fields whose value differs from the stored invoice; an emptied field is cleared."""
    changes: dict[str, str | None] = {}
    for name in CORRECTABLE:
        if name not in form:
            continue
        new = text(form, name) or None
        if name == "currency" and new:
            new = new.upper()
        if not _same(name, new, invoice.get(name)):
            changes[name] = new
    if not changes:
        raise FormError("nothing was changed")
    return _build(review.Correction, reviewer=viewer.reviewer, changes=changes)
