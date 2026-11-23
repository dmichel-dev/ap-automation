"""One-off recompute of normalized tax IDs: same rule as the pipeline, printed value untouched."""

from app import accounting, db, maintenance

from .conftest import query
from .test_accounting import make_invoice


def labelled_invoice(cfg):
    """An invoice stored before the label rule: the label is still in the normalized copy."""
    inv = make_invoice(cfg)
    query(
        cfg.database_url,
        "UPDATE ap.invoices SET supplier_tax_id = 'EIN 00-0000001', supplier_tax_id_norm = 'EIN000000001' "
        "WHERE id = %s",
        inv,
    )
    with db.connect(cfg.database_url) as conn:
        accounting.link_vendor(conn, "EIN000000001", "USD", "81", "Fenwhistle Janitorial Supply Co.")
    return inv


def renormalize(cfg, apply):
    with db.connect(cfg.database_url) as conn:
        return maintenance.renormalize_tax_ids(conn, apply)


def test_dry_run_changes_nothing(cfg):
    inv = labelled_invoice(cfg)
    out = renormalize(cfg, apply=False)
    assert out["invoices"] == [{"id": inv, "supplier_tax_id_norm": "EIN000000001", "new_norm": "000000001"}]
    assert query(cfg.database_url, "SELECT supplier_tax_id_norm FROM ap.invoices")[0]["supplier_tax_id_norm"] == (
        "EIN000000001"
    )


def test_apply_moves_the_key_and_its_vendor_link_and_keeps_the_printed_value(cfg):
    labelled_invoice(cfg)
    out = renormalize(cfg, apply=True)
    assert out["vendor_links_moved"] == 1
    row = query(cfg.database_url, "SELECT supplier_tax_id, supplier_tax_id_norm, supplier_key FROM ap.invoices")[0]
    assert row == {
        "supplier_tax_id": "EIN 00-0000001",
        "supplier_tax_id_norm": "000000001",
        "supplier_key": "000000001",
    }
    assert [r["supplier_key"] for r in query(cfg.database_url, "SELECT supplier_key FROM ap.vendor_links")] == [
        "000000001"
    ]
    assert renormalize(cfg, apply=True)["invoices"] == []  # idempotent
