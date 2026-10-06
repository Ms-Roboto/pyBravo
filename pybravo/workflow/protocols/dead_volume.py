"""Catalog dead-volume evidence used by protocol planning and validation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def catalog_dead_volume(definition: Mapping[str, Any] | None) -> tuple[float, str] | None:
    """Return a usable catalog estimate and its review status, if recorded."""
    if not isinstance(definition, Mapping) or definition.get("provisional"):
        return None
    status = definition.get("dead_volume_status")
    value = definition.get("dead_volume_ul")
    if (status not in {"reviewed", "placeholder"}
            or not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0):
        return None
    return float(value), status


def seed_reviewed_source_dead_volumes(plan: Any, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fill missing aspirated-source residual estimates from reviewed plate types.

    This is catalog-backed planning data, not a scientist decision or proof of
    physical liquid supply. Unreviewed placeholders and existing plan values
    are left untouched. The returned rows identify every inserted value.
    """
    definitions: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    for row in context.get("labware") or []:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        identity = row["id"]
        if identity in definitions:
            duplicates.add(identity)
        else:
            definitions[identity] = row

    aspirated_ids: set[str] = set()

    def collect(steps: Any) -> None:
        for step in steps or []:
            if step.kind in {"transfer", "distribute"} and step.source:
                aspirated_ids.add(step.source)
            elif step.kind == "mix" and step.material:
                aspirated_ids.add(step.material)
            collect(step.steps)

    collect(plan.steps)
    seeded: list[dict[str, Any]] = []
    for index, material in enumerate(plan.materials):
        if (material.role != "liquid" or material.id not in aspirated_ids
                or material.dead_volume_ul is not None or not material.labware_id
                or material.labware_id in duplicates):
            continue
        catalog_value = catalog_dead_volume(definitions.get(material.labware_id))
        if catalog_value is None or catalog_value[1] != "reviewed":
            continue
        material.dead_volume_ul = catalog_value[0]
        seeded.append({"path": f"/materials/{index}/dead_volume_ul",
                       "value": catalog_value[0], "labware_id": material.labware_id,
                       "source": "reviewed_labware_catalog"})
    return seeded


def scientist_confirmed_dead_volume(decisions: Any, index: int, material_id: str,
                                    labware_id: str, value: float) -> bool:
    """Require an explicit confirmation bound to the current material and plate."""
    path = f"/materials/{index}/dead_volume_review"
    for decision in reversed(decisions):
        row = decision.model_dump() if hasattr(decision, "model_dump") else decision
        if not isinstance(row, Mapping) or row.get("path") != path or row.get("actor") != "scientist":
            continue
        confirmed = row.get("value")
        return (isinstance(confirmed, Mapping)
                and confirmed.get("material_id") == material_id
                and confirmed.get("labware_id") == labware_id
                and isinstance(confirmed.get("dead_volume_ul"), (int, float))
                and not isinstance(confirmed.get("dead_volume_ul"), bool)
                and confirmed["dead_volume_ul"] == value)
    return False
