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
