"""Dimensioned lid exterior envelopes shared by rendering and collision checks.

An exterior envelope is deliberately solid: it reserves all space occupied by
the lid, including its unknown hollow interior. It is not a fitted shell and
must not be used to establish grasp contact or a delid/relid insertion path.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any


def lid_envelope_geometry(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a validated, centered-XY lid envelope in plate-base millimetres.

    Missing, incomplete, or internally inconsistent records remain unsupported.
    The provenance is retained so reports can distinguish dimensioned exterior
    clearance from unmeasured shell/interior detail.
    """
    meta = metadata or {}
    if not isinstance(meta, dict):
        return None
    geometry = meta.get("lid_geometry")
    if not isinstance(geometry, dict):
        return None
    if geometry.get("model") != "manufacturer_exterior_envelope":
        return None
    if not str(geometry.get("source") or "").strip():
        return None
    numbers: dict[str, float] = {}
    for key in ("length_mm", "width_mm", "height_mm", "seated_bottom_mm"):
        value = geometry.get(key)
        if isinstance(value, bool):
            return None
        try:
            numbers[key] = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(numbers[key]) or numbers[key] < 0:
            return None
        if key != "seated_bottom_mm" and numbers[key] == 0:
            return None
    top = numbers["seated_bottom_mm"] + numbers["height_mm"]
    if not math.isfinite(top):
        return None
    if meta.get("lidded_height_mm") is not None:
        if isinstance(meta["lidded_height_mm"], bool):
            return None
        try:
            catalog_top = float(meta["lidded_height_mm"])
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(catalog_top) or not math.isclose(catalog_top, top, abs_tol=1e-6):
            return None
    # If a plate-base height is known, the seated lid must straddle its rim.
    # A standalone generated lid has no plate height and uses its own local Z.
    plate_height = meta.get("base_height_mm")
    if plate_height is None and str(meta.get("kind") or "") != "lid":
        plate_height = meta.get("height_mm")
    if plate_height is not None:
        if isinstance(plate_height, bool):
            return None
        try:
            plate_height = float(plate_height)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(plate_height) or not numbers["seated_bottom_mm"] < plate_height <= top:
            return None
    result = deepcopy(geometry)
    result.update(numbers)
    result["lower_local_mm"] = [
        -numbers["length_mm"] / 2, -numbers["width_mm"] / 2, numbers["seated_bottom_mm"],
    ]
    result["upper_local_mm"] = [numbers["length_mm"] / 2, numbers["width_mm"] / 2, top]
    return result
