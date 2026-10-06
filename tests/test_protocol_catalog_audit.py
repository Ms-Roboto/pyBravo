from __future__ import annotations

from pybravo.types import HeadType
from pybravo.workflow.protocols import catalog_audit, methods


def test_audit_covers_every_disposable_head_and_exposes_unverified_origins(monkeypatch):
    class_row = {
        "liquid_class_id": "local-water",
        "name": "Local water",
        "machine_id": "machine-1",
        "head_type": "HT_384_D_70",
        "tip_id": "st_10ul",
        "aspirate": {},
        "dispense": {},
        "equation": {"control_points": []},
    }
    monkeypatch.setattr(catalog_audit.liquid_classes, "list_liquid_classes",
                        lambda *, machine_id, head_type: [class_row] if head_type == "HT_384_D_70" else [])
    monkeypatch.setattr(methods, "_raw_liquid_classes", lambda: {
        "local-water": {**class_row, "tip_id": ""},
    })

    audit = catalog_audit.audit_disposable_heads("machine-1", [])
    assert {head["head_type"] for head in audit["heads"]} == {
        head.name for head in HeadType if head.is_disposable
    }
    head_384 = next(head for head in audit["heads"] if head["head_type"] == "HT_384_D_70")
    assert head_384["classes_with_inferred_tip_id"] == ["local-water"]
    st10 = next(tip for tip in head_384["tips"] if tip["tip_id"] == "st_10ul")
    assert st10["local_liquid_class_ids"] == ["local-water"]
    assert "tip_id" in st10["local_class_unverified_fields"]["local-water"]
    assert "all_local_classes_have_unverified_fields" in st10["gaps"]
    assert "no_execution_ready_tipbox" in st10["gaps"]
