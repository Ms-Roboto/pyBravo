"""Conservative, catalog-backed tip-box choices for the configured head.

Names, capacity coincidences and fitted/teach-tip defaults are not compatibility
records. A recommendation needs rack geometry, an explicit rack-to-tip link,
and a tip definition that explicitly supports the head. This is selection help;
the reviewed compiler still checks the chosen inventory, liquid class and setup.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from pybravo.head_mode import head_geometry_for_type
from pybravo.types import HeadType


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unique_rows(rows: Iterable[Mapping[str, Any]], *identity_keys: str) -> dict[str, Mapping[str, Any]]:
    """Do not recommend an ambiguous catalog identifier with conflicting rows."""
    unique: dict[str, Mapping[str, Any]] = {}
    ambiguous = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identity = next((row.get(key) for key in identity_keys if row.get(key)), None)
        if not isinstance(identity, str):
            continue
        if identity in unique and unique[identity] != row:
            ambiguous.add(identity)
        else:
            unique[identity] = row
    return {identity: row for identity, row in unique.items() if identity not in ambiguous}


def _supports_head(tip: Mapping[str, Any], head: HeadType) -> bool:
    declarations = [tip.get(key) for key in ("compatible_heads", "supported_head_types") if tip.get(key)]
    # Missing membership means unknown, even though the general tips API treats
    # an empty list as a wildcard. Recommendations must establish compatibility.
    return bool(declarations) and all(
        isinstance(heads, (list, tuple)) and head.name in heads for heads in declarations
    )


def _candidate_calibration_allows_head(
    rack_id: str, rack: Mapping[str, Any], head: HeadType,
    tips: Mapping[str, Mapping[str, Any]], links: set[str],
    tip_offsets: Iterable[Mapping[str, Any]],
) -> bool:
    # An explicit rack-to-tip compatibility declaration is stronger than the
    # absence of calibration for this exact head.
    if any(identity in tips and _supports_head(tips[identity], head) for identity in links):
        return True

    def key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    calibrated_heads = set()
    for offset in tip_offsets:
        if not isinstance(offset, Mapping):
            continue
        if not ((offset.get("tipbox_id") and key(offset["tipbox_id"]) == key(rack_id))
                or (offset.get("tipbox") and key(offset["tipbox"]) == key(rack.get("name")))):
            continue
        try:
            value = offset.get("head_type")
            calibrated = HeadType[value] if isinstance(value, str) else HeadType(value)
        except (KeyError, ValueError, TypeError):
            continue
        calibrated_heads.add(calibrated)
    if not calibrated_heads or head in calibrated_heads:
        return True
    # Sharing a catalog tip is evidence of a common consumable family. This
    # permits e.g. the 16-channel ST head to see an incomplete 384 ST rack,
    # without guessing a family from rack names or numeric tip capacities.
    return any(_supports_head(tip, head) and _supports_head(tip, calibrated)
               for tip in tips.values() if tip.get("kind", "tip") == "tip"
               for calibrated in calibrated_heads)


def compatible_tipbox_choices(
    head_type: HeadType | str | int,
    labware: Iterable[Mapping[str, Any]],
    tip_definitions: Iterable[Mapping[str, Any]],
) -> list[dict]:
    """Return explicit compatible rack/tip pairs without filling missing metadata.

    For full 96/384-channel heads the rack must have that head's complete grid,
    regardless of the currently selected subset. For narrow 8/16-channel heads,
    the actual catalog grid must contain the complete head at matching pitch;
    no rack format is inferred from a short-tip/long-tip name or selected mode.
    """
    try:
        if isinstance(head_type, bool):
            return []
        head = HeadType[head_type] if isinstance(head_type, str) else HeadType(head_type)
    except (KeyError, TypeError, ValueError):
        return []
    if not head.is_disposable:
        return []
    geometry = head_geometry_for_type(head)
    racks = _unique_rows(labware, "id", "labware_id")
    tips = _unique_rows(tip_definitions, "tip_id", "id")
    choices = []
    for rack_id, rack in racks.items():
        if "tip_box" not in {rack.get("kind"), rack.get("base_class")}:
            continue
        rows, cols, wells = rack.get("rows"), rack.get("cols"), rack.get("wells")
        if not all(_positive_integer(value) for value in (rows, cols, wells)) or rows * cols != wells:
            continue
        if geometry.columns > 1:
            if (rows, cols) != (geometry.rows, geometry.columns):
                continue
        elif rows < geometry.rows or cols < geometry.columns:
            continue
        px, py = rack.get("spacing_x_mm"), rack.get("spacing_y_mm")
        if not (_positive(px) and _positive(py)
                and math.isclose(px, geometry.pitch_x_mm, rel_tol=0, abs_tol=1e-6)
                and math.isclose(py, geometry.pitch_y_mm, rel_tol=0, abs_tol=1e-6)):
            continue
        supported = rack.get("supported_tip_ids") or []
        if not isinstance(supported, (list, tuple)) or not all(isinstance(identity, str) for identity in supported):
            continue
        primary = rack.get("tip_definition_id")
        linked_ids = set(supported)
        if isinstance(primary, str) and primary and (not supported or primary in supported):
            linked_ids.add(primary)
        for tip_id in sorted(linked_ids):
            tip = tips.get(tip_id)
            if tip is None or tip.get("kind", "tip") != "tip" or not _supports_head(tip, head):
                continue
            capacity, length = tip.get("capacity_ul"), tip.get("length_mm")
            if not (_positive(capacity) and _positive(length)):
                continue
            choices.append({
                "labware_id": rack_id, "labware_name": str(rack.get("name") or rack_id),
                "tip_definition_id": tip_id, "tip_name": str(tip.get("label") or tip.get("name") or tip_id),
                "rows": rows, "cols": cols, "wells": wells,
                "spacing_x_mm": px, "spacing_y_mm": py,
                "tip_capacity_ul": capacity, "tip_length_mm": length,
            })
    return sorted(choices, key=lambda row: (row["labware_name"].casefold(), row["labware_id"], row["tip_capacity_ul"], row["tip_definition_id"]))


def tipbox_catalog_candidates(
    head_type: HeadType | str | int,
    labware: Iterable[Mapping[str, Any]],
    tip_definitions: Iterable[Mapping[str, Any]],
    *,
    tip_offsets: Iterable[Mapping[str, Any]] = (),
    limit: int = 20,
) -> list[dict]:
    """List plausible incomplete racks as catalog-maintenance candidates only.

    Recorded well count and both pitches must fit the full configured head.
    Unknown grid dimensions or tip associations stay unknown. Already verified
    racks and records with known conflicting geometry are not candidates.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("Candidate limit must be a nonnegative integer.")
    try:
        if isinstance(head_type, bool):
            return []
        head = HeadType[head_type] if isinstance(head_type, str) else HeadType(head_type)
    except (KeyError, TypeError, ValueError):
        return []
    if not head.is_disposable:
        return []
    geometry = head_geometry_for_type(head)
    racks = _unique_rows(labware, "id", "labware_id")
    tips = _unique_rows(tip_definitions, "tip_id", "id")
    offsets = list(tip_offsets)
    verified_racks = {row["labware_id"] for row in compatible_tipbox_choices(head, racks.values(), tips.values())}
    candidates = []
    for rack_id, rack in racks.items():
        if rack_id in verified_racks or "tip_box" not in {rack.get("kind"), rack.get("base_class")}:
            continue
        wells, rows, cols = rack.get("wells"), rack.get("rows"), rack.get("cols")
        px, py = rack.get("spacing_x_mm"), rack.get("spacing_y_mm")
        if not _positive_integer(wells):
            continue
        head_wells = geometry.rows * geometry.columns
        if (geometry.columns > 1 and wells != head_wells) or wells < head_wells:
            continue
        if not (_positive(px) and _positive(py)
                and math.isclose(px, geometry.pitch_x_mm, rel_tol=0, abs_tol=1e-6)
                and math.isclose(py, geometry.pitch_y_mm, rel_tol=0, abs_tol=1e-6)):
            continue
        known_rows, known_cols = _positive_integer(rows), _positive_integer(cols)
        if known_rows and known_cols and rows * cols != wells:
            continue
        if geometry.columns > 1:
            if (known_rows and rows != geometry.rows) or (known_cols and cols != geometry.columns):
                continue
        elif (known_rows and rows < geometry.rows) or (known_cols and cols < geometry.columns):
            continue
        missing = [] if known_rows and known_cols else ["rows_cols"]
        supported = rack.get("supported_tip_ids") or []
        primary = rack.get("tip_definition_id")
        links = {identity for identity in supported if isinstance(identity, str) and identity} if isinstance(supported, (list, tuple)) else set()
        if isinstance(primary, str) and primary and (not supported or primary in links):
            links.add(primary)
        if not _candidate_calibration_allows_head(rack_id, rack, head, tips, links, offsets):
            continue
        if not links:
            missing.append("tip_link")
        else:
            incomplete_options = []
            has_complete_tip = False
            for tip_id in sorted(links):
                tip = tips.get(tip_id)
                if tip is None:
                    incomplete_options.append(["tip_definition"])
                    continue
                if tip.get("kind", "tip") != "tip":
                    continue
                declarations = [tip.get(key) for key in ("compatible_heads", "supported_head_types") if tip.get(key)]
                if declarations and not _supports_head(tip, head):
                    continue  # A known incompatibility is not missing metadata.
                absent = [] if declarations else ["head_compatibility"]
                if not _positive(tip.get("capacity_ul")):
                    absent.append("tip_capacity")
                if not _positive(tip.get("length_mm")):
                    absent.append("tip_length")
                if absent:
                    incomplete_options.append(absent)
                else:
                    has_complete_tip = True
            if not has_complete_tip:
                if not incomplete_options:
                    continue
                missing.extend(sorted({field for option in incomplete_options for field in option}))
        if not missing:
            continue
        candidates.append({
            "labware_id": rack_id, "labware_name": str(rack.get("name") or rack_id),
            "rows": rows, "cols": cols, "wells": wells,
            "spacing_x_mm": px, "spacing_y_mm": py,
            "verified": False, "missing_metadata": missing,
        })
    return sorted(candidates, key=lambda row: (row["labware_name"].casefold(), row["labware_id"]))[:min(limit, 20)]
