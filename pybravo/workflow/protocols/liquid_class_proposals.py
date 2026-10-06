"""Read-only, cross-profile liquid-class references for planning in simulation.

An imported class from another instrument is useful evidence for a scientist to
review, but it is never an active class or a method that a protocol can pin.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from pybravo import liquid_classes

from .methods import (
    _MOTION_FIELDS,
    _authored_methods,
    _class_ref,
    _digest,
    _liquid_store_path,
    _raw_liquid_classes,
)


class LiquidClassProposalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tip_id: str = Field(min_length=1)
    volume_ul: float = Field(gt=0, allow_inf_nan=False)
    reagent_family: str | None = None
    source_labware_id: str | None = None
    destination_labware_id: str | None = None


def _explicit_motion(raw: Mapping[str, Any], active: Mapping[str, Any]) -> bool:
    for phase in ("aspirate", "dispense"):
        raw_phase, active_phase = raw.get(phase), active.get(phase)
        if not isinstance(raw_phase, Mapping) or not isinstance(active_phase, Mapping):
            return False
        for field in _MOTION_FIELDS:
            value, normalized = raw_phase.get(field), active_phase.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
            if (field == "post_delay_ms" and value < 0) or (field != "post_delay_ms" and value <= 0):
                return False
            if not math.isclose(float(value), float(normalized), rel_tol=0, abs_tol=1e-9):
                return False
    return True


def _explicit_calibration(raw: Mapping[str, Any], active: Mapping[str, Any], volume_ul: float) -> bool:
    raw_equation = raw.get("equation")
    active_equation = active.get("equation")
    if not isinstance(raw_equation, Mapping) or not isinstance(active_equation, Mapping):
        return False
    points = raw_equation.get("control_points")
    normalized = active_equation.get("control_points")
    if not isinstance(points, list) or not isinstance(normalized, list) or len(points) < 2:
        return False
    try:
        source_points = sorted((float(p["desired_ul"]), float(p["commanded_ul"])) for p in points)
        active_points = sorted((float(p["desired_ul"]), float(p["commanded_ul"])) for p in normalized)
    except (KeyError, TypeError, ValueError):
        return False
    if source_points != active_points or not all(math.isfinite(v) and math.isfinite(c) and v >= 0 and c >= 0
                                                 for v, c in source_points):
        return False
    return source_points[0][0] <= volume_ul <= source_points[-1][0]


def _contextual_references(head_type: str, tip_id: str, reagent_family: str | None) -> list[dict[str, Any]]:
    """Publication context never attests to a local class's numeric settings."""
    if not reagent_family:
        return []
    references = []
    for method in _authored_methods():
        applies = method.applicability
        if method.status != "reference_candidate" or not any(
            family.casefold() == reagent_family.casefold() for family in applies.reagent_families
        ):
            continue
        if applies.head_types and head_type not in applies.head_types:
            continue
        if applies.tip_ids and tip_id not in applies.tip_ids:
            continue
        for evidence in method.evidence:
            if evidence.source_type == "publication" and evidence.source_url:
                references.append({
                    "method_id": method.method_id,
                    "title": method.title,
                    "source_type": evidence.source_type,
                    "source_url": evidence.source_url,
                    "note": evidence.note,
                    "relation": "context_only",
                    "qualifies_numeric_settings": False,
                })
    return sorted(references, key=lambda row: (row["method_id"], row["source_url"]))


def propose_liquid_classes(context: Mapping[str, Any], query: LiquidClassProposalQuery) -> dict[str, Any]:
    """Show exact, locally recorded settings from another profile, never run choices.

    Reagent and plate applicability are not encoded by a liquid-class record.
    The candidate therefore stays explicitly unverified even when its tip,
    head, capacity and calibration range match the request.
    """
    request = query.model_dump(mode="json")
    result: dict[str, Any] = {"query": request, "summary_reason": "", "candidates": [], "references": []}
    result["planning_liquid_class_candidates"] = result["candidates"]
    machine_id = str(context.get("machine_id") or "")
    head_type = str(context.get("head_type") or "")
    if context.get("controller_type") != "simulation":
        result["summary_reason"] = "Cross-profile planning references are available only in simulation."
        return result
    tips = {
        str(row.get("tip_id") or row.get("id")): row
        for row in context.get("tip_definitions", [])
        if isinstance(row, Mapping)
    }
    tip = tips.get(query.tip_id)
    if not tip or head_type not in (tip.get("compatible_heads") or tip.get("supported_head_types") or []):
        result["summary_reason"] = "Select an explicitly head-compatible tip before seeking a liquid class."
        return result
    capacity = tip.get("capacity_ul")
    if not isinstance(capacity, (int, float)) or query.volume_ul > capacity:
        result["summary_reason"] = "The requested stroke exceeds the selected tip's nominal capacity."
        return result
    active_classes = context.get("liquid_classes") or []
    if any(
        isinstance(row, Mapping) and row.get("tip_id") == query.tip_id
        for row in active_classes
    ):
        result["summary_reason"] = "An active liquid class exists; use normal method lookup for this machine."
        return result

    result["references"] = _contextual_references(head_type, query.tip_id, query.reagent_family)

    raw_classes = _raw_liquid_classes()
    if not raw_classes:
        result["summary_reason"] = "No raw local class records are available to substantiate a proposal."
        return result
    source_path = (
        "config/liquid_classes.yaml"
        if _liquid_store_path() == liquid_classes._STORE_PATH
        else "configured-liquid-class-store"
    )
    candidates = []
    for active in liquid_classes.list_liquid_classes(head_type=head_type, tip_id=query.tip_id):
        class_id = str(active.get("liquid_class_id") or "")
        raw = raw_classes.get(class_id)
        if (
            not raw
            or active.get("machine_id") == machine_id
            or raw.get("machine_id") != active.get("machine_id")
            or raw.get("head_type") != head_type
            or raw.get("tip_id") != query.tip_id  # no tip inferred from nominal capacity
            or not math.isclose(float(raw.get("tip_capacity_ul") or 0), float(capacity), abs_tol=1e-6)
            or not _explicit_motion(raw, active)
            or not _explicit_calibration(raw, active, query.volume_ul)
        ):
            continue
        source_machine = str(active["machine_id"])
        reagent = (query.reagent_family or "").strip()
        reagent_note = (
            f"This class has no recorded suitability for {reagent}; liquid-specific suitability is unverified."
            if reagent else
            "The source liquid is unspecified; liquid-specific suitability is unverified."
        )
        caveats = [
            f"Recorded for {source_machine}, not active machine {machine_id}; these values cannot be executed here.",
            reagent_note,
            "No reviewed method links these settings to the selected source/destination plates or pipetting heights.",
        ]
        if result["references"]:
            caveats.append("Related publications provide context only; they do not qualify this class's numeric settings.")
        desired_points = [float(point["desired_ul"]) for point in active["equation"]["control_points"]]
        nearest_point = min(desired_points, key=lambda point: (abs(point - query.volume_ul), point == 0, point))
        point_distance = abs(nearest_point - query.volume_ul)
        # A display/order metric only: it is not a reagent, plate, or transfer-performance score.
        proximity_score = round(100 * (1 - min(point_distance / float(capacity), 1)), 2)
        candidate = {
            "liquid_class_id": class_id,
            "name": active["name"],
            "machine_id": source_machine,
            "head_type": head_type,
            "tip_id": query.tip_id,
            "tip_capacity_ul": float(capacity),
            "source_machine_id": source_machine,
            "source_head_type": head_type,
            "source_tip_id": query.tip_id,
            "source_tip_capacity_ul": float(capacity),
            "status": "imported_unverified",
            "execution_ready": False,
            "can_apply_to_plan": False,
            "score": proximity_score,
            "score_scope": "calibration_control_point_proximity_only",
            "nearest_control_point_ul": nearest_point,
            "calibration_control_point_distance_ul": point_distance,
            "ranking_reason": (
                f"Nearest recorded desired-volume control point is {nearest_point:g} µL, "
                f"{point_distance:.4g} µL from the requested {query.volume_ul:g} µL. "
                "This ordering does not establish reagent or plate suitability."
            ),
            "aspirate": deepcopy(active["aspirate"]),
            "dispense": deepcopy(active["dispense"]),
            "equation": deepcopy(active["equation"]),
            "source_field_origins": _class_ref(active, raw).source_field_origins,
            "provenance": {
                "source_type": "local_config",
                "source_path": source_path,
                "source_digest": _digest(raw),
                "note": "Locally configured for another machine; not a reagent- or plate-qualified method.",
            },
            "reasons": [
                f"Explicitly recorded for {head_type} and {query.tip_id}.",
                f"The requested {query.volume_ul:g} µL stroke lies within the recorded calibration range.",
            ],
            "caveats": caveats,
            "missing_fields": [
                "active_machine_liquid_class",
                "reagent_applicability",
                "source_and_destination_labware_applicability",
                "aspirate.distance_from_bottom_mm",
                "dispense.distance_from_bottom_mm",
                "local_method_review",
            ],
        }
        candidates.append(candidate)
    candidates.sort(key=lambda row: (
        row["calibration_control_point_distance_ul"], row["name"].lower(), row["liquid_class_id"]
    ))
    result["candidates"] = candidates
    result["planning_liquid_class_candidates"] = candidates
    result["summary_reason"] = (
        "No class is installed for the active simulated machine and tip. "
        "Showing unverified, cross-profile local settings for planning review only."
        if candidates else
        "No exact head/tip/volume class with explicit local settings was found on another profile."
    )
    return result
