"""Read-only, geometry-derived liquid Z-height planning proposals.

These estimates do not qualify a liquid method or certify a collision-free,
noncontact path.  In particular, a catalog well's capacity and opening width
do not describe its taper, meniscus, tip outer diameter, or manufacturing
tolerances.  Z-entry speeds come only from an explicitly selected liquid class.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


# A review heuristic, not an instrument clearance specification.  A proposal
# needs this much *modeled* room both above the residual source volume and
# around a destination's estimated final liquid surface.
_PLANNING_GAP_MM = 1.0
_UNIFORM_SECTION_TOLERANCE = 0.15


def _positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _nonnegative(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _geometry(labware: Mapping[str, Any]) -> dict[str, float] | None:
    depth = labware.get("well_depth_mm")
    width = labware.get("well_diameter_mm")
    capacity = labware.get("well_volume_ul")
    if not all(_positive(value) for value in (depth, width, capacity)):
        return None
    # The flat catalog uses "diameter" for both round openings and the width
    # of square wells. A missing or unrecognized shape cannot support an area
    # estimate; guessing square would overstate headspace for a round well.
    shape = labware.get("well_geometry")
    if isinstance(shape, str):
        shape = shape.strip().casefold()
    if shape in (1, "1", "round", "circular"):
        area = math.pi * (width / 2) ** 2
    elif shape in (2, "2", "square"):
        area = width**2
    else:
        return None
    return {
        "well_depth_mm": float(depth),
        "well_width_mm": float(width),
        "well_capacity_ul": float(capacity),
        "modeled_max_section_mm2": area,
        "prism_capacity_ul": area * depth,
    }


def _motion(liquid_class: Mapping[str, Any] | None, phase: str) -> dict[str, Any]:
    """Expose recorded class values, never derive Z speed from well geometry."""
    values = liquid_class.get(phase) if isinstance(liquid_class, Mapping) else None
    if not isinstance(values, Mapping):
        return {
            "status": "unresolved",
            "reason": "Select a liquid class with explicit Z-entry motion settings.",
        }
    keys = ("z_in_velocity_mm_s", "z_in_acceleration_mm_s2",
            "z_out_velocity_mm_s", "z_out_acceleration_mm_s2")
    if not all(_positive(values.get(key)) for key in keys):
        return {
            "status": "unresolved",
            "reason": "The selected class lacks explicit positive Z motion settings.",
        }
    origins = liquid_class.get("source_field_origins") or liquid_class.get("field_origins") or {}
    if not isinstance(origins, Mapping):
        origins = {}
    return {
        "status": "class_setting_for_review",
        "source": "selected_liquid_class",
        "liquid_class_id": liquid_class.get("liquid_class_id") or liquid_class.get("id"),
        **{key: float(values[key]) for key in keys},
        "field_origins": {key: origins.get(f"{phase}.{key}", "class_value_origin_unverified")
                          for key in keys},
        "geometry_derived": False,
        "method_applicability_qualified": False,
    }


def _unresolved(phase: str, reasons: list[str], geometry: dict[str, float] | None,
                motion: dict[str, Any], **evidence: Any) -> dict[str, Any]:
    return {
        "phase": phase,
        "status": "unresolved",
        "distance_from_bottom_mm": None,
        "reasons": reasons,
        "evidence_level": "geometry_heuristic",
        "requires_method_review": True,
        "geometry": geometry,
        "motion": motion,
        **evidence,
    }


def propose_aspiration_height(
    labware: Mapping[str, Any],
    *,
    initial_volume_ul: float,
    total_withdrawal_ul: float,
    dead_volume_ul: float,
    liquid_class: Mapping[str, Any] | None = None,
    planning_gap_mm: float = _PLANNING_GAP_MM,
) -> dict[str, Any]:
    """Estimate a source height using the smallest planned residual volume.

    The opening width bounds the well cross-section only if catalog geometry
    describes the widest part of the well.  The result is a candidate for
    review, not a measured liquid level or proof that a tip clears the bottom.
    """
    motion = _motion(liquid_class, "aspirate")
    geometry = _geometry(labware)
    if geometry is None:
        return _unresolved("aspirate", ["Catalog capacity, depth, opening width, and a known well shape are required."],
                           None, motion)
    if not all(_nonnegative(value) for value in (initial_volume_ul, total_withdrawal_ul, dead_volume_ul)):
        return _unresolved("aspirate", ["Starting, withdrawn, and dead volumes must be finite and nonnegative."],
                           geometry, motion)
    if not _positive(planning_gap_mm):
        return _unresolved("aspirate", ["The planning gap must be positive."], geometry, motion)
    capacity = geometry["well_capacity_ul"]
    residual = initial_volume_ul - total_withdrawal_ul
    if initial_volume_ul > capacity or residual < dead_volume_ul or residual < 0:
        return _unresolved("aspirate", ["The planned volume exceeds well capacity or falls below dead volume."],
                           geometry, motion, residual_volume_ul=residual)
    # A capacity larger than the maximum opening area times depth contradicts
    # the dimensions, so even the lower-bound liquid level would be unsound.
    if capacity > geometry["prism_capacity_ul"] * (1 + _UNIFORM_SECTION_TOLERANCE):
        return _unresolved("aspirate", ["Catalog capacity exceeds the geometric upper bound."],
                           geometry, motion, residual_volume_ul=residual)
    area = geometry["modeled_max_section_mm2"]
    residual_level = residual / area
    dead_level = dead_volume_ul / area
    modeled_room = residual_level - dead_level
    evidence = {
        "residual_volume_ul": residual,
        "modeled_minimum_residual_liquid_height_mm": round(residual_level, 4),
        "modeled_dead_volume_height_mm": round(dead_level, 4),
        "modeled_usable_liquid_column_mm": round(modeled_room, 4),
        "planning_gap_mm": float(planning_gap_mm),
    }
    if modeled_room < 2 * planning_gap_mm:
        return _unresolved("aspirate", ["The modeled usable liquid column has too little room for this planning heuristic."],
                           geometry, motion, **evidence)
    height = (residual_level + dead_level) / 2
    if height >= geometry["well_depth_mm"]:
        return _unresolved("aspirate", ["The modeled height reaches or exceeds the well rim."],
                           geometry, motion, **evidence)
    return {
        "phase": "aspirate",
        "status": "proposed_for_review",
        "distance_from_bottom_mm": round(height, 3),
        "evidence_level": "geometry_heuristic",
        "requires_method_review": True,
        "geometry": geometry,
        "motion": motion,
        **evidence,
        "limitations": [
            "The catalog opening width is assumed to bound the well cross-section.",
            "Dead volume is not a measured liquid level; tip dimensions, bottom clearance, and meniscus are not modeled.",
            "Confirm this height with the actual plate, tip, liquid, and method before hardware use.",
        ],
    }


def propose_noncontact_dispense_height(
    labware: Mapping[str, Any],
    *,
    initial_volume_ul: float,
    dispense_volume_ul: float,
    liquid_class: Mapping[str, Any] | None = None,
    planning_gap_mm: float = _PLANNING_GAP_MM,
) -> dict[str, Any]:
    """Propose a noncontact height only when the simple well model has room.

    A nearly full or tapered well needs a measured surface/clearance model.
    A class's Z-entry speed is reported independently, even if height remains
    unresolved.
    """
    motion = _motion(liquid_class, "dispense")
    geometry = _geometry(labware)
    if geometry is None:
        return _unresolved("dispense", ["Catalog capacity, depth, opening width, and a known well shape are required."],
                           None, motion)
    if not all(_nonnegative(value) for value in (initial_volume_ul, dispense_volume_ul)):
        return _unresolved("dispense", ["Starting and dispense volumes must be finite and nonnegative."],
                           geometry, motion)
    if not _positive(planning_gap_mm):
        return _unresolved("dispense", ["The planning gap must be positive."], geometry, motion)
    final_volume = initial_volume_ul + dispense_volume_ul
    if final_volume > geometry["well_capacity_ul"]:
        return _unresolved("dispense", ["The final volume exceeds catalog well capacity."],
                           geometry, motion, final_volume_ul=final_volume)
    # Uniform-section interpolation is useful only when the listed opening,
    # depth, and capacity broadly agree.  LDV wells are a counterexample.
    ratio = geometry["well_capacity_ul"] / geometry["prism_capacity_ul"]
    nominal_surface = geometry["well_depth_mm"] * final_volume / geometry["well_capacity_ul"]
    headspace = geometry["well_depth_mm"] - nominal_surface
    evidence = {
        "final_volume_ul": final_volume,
        "capacity_fraction": round(final_volume / geometry["well_capacity_ul"], 4),
        "nominal_liquid_surface_mm": round(nominal_surface, 4),
        "nominal_headspace_mm": round(headspace, 4),
        "opening_depth_capacity_ratio": round(ratio, 4),
        "planning_gap_mm": float(planning_gap_mm),
    }
    reasons = []
    if abs(ratio - 1) > _UNIFORM_SECTION_TOLERANCE:
        reasons.append("Opening width, depth, and capacity do not support a uniform-section liquid-height model.")
    if headspace < 2 * planning_gap_mm:
        reasons.append("The final volume leaves too little modeled headspace for a noncontact in-well dispense.")
    if reasons:
        return _unresolved("dispense", reasons, geometry, motion, **evidence)
    return {
        "phase": "dispense",
        "status": "proposed_for_review",
        "distance_from_bottom_mm": round(nominal_surface + planning_gap_mm, 3),
        "evidence_level": "geometry_heuristic",
        "requires_method_review": True,
        "geometry": geometry,
        "motion": motion,
        **evidence,
        "limitations": [
            "The liquid surface assumes a uniform-section well; catalog dimensions are approximate.",
            "Meniscus, tip dimensions, positioning tolerance, and splash are not modeled.",
            "Confirm this height with the actual plate, tip, liquid, and method before hardware use.",
        ],
    }


def propose_transfer_heights(
    source_labware: Mapping[str, Any],
    destination_labware: Mapping[str, Any],
    *,
    source_initial_volume_ul: float,
    total_source_withdrawal_ul: float,
    source_dead_volume_ul: float,
    destination_initial_volume_ul: float,
    dispense_volume_ul: float,
    liquid_class: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return separate phase proposals for a transfer or distribution plan."""
    aspirate = propose_aspiration_height(
        source_labware,
        initial_volume_ul=source_initial_volume_ul,
        total_withdrawal_ul=total_source_withdrawal_ul,
        dead_volume_ul=source_dead_volume_ul,
        liquid_class=liquid_class,
    )
    dispense = propose_noncontact_dispense_height(
        destination_labware,
        initial_volume_ul=destination_initial_volume_ul,
        dispense_volume_ul=dispense_volume_ul,
        liquid_class=liquid_class,
    )
    return {
        "aspirate": aspirate,
        "dispense": dispense,
        "complete_height_proposal": aspirate["status"] == dispense["status"] == "proposed_for_review",
        "physical_safety_established": False,
    }
