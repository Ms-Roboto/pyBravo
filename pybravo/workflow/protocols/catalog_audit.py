"""Read-only coverage audit for every disposable Bravo head.

This reports catalog evidence and gaps. It does not infer that any head, rack,
tips, liquid class, or plate is loaded on the physical instrument.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from pybravo import liquid_classes
from pybravo.deck.labware import build_labware_catalog, normalize_labware_definitions
from pybravo.tips import get_tip_definitions_for_head
from pybravo.types import HeadType

from .tipbox_choices import compatible_tipbox_choices


def audit_disposable_heads(machine_id: str, labware: list[dict[str, Any]]) -> dict[str, Any]:
    """Enumerate exact rack/tip links and local class availability by head."""
    from .methods import _class_ref, _raw_liquid_classes

    raw_classes = _raw_liquid_classes()
    rows: list[dict[str, Any]] = []
    for head in HeadType:
        if not head.is_disposable:
            continue
        tips = []
        for definition in get_tip_definitions_for_head(head):
            tip = asdict(definition)
            tip["id"] = tip["tip_id"]
            tips.append(tip)
        choices = compatible_tipbox_choices(head.name, labware, tips)
        classes = liquid_classes.list_liquid_classes(machine_id=machine_id, head_type=head.name)
        class_origins = {
            str(item.get("liquid_class_id") or item.get("id")): _class_ref(
                item, raw_classes.get(str(item.get("liquid_class_id") or item.get("id")))
            ).field_origins
            for item in classes
        }
        tip_rows = []
        for tip in tips:
            identity = tip["tip_id"]
            racks = [choice for choice in choices if choice.get("tip_definition_id") == identity]
            matching_classes = [item for item in classes if item.get("tip_id") == identity]
            class_ids = sorted({str(item.get("liquid_class_id") or item.get("id")) for item in matching_classes})
            gaps = []
            if not tip.get("length_mm"):
                gaps.append("tip_length_unknown")
            if not any(choice.get("execution_ready") is True for choice in racks):
                gaps.append("no_execution_ready_tipbox")
            if not matching_classes:
                gaps.append("no_exact_local_liquid_class")
            elif all(any(value in {"normalizer_default_or_unknown", "inferred_or_unknown"}
                         for value in class_origins[class_id].values()) for class_id in class_ids):
                gaps.append("all_local_classes_have_unverified_fields")
            tip_rows.append({
                "tip_id": identity,
                "capacity_ul": tip.get("capacity_ul"),
                "length_mm": tip.get("length_mm"),
                "tip_source": tip.get("source"),
                "rack_ids": sorted({str(choice["labware_id"]) for choice in racks}),
                "execution_ready_rack_ids": sorted({str(choice["labware_id"]) for choice in racks
                                                   if choice.get("execution_ready") is True}),
                "local_liquid_class_ids": class_ids,
                "local_class_unverified_fields": {
                    class_id: sorted(key for key, origin in class_origins[class_id].items()
                                     if origin in {"normalizer_default_or_unknown", "inferred_or_unknown"})
                    for class_id in class_ids
                },
                "gaps": gaps,
            })
        unknown_class_tips = sorted({str(item["tip_id"]) for item in classes
                                     if item.get("tip_id") and item["tip_id"] not in {tip["tip_id"] for tip in tips}})
        rows.append({
            "head_type": head.name,
            "tips": tip_rows,
            "local_liquid_class_count": len(classes),
            "classes_with_inferred_tip_id": sorted(class_id for class_id, origins in class_origins.items()
                                                   if origins.get("tip_id") == "inferred_or_unknown"),
            "liquid_class_tip_ids_without_head_link": unknown_class_tips,
        })
    return {
        "standard": "pybravo.disposable-head-catalog-audit",
        "schema_version": "1.0.0",
        "machine_id": machine_id,
        "heads": rows,
        "scope": "Local catalog/configuration evidence only; not an inventory or qualification claim.",
    }


def audit_local_catalogs(machine_id: str) -> dict[str, Any]:
    """Load the current catalog snapshot, without opening a hardware connection."""
    definitions, _ = normalize_labware_definitions(build_labware_catalog().list_definitions())
    return audit_disposable_heads(machine_id, [definition.to_summary() for definition in definitions])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Audit Bravo disposable heads and local method prerequisites")
    parser.add_argument("--machine-id", required=True, help="Machine ID whose local liquid classes should be audited")
    args = parser.parse_args()
    print(json.dumps(audit_local_catalogs(args.machine_id), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
