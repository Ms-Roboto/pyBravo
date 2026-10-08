"""Recorded well geometry required to rehearse a liquid task.

This check has no physics-engine dependencies so planning can report the same
missing geometry before a native task starts moving.
"""

from __future__ import annotations

import math

from pybravo.deck.geometry import well_geometry_from_metadata


def missing_liquid_well_geometry(metadata) -> list[str]:
    """Do not let unknown depth turn a well-bottom target into a rim target."""
    raw = metadata or {}

    def positive(value):
        try:
            return not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError):
            return False

    missing = [field for field in ("well_depth_mm", "well_diameter_mm") if not positive(raw.get(field))]
    try:
        geometry = well_geometry_from_metadata({field: raw.get(field) for field in ("rows", "cols", "wells")})
        rows, cols = geometry.rows, geometry.cols
    except (TypeError, ValueError, OverflowError):
        rows, cols = 0, 0
    if rows <= 0 or cols <= 0:
        missing.append("well_grid_rows_columns")
    # A single well (or trough on one axis) does not need an unused pitch.
    for field, count in (("spacing_x_mm", cols), ("spacing_y_mm", rows)):
        if count != 1 and not positive(raw.get(field) or raw.get("spacing_mm")):
            missing.append(field)
    return missing
