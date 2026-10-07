"""Machine-readable protocol choices derived from the active Bravo context.

This describes what the reviewed Protocol Assistant can *propose*. It is not
an execution grant: the existing validator, strict simulation and approval
path remain authoritative. Runtime deck contents and inventory are omitted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pybravo.head_mode import head_geometry_for_type
from pybravo.types import HeadType

from .tipbox_choices import compatible_tipbox_choices, tipbox_catalog_candidates

STANDARD = "pybravo.bravo-capability-manifest"
SCHEMA_VERSION = "0.3.0"

_LABWARE_FIELDS = (
    "id", "name", "kind", "base_class", "wells", "rows", "cols",
    "spacing_x_mm", "spacing_y_mm", "well_volume_ul", "well_depth_mm",
    "well_diameter_mm", "well_geometry",
    "dead_volume_ul", "dead_volume_status",
    "height_mm", "stack_height_mm", "provisional", "supported_tip_ids",
    "tip_definition_id", "compatible_head_types",
)
_TIP_FIELDS = (
    "id", "tip_id", "label", "kind", "capacity_ul", "length_mm",
    "overflow_ul", "compatible_heads", "supported_head_types", "source",
)
_TIPBOX_FIELDS = (
    "labware_id", "labware_name", "tip_definition_id", "tip_name",
    "rows", "cols", "wells", "spacing_x_mm", "spacing_y_mm",
    "tip_capacity_ul", "tip_length_mm", "tip_row_stride", "tip_col_stride",
    "required_head_mode", "execution_ready", "missing_metadata",
)
_CANDIDATE_FIELDS = (
    "labware_id", "labware_name", "rows", "cols", "wells",
    "spacing_x_mm", "spacing_y_mm", "missing_metadata", "provisional",
)
_LIQUID_CLASS_FIELDS = ("id", "name", "machine_id", "head_type", "tip_id", "tip_capacity_ul")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _fields(row: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: _json_value(row[name]) for name in names if name in row}


def _rows(context: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = context.get(key)
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _status_counts(rows: list[dict[str, Any]], field: str = "status") -> dict[str, int]:
    """Summarize catalog evidence without promoting an entry to a run approval."""
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get(field) or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _positive(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _parameter(name: str, type_name: str = "string", *, unit: str | None = None,
               required: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "type": type_name, "required": required}
    if unit is not None:
        result["unit"] = unit
    return result


_P = _parameter

_OPERATION_SPECS: tuple[tuple[str, str, tuple[dict[str, Any], ...], tuple[str, ...],
                              tuple[str, ...], tuple[str, ...]], ...] = (
    ("transfer", "Move a per-channel volume between addressed source and destination wells.",
     (_P("source"), _P("destination"), _P("source_anchor"), _P("destination_anchor"),
      _P("volume_ul", "number", unit="uL"), _P("method_ref", "object", required=False)),
     ("disposable_head", "compatible_tip_pair", "confirmed_tip_inventory", "approved_liquid_class",
      "reachable_wells", "source_and_destination_capacity"),
     ("source_volume_decreased", "destination_volume_increased"),
     ("liquid/Aspirate", "liquid/Dispense")),
    ("distribute", "Dispense ordered volumes from one source; the compiler selects a safe aspiration pattern.",
     (_P("source"), _P("source_anchor"), _P("dispenses", "array"),
      _P("method_ref", "object", required=False)),
     ("disposable_head", "compatible_tip_pair", "confirmed_tip_inventory", "approved_liquid_class",
      "reachable_wells", "source_and_destination_capacity", "distribute_volume_accounting"),
     ("source_volume_decreased", "destination_volume_increased"),
     ("liquid/Aspirate", "liquid/Dispense")),
    ("mix", "Aspirate and dispense repeatedly within one addressed material.",
     (_P("material"), _P("anchor"), _P("volume_ul", "number", unit="uL"), _P("cycles", "integer"),
      _P("method_ref", "object", required=False)),
     ("disposable_head", "compatible_tip_pair", "confirmed_tip_inventory", "approved_liquid_class",
      "reachable_wells", "source_and_destination_capacity"),
     ("material_mixed",), ("liquid/Mix",)),
    ("move_plate", "Move a single accessible plate to an empty deck position.",
     (_P("material"), _P("destination_slot", "integer")),
     ("configured_gripper", "top_plate_access", "deck_destination_available"),
     ("plate_relocated",), ("plate/PickPlace",)),
    ("destack_plate", "Remove the top plate from a stack into an empty position.",
     (_P("material"), _P("destination_slot", "integer")),
     ("configured_gripper", "top_plate_access", "source_stack_has_multiple_plates",
      "deck_destination_available"),
     ("plate_relocated", "source_stack_decreased"), ("plate/Destack",)),
    ("stack_plate", "Place an accessible plate on a compatible occupied stack.",
     (_P("material"), _P("destination_slot", "integer")),
     ("configured_gripper", "top_plate_access", "compatible_destination_stack"),
     ("plate_relocated", "destination_stack_increased"), ("plate/Stack",)),
    ("wait", "Wait for a specified elapsed duration; no temperature control is implied.",
     (_P("duration_s", "number", unit="s"),), ("positive_duration",),
     ("elapsed_time",), ("system/Wait",)),
    ("manual", "Pause for a stated operator action or external instrument handoff.",
     (_P("message"), _P("duration_s", "number", unit="s", required=False)),
     ("operator_instruction",), ("operator_checkpoint",), ("system/Manual",)),
    ("repeat", "Repeat a bounded sequence of assistant steps.",
     (_P("repeat", "integer"), _P("steps", "array")),
     ("bounded_repeat",), ("steps_repeated",), ()),
)

_ROBOT_DESCRIPTIONS = {
    "tips/TipsOn": "Pick up the validated tip footprint from a confirmed supply rack.",
    "tips/TipsOff": "Return or discard the loaded tip footprint according to setup.",
    "liquid/Aspirate": "Aspirate liquid from the addressed source wells.",
    "liquid/Dispense": "Dispense liquid into the addressed destination wells.",
    "liquid/Mix": "Perform repeated aspiration and dispense at one location.",
    "plate/PickPlace": "Move an accessible plate with the configured gripper.",
    "plate/Stack": "Place a plate onto a compatible existing stack.",
    "plate/Destack": "Remove the top plate from a stack.",
    "system/Wait": "Run a timed wait.",
    "system/Manual": "Pause for an operator checkpoint.",
}

_CONSTRAINTS = {
    "disposable_head": "Protocol Assistant liquid actions require a configured disposable-tip head.",
    "compatible_tip_pair": "Rack geometry, rack-to-tip link and tip-to-head membership must match exactly.",
    "confirmed_tip_inventory": "The scientist must confirm actual fresh tips; catalog compatibility is not inventory.",
    "approved_liquid_class": "The chosen liquid class must match the active machine, head and selected tip.",
    "reachable_wells": "Head footprint and selected anchors must fit each plate's verified grid and pitch.",
    "source_and_destination_capacity": "Source stays above dead volume; destination stays below per-well capacity.",
    "distribute_volume_accounting": "One shared aspiration requires a permitting method, effective tip capacity and calibrated command volume; otherwise the compiler uses safe paired actions or reports a validation error.",
    "configured_gripper": "The active profile must provide the Bravo gripper axes.",
    "top_plate_access": "Only the top plate of a stack may be addressed or moved.",
    "deck_destination_available": "A plate move or destack requires an empty destination position.",
    "source_stack_has_multiple_plates": "Destack requires at least two plates at the source position.",
    "compatible_destination_stack": "Stack requires a same-type plate already at the destination.",
    "positive_duration": "Elapsed wait time must be positive and finite.",
    "operator_instruction": "A manual checkpoint must state the operator's action.",
    "bounded_repeat": "Repeat count, nesting and expanded operation count must fit compiler limits.",
    "source_volume_decreased": "Each addressed source well loses the aspirated volume.",
    "destination_volume_increased": "Each addressed destination well gains the dispensed volume.",
    "material_mixed": "Liquid is aspirated and dispensed within the same addressed material.",
    "plate_relocated": "The moved plate's deck position changes.",
    "source_stack_decreased": "The source stack loses its top plate.",
    "destination_stack_increased": "The destination stack gains a top plate.",
    "elapsed_time": "The workflow includes the specified timed wait.",
    "operator_checkpoint": "Execution pauses for an operator action.",
    "steps_repeated": "The nested body expands up to the reviewed repeat count.",
    "st70_excluded_from_1536": "ST70 tips are incompatible with 1536-well microplates.",
    "provisional_labware_excluded": "Provisional catalog labware is for review and cannot compile for execution.",
}

_REVIEW_REQUIREMENTS = {
    "physical_labware_identity": "Confirm every physical plate and rack against its selected catalog ID.",
    "deck_layout_and_clearance": "Confirm deck positions, stack order, gripper clearance and plate teachpoints.",
    "fresh_tip_inventory": "Inspect each supply rack and record the fresh tip wells before every run.",
    "tip_identity_and_reuse": "Confirm loaded tip IDs, disposal and any source-specific reuse rationale.",
    "liquid_setup": "Confirm source/dead volumes, approved liquid class, head mode and pipetting height.",
    "simulation_and_approval": "Validate, strictly simulate and obtain scientist approval before release.",
}


def _head(context: Mapping[str, Any]) -> tuple[HeadType | None, dict[str, Any]]:
    value = context.get("head_type")
    identity = {key: context[key] for key in ("machine_id", "profile_name", "controller_type")
                if isinstance(context.get(key), str) and context[key]}
    try:
        head = HeadType[value] if isinstance(value, str) else HeadType(value)
    except (KeyError, TypeError, ValueError):
        return None, {**identity, "head_type": value, "geometry": None,
                      "has_gripper": context.get("has_gripper") is True, "deck_slots": list(range(1, 10))}
    geometry = head_geometry_for_type(head)
    return head, {
        **identity,
        "head_type": head.name,
        "geometry": {"rows": geometry.rows, "columns": geometry.columns,
                     "pitch_x_mm": geometry.pitch_x_mm, "pitch_y_mm": geometry.pitch_y_mm},
        "has_gripper": context.get("has_gripper") is True,
        "deck_slots": list(range(1, 10)),
        "max_operations": min(1000, max(1, int(context.get("max_operations") or 500))),
        **({"head_max_volume_ul": context["head_max_volume_ul"]}
           if _positive(context.get("head_max_volume_ul")) else {}),
    }


def _assistant_operations(head: HeadType | None, has_gripper: bool) -> list[dict[str, Any]]:
    operations = []
    for identity, description, parameters, preconditions, effects, lowering in _OPERATION_SPECS:
        if identity in {"transfer", "distribute", "mix"}:
            selectable = bool(head and head.is_disposable)
        elif identity in {"move_plate", "destack_plate", "stack_plate"}:
            selectable = has_gripper
        else:
            selectable = True
        operations.append({
            "id": identity, "description": description, "selectable": selectable,
            "availability": "available" if selectable else "unavailable",
            "parameters": [dict(item) for item in parameters],
            "preconditions": list(preconditions), "effects": list(effects),
            "lowers_to": list(lowering),
        })
    return operations


def _transfer_patterns(head: HeadType | None, labware: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if head is None or not head.is_disposable:
        return []
    geometry = head_geometry_for_type(head)
    if (geometry.rows, geometry.columns, geometry.pitch_x_mm, geometry.pitch_y_mm) != (16, 24, 4.5, 4.5):
        return []

    def has_grid(rows: int, cols: int, pitch: float) -> bool:
        return any(row.get("base_class") == "microplate" and not row.get("provisional")
                   and row.get("rows") == rows and row.get("cols") == cols
                   and _positive(row.get("spacing_x_mm")) and _positive(row.get("spacing_y_mm"))
                   and math.isclose(row["spacing_x_mm"], pitch, abs_tol=1e-6)
                   and math.isclose(row["spacing_y_mm"], pitch, abs_tol=1e-6)
                   for row in labware)

    if not (has_grid(16, 24, 4.5) and has_grid(32, 48, 2.25)):
        return []
    return [{
        "id": "384_to_1536_quadrant", "description": "A full 384-channel source footprint maps to one 1536-well quadrant.",
        "source_labware_wells": 384, "destination_labware_wells": 1536,
        "source_anchor": "A1", "destination_anchors": ["A1", "A2", "B1", "B2"],
        "status": "geometry_option", "review_required": True,
    }]


def _tip_plate_compatibility(
    head: HeadType | None,
    labware: list[dict[str, Any]],
    tips: list[dict[str, Any]],
    choices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """State only known tip/plate facts, never a general compatibility matrix."""
    if head is None or not head.is_disposable:
        return []
    known_tips = {tip["id"] for tip in tips}
    ready_pairs = {choice["tip_definition_id"] for choice in choices if choice["execution_ready"]}
    rows = []
    for plate in labware:
        if plate.get("base_class") != "microplate" or plate.get("wells") != 1536 or plate.get("provisional"):
            continue
        common = {"target_labware_id": plate["id"], "target_wells": 1536,
                  "applies_to": ["source", "destination", "mix"]}
        if "st_70ul" in known_tips:
            # This is the exact exclusion enforced by the protocol validator
            # for any aspirate, dispense or mix touching 1536-well labware.
            rows.append({**common, "tip_definition_id": "st_70ul", "compatible": False,
                         "status": "incompatible", "execution_ready": False,
                         "provenance": "protocol_validator",
                         "reason": "ST70 tips cannot be used with 1536-well plates."})
        valid_target_grid = (
            plate.get("rows") == 32 and plate.get("cols") == 48
            and _positive(plate.get("spacing_x_mm")) and _positive(plate.get("spacing_y_mm"))
            and math.isclose(plate["spacing_x_mm"], 2.25, abs_tol=1e-6)
            and math.isclose(plate["spacing_y_mm"], 2.25, abs_tol=1e-6)
            and _positive(plate.get("well_volume_ul"))
        )
        if (head in {HeadType.HT_384_D_70, HeadType.HT_384_D_70_S2}
                and valid_target_grid and "st_10ul" in ready_pairs):
            # Agilent documents ST10 for 1536; the validator can address this
            # grid with a 384ST footprint. Actual liquid-handling setup and
            # physical alignment remain unconfirmed at manifest time.
            rows.append({**common, "tip_definition_id": "st_10ul", "compatible": True,
                         "status": "planning_compatible", "execution_ready": False,
                         "provenance": "catalog_pair_and_validator",
                         "reason": "Catalog tip/rack pair and 384ST-to-1536 grid support a review draft; "
                                   "confirm liquid class, tip inventory, plate and alignment before execution."})
    return sorted(rows, key=lambda row: (row["target_labware_id"], row["tip_definition_id"]))


def _setup_options(head: HeadType | None) -> dict[str, Any]:
    geometry = head_geometry_for_type(head) if head is not None else None
    modes = ["all_barrels", "single_barrel"]
    if geometry is not None and geometry.rows > 1:
        modes.append("row")
    if geometry is not None and geometry.columns > 1:
        modes.append("column")
    if geometry is not None and geometry.rows > 1 and geometry.columns > 1:
        modes.append("rectangle")
    head_modes = []
    if head and head.is_disposable:
        for mode in modes:
            required_fields = ["subset_type", "subset_config"]
            if mode in {"row", "rectangle"}:
                required_fields.append("row_count")
            if mode in {"column", "rectangle"}:
                required_fields.append("column_count")
            option: dict[str, Any] = {
                "id": mode, "required_fields": required_fields,
                "subset_config_values": (["back_left"] if mode == "all_barrels" else
                                         ["back_left", "back_right", "front_left", "front_right"]),
                "requires_confirmation": True,
                "compatible_tip_pair_check_required": True,
            }
            if "row_count" in required_fields:
                option["row_count_range"] = [1, geometry.rows]
            if "column_count" in required_fields:
                option["column_count_range"] = [1, geometry.columns]
            head_modes.append(option)
    return {
        "tip_strategies": [
            {"id": "fresh_each_step", "description": "Pick a fresh set for each liquid step."},
            {"id": "fresh_each_source", "description": "Use a dedicated tip set per source; requires a reuse rationale.",
             "requires_reason": True},
            {"id": "reuse_all", "description": "Reuse a tip set across steps; requires a contamination assessment.",
             "requires_reason": True},
        ],
        "tip_disposals": [
            {"id": "waste_container", "description": "Use the ID of a configured waste material, not this option ID.",
             "value_kind": "material_id", "required_material_role": "waste",
             "setup_field": "tip_disposal_id"},
            {"id": "return_to_source_rack", "description": "Return spent tips to their own rack without reselecting them.",
             "value_kind": "literal", "setup_value": "return_to_source_rack",
             "setup_field": "tip_disposal_id",
             "requires_tip_strategy": "fresh_each_source"},
        ],
        "head_modes": head_modes,
    }


def _setup_decision_rules(head: HeadType | None) -> list[dict[str, Any]]:
    """Publish reviewable setup reasoning, without inventing experimental data.

    Predicates refer to facts extracted from a particular proposed plan, not
    live instrument state. A consumer must establish every predicate before it
    applies a recommendation; missing facts never count as ``False``.
    """
    if head is None or not head.is_disposable:
        return []

    def predicate(fact: str, value: Any, op: str = "eq") -> dict[str, Any]:
        return {"fact": fact, "op": op, "value": value}

    def rule(identity: str, decision: str, conditions: list[dict[str, Any]],
             recommendation: Any, evidence_level: str, rationale: str,
             provenance: list[str], required_evidence: list[str],
             **extra: Any) -> dict[str, Any]:
        return {
            "id": identity, "decision": decision, "when": {"all": conditions},
            "recommendation": recommendation, "evidence_level": evidence_level,
            "requires_confirmation": True, "rationale": rationale,
            "provenance": provenance, "required_evidence": required_evidence,
            **extra,
        }

    liquid_step = predicate("liquid_step_count", 1, "gte")
    return [
        rule(
            "head_mode_full_footprint", "/setup/head_mode",
            [liquid_step, predicate("full_head_footprint", True)],
            {"subset_type": "all_barrels", "subset_config": "back_left",
             "row_count": None, "column_count": None},
            "heuristic",
            "An affirmative all-wells instruction and matching catalog grid suggest all barrels; "
            "the scientist must confirm intended coverage. The back-left corner is canonical, and "
            "row and column counts come from the configured head rather than setup entry.",
            ["pybravo.head_mode.normalize_head_mode", "pybravo.workflow.protocols.validation"],
            ["Affirmative source evidence or a structured scientist decision states full-head coverage "
             "for every liquid step on verified plate grids."],
        ),
        rule(
            "head_mode_partial_selection", "/setup/head_mode",
            [liquid_step, predicate("full_head_footprint", False)], None,
            "scientist_input",
            "A partial footprint needs the intended channel shape, size and corner; plate well count "
            "alone does not identify which barrels should be active.",
            ["pybravo.head_mode.normalize_head_mode", "pybravo.workflow.protocols.validation"],
            ["Intended active channels or target region, orientation and verified tip-rack footprint."],
        ),
        rule(
            "tip_strategy_dedicated_source", "/setup/tip_strategy",
            [liquid_step, predicate("source_count", 2, "gte"),
             predicate("dedicated_tip_rack_per_source", True),
             predicate("destination_initially_empty", True),
             predicate("same_source_reuse_authorized", True)],
            "fresh_each_source", "heuristic",
            "A dedicated tip set per source isolates sources while allowing that set to dispense "
            "into initially empty destinations. Reuse still needs the scientist's contamination assessment.",
            ["pybravo.workflow.protocols.validation", "scientist_confirmed_reuse_intent"],
            ["One physically distinct compatible rack per source in execution order.",
             "Scientist authorization to reuse only within the same source.",
             "Destination plates are initially empty and the procedure does not mix sources through tips."],
        ),
        rule(
            "tip_strategy_fresh_step", "/setup/tip_strategy",
            [liquid_step, predicate("same_source_reuse_authorized", False)],
            "fresh_each_step", "heuristic",
            "Fresh tips for each liquid step avoid assumed reuse. The plan must have enough confirmed "
            "compatible tips for every pickup.",
            ["pybravo.workflow.protocols.validation"],
            ["Explicit decision not to reuse tips within a source.",
             "Sufficient inspected fresh-tip footprints for every liquid step."],
        ),
        rule(
            "tip_strategy_quadrant_conditional", "/setup/tip_strategy",
            [liquid_step, predicate("four_source_quadrant_recipe", True),
             predicate("four_source_five_ul_pairings", True),
             predicate("full_head_footprint", True),
             predicate("dedicated_tip_rack_per_source", True),
             predicate("st10_384_dedicated_racks", True),
             predicate("destination_footprints_disjoint", True),
             predicate("no_recorded_destination_liquid", True),
             predicate("same_source_reuse_prohibited", False)],
            "fresh_each_source", "heuristic",
            "The four-source quadrant recipe can isolate sources with one dedicated ST tip set per source. "
            "This is a conditional draft choice: confirm both destinations start empty, each dispense avoids "
            "contact with destination liquid, and the same source's tip set may serve both plates. "
            "Two 5 µL dispenses exactly fill a nominal ST10 tip, leaving no allowance for residual liquid "
            "or an air gap. Keep two separate 5 µL aspirations unless a reviewed method proves a shared "
            "aspiration safe. A contamination assessment is still required before execution.",
            ["config/protocol_recipes.yaml#four_384_to_1536_quadrants", "cited_protocol_text",
             "active_head_catalog_pair"],
            ["Scientist confirms both destination plates start empty and records their starting volumes.",
             "Selected dispense method avoids destination-liquid contact or has a reviewed carryover policy.",
             "Scientist records a same-source tip-reuse contamination assessment and confirms physical racks."],
        ),
        rule(
            "tip_rack_order_by_source", "/setup/tip_rack_ids",
            [liquid_step, predicate("source_count", 2, "gte"),
             predicate("dedicated_tip_rack_per_source", True),
             predicate("tip_strategy", "fresh_each_source")],
            {"resolver": "source_ordered_verified_tip_rack_ids"}, "heuristic",
            "Pair one exact catalog-compatible tip rack with each source in first-use order. "
            "A rack ID encoding its source ID is a review candidate, not a verified association; "
            "an explicit scientist mapping provides stronger evidence. Physical identity and inventory must be checked.",
            ["pybravo.workflow.protocols.validation", "active_head_catalog_pair", "scientist_source_rack_mapping"],
            ["One distinct tip material and exact rack–tip catalog pair per source.",
             "Source execution order and rack order are both known.",
             "Physical rack contents and fresh-tip wells are confirmed before release."],
        ),
        rule(
            "tip_disposal_return_to_source", "/setup/tip_disposal_id",
            [liquid_step, predicate("tip_strategy", "fresh_each_source"),
             predicate("dedicated_tip_rack_per_source", True),
             predicate("waste_material_present", False)],
            "return_to_source_rack", "heuristic",
            "With source-dedicated racks and no waste receptacle in the plan, spent tips can return "
            "to their original wells only if they will never be selected again.",
            ["pybravo.workflow.protocols.validation"],
            ["The original rack can accept each spent tip footprint.",
             "Returned wells are marked spent and cannot be reused.",
             "Scientist confirms the physical rack and contamination policy."],
        ),
        rule(
            "tip_disposal_waste_material", "/setup/tip_disposal_id",
            [liquid_step, predicate("waste_material_present", True)], None,
            "scientist_input",
            "Select the material ID of an on-deck waste receptacle; the option name "
            "'waste_container' is not a setup value.",
            ["pybravo.workflow.protocols.validation"],
            ["A material with role waste is placed on an available deck position and has capacity."],
        ),
        rule(
            "pipetting_height_qualification", "/setup/distance_from_bottom_mm",
            [liquid_step], None, "scientist_input",
            "Well depth only bounds the allowed height. It does not establish a safe aspiration or "
            "dispense position for the liquid, tip, well shape and volume.",
            ["pybravo.workflow.protocols.validation", "labware_catalog.well_depth_mm"],
            ["Qualified height for the selected tip, liquid class, labware and volume."],
            bounds={"minimum_inclusive": 0, "maximum_exclusive_fact": "minimum_addressed_well_depth_mm"},
        ),
        rule(
            "liquid_class_qualification", "/setup/liquid_class",
            [liquid_step], None, "scientist_input",
            "A catalog entry is not evidence that a liquid class is suitable for this reagent or procedure.",
            ["pybravo.workflow.protocols.validation", "active_liquid_class_catalog"],
            ["Approved liquid class for the active machine, head, loaded tip and reagent."],
        ),
        rule(
            "tip_reuse_assessment", "/setup/tip_reuse_reason",
            [liquid_step, predicate("tip_strategy", ["fresh_each_source", "reuse_all"], "in")],
            None, "scientist_input",
            "The contamination rationale must be supplied or approved by the scientist; the model "
            "must not invent one from empty destination status alone.",
            ["pybravo.workflow.protocols.validation"],
            ["Scientist's explicit contamination and carryover assessment."],
        ),
    ]


def build_capability_manifest(context: dict) -> dict:
    """Build a sanitized, versioned choice dictionary from ``machine_context``.

    A listed catalog option is a possible *review draft*, not proof of physical
    presence, filled tips, calibrated technique or execution approval.
    """
    head, machine = _head(context)
    labware = []
    for row in _rows(context, "labware"):
        item = _fields(row, _LABWARE_FIELDS)
        if not isinstance(item.get("id"), str) or not item["id"]:
            continue
        item["catalog_status"] = "provisional" if item.get("provisional") else "catalog_entry"
        labware.append(item)
    labware.sort(key=lambda item: item["id"])

    tips = []
    for row in _rows(context, "tip_definitions"):
        item = _fields(row, _TIP_FIELDS)
        identity = item.get("tip_id") or item.get("id")
        if not isinstance(identity, str) or not identity:
            continue
        item["id"] = identity
        item.pop("tip_id", None)
        item["catalog_status"] = "defined" if _positive(item.get("capacity_ul")) else "incomplete"
        tips.append(item)
    tips.sort(key=lambda item: item["id"])

    # Recompute from the catalog snapshot. A stale or malformed supplied
    # `tipbox_choices` list must never promote a provisional/incompatible rack.
    verified_pairs = compatible_tipbox_choices(head, labware, tips) if head else []
    choices = []
    for row in verified_pairs:
        item = _fields(row, _TIPBOX_FIELDS)
        if not item.get("labware_id") or not item.get("tip_definition_id"):
            continue
        item["execution_ready"] = row.get("execution_ready") is True
        item["missing_metadata"] = list(row.get("missing_metadata") or [])
        item["status"] = "catalog_verified" if item["execution_ready"] else "planning_only"
        item["provenance"] = "active_head_catalog_pair"
        choices.append(item)
    choices.sort(key=lambda item: (item["labware_id"], item["tip_definition_id"]))

    catalog_candidates = tipbox_catalog_candidates(
        head, labware, tips, tip_offsets=_rows(context, "tip_offsets"),
    ) if head else []
    candidates = []
    for row in catalog_candidates:
        item = _fields(row, _CANDIDATE_FIELDS)
        if not item.get("labware_id"):
            continue
        item.update(status="catalog_candidate", execution_ready=False,
                    provenance="incomplete_catalog_record")
        candidates.append(item)
    candidates.sort(key=lambda item: item["labware_id"])

    liquid_classes = []
    for row in _rows(context, "liquid_classes"):
        item = _fields(row, _LIQUID_CLASS_FIELDS)
        identity = item.get("name") or item.get("id")
        if identity:
            item["id"] = identity
            liquid_classes.append(item)
    liquid_classes.sort(key=lambda item: item["id"])

    # The method registry is a separate, versioned knowledge layer.  BCM
    # advertises where to query it and its digest, never its numeric setpoints.
    from .methods import method_registry

    registry = method_registry(context)
    method_summaries = registry.get("methods") or []
    method_reference = {
        "standard": registry["standard"],
        "schema_version": registry["schema_version"],
        "digest": registry["digest"],
        "registry_url": "/api/protocols/methods",
        "lookup_url": "/api/protocols/methods/lookup",
        "method_count": len(method_summaries),
        "status_counts": _status_counts(method_summaries),
    }

    class_summaries = [
        {"status": "identity_complete" if all(item.get(key) for key in
         ("id", "machine_id", "head_type", "tip_id")) else "incomplete"}
        for item in liquid_classes
    ]
    catalog_summary = {
        "labware": {"count": len(labware), "status_counts": _status_counts(labware, "catalog_status"),
                    "provenance": "active_labware_catalog"},
        "tip_definitions": {"count": len(tips), "status_counts": _status_counts(tips, "catalog_status"),
                            "provenance_sources": sorted({str(item["source"]) for item in tips
                                                          if item.get("source")})},
        "tipbox_pairs": {"count": len(choices), "status_counts": _status_counts(choices),
                         "provenance": "active_head_catalog_pair"},
        "liquid_classes": {"count": len(liquid_classes), "status_counts": _status_counts(class_summaries),
                           "provenance": "active_liquid_class_store"},
    }

    robot_operations = []
    for identity, description in _ROBOT_DESCRIPTIONS.items():
        if identity.startswith(("tips/", "liquid/")):
            available = bool(head and head.is_disposable)
        elif identity.startswith("plate/"):
            available = machine["has_gripper"]
        else:
            available = True
        robot_operations.append({"id": identity, "description": description,
                                 "model_selectable": False,
                                 "availability": "available" if available else "unavailable"})

    return {
        "schema_version": SCHEMA_VERSION, "standard": STANDARD,
        "context_hash": str(context.get("context_hash") or ""), "machine": machine,
        "assistant_operations": _assistant_operations(head, machine["has_gripper"]),
        "robot_operations": robot_operations,
        "tipbox_choices": choices, "tipbox_catalog_candidates": candidates,
        "labware": labware, "tip_definitions": tips, "liquid_classes": liquid_classes,
        "setup_options": _setup_options(head),
        "setup_decision_rules": _setup_decision_rules(head),
        "transfer_patterns": _transfer_patterns(head, labware),
        "tip_plate_compatibility": _tip_plate_compatibility(head, labware, tips, choices),
        "catalog_summary": catalog_summary,
        "method_registry": method_reference,
        "constraints": [{"id": identity, "description": description}
                        for identity, description in _CONSTRAINTS.items()],
        "review_requirements": [{"id": identity, "description": description}
                                for identity, description in _REVIEW_REQUIREMENTS.items()],
    }


def compact_capability_options(manifest: dict) -> dict:
    """Return the bounded subset the local model needs when choosing a draft.

    The caller supplies small catalog excerpts separately. This selection
    therefore contains action semantics and setup choices only; it does not
    duplicate any plate, tip, pair or liquid-class rows.
    """
    operations = [{
        "id": row["id"], "selectable": row["selectable"],
        "parameters": [parameter["name"] for parameter in row["parameters"] if parameter["required"]],
        "lowers_to": row["lowers_to"],
    } for row in manifest.get("assistant_operations", [])]
    setup = manifest.get("setup_options") or {}

    def compact_rule(row: dict) -> dict:
        result = {key: row[key] for key in (
            "id", "decision", "when", "recommendation", "evidence_level", "rationale",
            "required_evidence", "bounds") if key in row}
        # The full, citable reasoning stays in the published manifest and the
        # deterministic resolver. Give the small local model only the decision
        # cue; repeating every qualification in its prompt exceeds its budget.
        if row.get("recommendation") is not None:
            result["rationale"] = row.get("rationale", "").split(". ", 1)[0].rstrip(".") + "."
            result["required_evidence"] = list(row.get("required_evidence") or [])[:1]
        if row.get("id") == "tip_strategy_quadrant_conditional":
            result["rationale"] = (
                "Conditional draft for four 384 sources and two empty 1536 destinations: dedicate one "
                "ST10 tip set per source, assess noncontact reuse, and keep separate 5 µL aspirations "
                "until a reviewed method supports sharing."
            )
        return result

    return {
        "schema_version": manifest.get("schema_version"),
        "context_hash": manifest.get("context_hash"),
        "method_registry": {key: manifest.get("method_registry", {}).get(key)
                            for key in ("schema_version", "digest", "lookup_url")},
        "machine": {key: manifest.get("machine", {}).get(key) for key in ("head_type", "geometry", "has_gripper")},
        "assistant_operations": operations,
        "setup_options": {
            "tip_strategies": [{key: item[key] for key in ("id", "requires_reason") if key in item}
                               for item in setup.get("tip_strategies", [])],
            "tip_disposals": [{key: item[key] for key in (
                "id", "value_kind", "required_material_role", "setup_value", "setup_field",
                "requires_tip_strategy") if key in item} for item in setup.get("tip_disposals", [])],
            "head_modes": [{key: item[key] for key in (
                "id", "required_fields", "requires_confirmation", "compatible_tip_pair_check_required")
                if key in item} for item in setup.get("head_modes", [])],
        },
        "setup_decision_rules": [compact_rule(row) for row in manifest.get("setup_decision_rules", [])],
        "transfer_patterns": [{key: row[key] for key in (
            "id", "source_labware_wells", "destination_labware_wells", "source_anchor", "destination_anchors")
            if key in row} for row in manifest.get("transfer_patterns", [])],
        "tip_plate_compatibility": [{key: row[key] for key in (
            "tip_definition_id", "target_labware_id", "compatible", "status") if key in row}
            for row in manifest.get("tip_plate_compatibility", [])],
        "review_requirements": [row["id"] for row in manifest.get("review_requirements", [])],
    }
