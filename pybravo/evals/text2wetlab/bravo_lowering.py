"""Lower model-authored ordered actions into an unreviewed Bravo Designer draft.

The caller supplies explicit Bravo bindings and read-only catalogs for both
machines. This module never chooses experimental content, saves a draft, runs a
robot, or treats a Designer graph as approved. Unsupported OT-2 module actions
remain ordered manual handoffs with grouped blockers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pybravo.head_mode import normalize_head_mode
from pybravo.types import HeadType
from pybravo.workflow.drafter.schema import (
    DraftedDeckItem,
    DraftedGraph,
    DraftedLink,
    DraftedNode,
    DraftedWorkflow,
    SourceCitation,
)

from .action_ir import (
    ActionPlan,
    ActionPlanError,
    Comment,
    Delay,
    Drop,
    ForEach,
    LabwareFacts,
    Mix,
    Pause,
    Pickup,
    PipetteFacts,
    RefillTips,
    SourceSpan,
    Stroke,
    validate_action_evidence,
)

_WELL = re.compile(r"([A-Z])([1-9][0-9]*)\Z")
_SYMBOL = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\Z")
_MODULE_KINDS = frozenset({
    "set_temperature", "set_block_temperature", "set_lid_temperature",
    "open_lid", "close_lid", "magnet_engage", "magnet_disengage",
})
_REVIEW = (
    "Confirm each physical Bravo labware identity, deck position, and tip inventory.",
    "Review reagent suitability, liquid method, starting well volumes, pipetting height, and source citations with a scientist.",
    "Run strict Bravo simulation and obtain approval before execution.",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BravoLabwareBinding(_Strict):
    labware_id: str = Field(min_length=1)
    slot: int = Field(ge=1, le=9)
    tip_definition_id: str | None = None


class BravoPickupBinding(_Strict):
    rack_id: str = Field(min_length=1)
    tip_anchor_row: int = Field(ge=0)
    tip_anchor_col: int = Field(ge=0)


class BravoLoweringSetup(_Strict):
    """Explicit proposed mappings; these are review inputs, not approvals."""

    labware: dict[str, BravoLabwareBinding]
    pickup: dict[str, BravoPickupBinding]
    disposal: BravoLabwareBinding | None = None
    liquid_class_id: str = Field(min_length=1)
    distance_from_bottom_mm: float = Field(ge=0)
    head_mode: dict[str, Any]


@dataclass(frozen=True)
class LoweringResult:
    """Reviewable graph plus grouped blockers; never an executable release.

    A trusted caller may persist ``workflow`` through
    ``WorkflowStorage.create_generated_draft`` with ``issues=list(blockers)``.
    The ordinary Designer create route cannot establish generated lineage.
    """

    status: str
    workflow: dict[str, Any] | None
    blockers: tuple[dict[str, Any], ...]
    review_requirements: tuple[str, ...]
    source_plan_sha256: str | None
    bravo_context_hash: str | None


def _positive(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _rows(context: Mapping[str, Any], key: str, identity: str) -> dict[str, dict]:
    unique: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for row in context.get(key) or []:
        if not isinstance(row, dict):
            continue
        name = row.get(identity)
        if not isinstance(name, str) or not name:
            continue
        if name in unique and unique[name] != row:
            ambiguous.add(name)
        unique[name] = row
    return {name: row for name, row in unique.items() if name not in ambiguous}


def _well_in_grid(well: str, row: Mapping[str, Any]) -> bool:
    match = _WELL.fullmatch(well)
    return bool(match and isinstance(row.get("rows"), int)
                and isinstance(row.get("cols"), int)
                and ord(match.group(1)) - ord("A") < row["rows"]
                and int(match.group(2)) <= row["cols"])


def _expand(plan: ActionPlan, add) -> list[tuple[Any, str, str, tuple[str, ...]]]:
    """Expand only explicit model-authored well bindings, retaining each row path."""
    expanded: list[tuple[Any, str, str, tuple[str, ...]]] = []
    for index, action in enumerate(plan.actions):
        path = f"actions[{index}]"
        if not isinstance(action, ForEach):
            expanded.append((action, path, path, ()))
            continue
        if action.selector is not None:
            add("CATALOG_SELECTOR_REMAP_REQUIRED", path,
                "The OT-2 catalog selector has no reviewed Bravo well pairing.", action.kind)
            continue
        if not action.bindings:
            add("WELL_BINDINGS_MISSING", path, "Provide explicit ordered well bindings.", action.kind)
            continue
        names = set(action.bindings[0])
        used = {symbol.group(1) for item in action.actions if isinstance(item, (Stroke, Mix))
                if (symbol := _SYMBOL.fullmatch(item.well)) is not None}
        if not names or names != used or any(set(row) != names for row in action.bindings):
            add("WELL_BINDINGS_INVALID", path,
                "Every named well binding must be used exactly by the ordered body.", action.kind)
            continue
        for row_index, row in enumerate(action.bindings):
            for body_index, item in enumerate(action.actions):
                original = f"{path}.actions[{body_index}]"
                instance = f"{path}.bindings[{row_index}].actions[{body_index}]"
                if isinstance(item, (Stroke, Mix)) and item.well.startswith("$"):
                    symbol = _SYMBOL.fullmatch(item.well)
                    if symbol is None or symbol.group(1) not in row:
                        add("WELL_BINDINGS_INVALID", instance,
                            "The action refers to an absent well binding.", item.kind)
                        continue
                    item = item.model_copy(update={"well": row[symbol.group(1)]})
                expanded.append((item, instance, original, tuple(action.evidence_refs)))
    if len(expanded) > 500:
        add("DESIGNER_GRAPH_LIMIT", "actions", "Expanded plan exceeds 500 Designer actions.", "for_each")
    return expanded


def lower_actions_to_designer(
    raw_plan: ActionPlan | dict,
    *,
    setup: BravoLoweringSetup | dict,
    bravo_context: Mapping[str, Any],
    source_labware_catalog: Mapping[str, LabwareFacts],
    source_pipette_catalog: Mapping[str, PipetteFacts],
    source_spans: Mapping[str, SourceSpan],
    instruction: str,
    model_provenance: Mapping[str, str],
    paper: str | None = None,
    name: str = "Unreviewed action draft",
) -> LoweringResult:
    """Build a Designer graph without replacing any model volume, well, or order."""
    groups: dict[str, dict[str, Any]] = {}
    fatal = False

    def add(code: str, path: str, message: str, kind: str, *,
            stops_graph: bool = True, details: dict[str, Any] | None = None) -> None:
        nonlocal fatal
        fatal |= stops_graph
        group = groups.setdefault(code, {"code": code, "message": message, "occurrences": []})
        occurrence = {"path": path, "kind": kind}
        if details is not None:
            occurrence["details"] = details
        group["occurrences"].append(occurrence)

    def result(workflow: dict[str, Any] | None, plan_sha: str | None) -> LoweringResult:
        return LoweringResult(
            status="blocked" if groups else "unreviewed",
            workflow=workflow,
            blockers=tuple(groups.values()),
            review_requirements=_REVIEW,
            source_plan_sha256=plan_sha,
            bravo_context_hash=bravo_context.get("context_hash")
            if isinstance(bravo_context.get("context_hash"), str) else None,
        )

    try:
        plan = raw_plan if isinstance(raw_plan, ActionPlan) else ActionPlan.model_validate(raw_plan)
        selected = setup if isinstance(setup, BravoLoweringSetup) else BravoLoweringSetup.model_validate(setup)
        validate_action_evidence(plan, source_spans=source_spans,
                                 instruction=instruction, paper=paper)
    except (ValidationError, ActionPlanError, ValueError) as exc:
        add("ACTION_INPUT_UNVERIFIED", "actions", str(exc), "plan")
        return result(None, None)
    plan_sha = hashlib.sha256(json.dumps(plan.model_dump(mode="json"), sort_keys=True,
                                        ensure_ascii=False).encode("utf-8")).hexdigest()
    model_id = model_provenance.get("model") if isinstance(model_provenance, Mapping) else None
    source_id = model_provenance.get("source_id") if isinstance(model_provenance, Mapping) else None
    if (not isinstance(model_id, str) or not model_id.strip()
            or not isinstance(source_id, str) or not source_id.strip()):
        add("MODEL_PROVENANCE_UNVERIFIED", "model_provenance",
            "The caller must identify the model and source record for this action plan.", "setup")
    expanded = _expand(plan, add)
    labware_rows = _rows(bravo_context, "labware", "id")
    class_rows = _rows(bravo_context, "liquid_classes", "liquid_class_id")
    tip_choices = [item for item in bravo_context.get("tipbox_choices") or []
                   if isinstance(item, dict)]
    try:
        head_value = bravo_context.get("head_type")
        head = HeadType[head_value] if isinstance(head_value, str) else HeadType(head_value)
        if not head.is_disposable:
            raise ValueError("The active Bravo head is not disposable-tip pipetting hardware.")
        spec = selected.head_mode
        mode = normalize_head_mode(head, spec.get("subset_type"), spec.get("subset_config"),
                                   spec.get("row_count"), spec.get("column_count"))
        if (mode.subset_type != spec.get("subset_type") or mode.subset_config != spec.get("subset_config")
                or mode.num_channels != 1):
            raise ValueError("An exact single-barrel Bravo head mode is required for one-well actions.")
    except (KeyError, TypeError, ValueError) as exc:
        add("BRAVO_HEAD_MODE_UNSUPPORTED", "setup.head_mode", str(exc), "setup")
        mode = None
    if len(plan.pipettes) > 1:
        add("MULTIPLE_OT2_PIPETTES_UNSUPPORTED", "pipettes",
            "One Bravo head cannot preserve multiple OT-2 pipette identities.", "setup")
    for pipette in plan.pipettes:
        facts = source_pipette_catalog.get(pipette.model)
        if facts is None or facts.channels != 1:
            add("SOURCE_CHANNEL_MAPPING_UNCONFIRMED", f"pipettes[{pipette.id}]",
                "A trusted single-channel OT-2 pipette definition is required.", "setup")

    locations: dict[str, int] = {}
    rows_by_load: dict[str, dict] = {}
    deck: dict[str, list[DraftedDeckItem]] = {}
    used_slots: set[int] = set()
    for load in plan.labware:
        path = f"labware[{load.id}]"
        binding = selected.labware.get(load.id)
        source_facts = source_labware_catalog.get(load.load_name)
        if binding is None or source_facts is None:
            add("LABWARE_BINDING_UNCONFIRMED", path,
                "Both an explicit Bravo binding and trusted OT-2 definition are required.", "setup")
            continue
        if load.module_id is not None:
            add("OT2_MODULE_LABWARE_REMAP_REQUIRED", path,
                "Module-mounted OT-2 labware cannot be treated as an ordinary Bravo deck plate.", "setup")
        row = labware_rows.get(binding.labware_id)
        if row is None or row.get("provisional"):
            add("BRAVO_LABWARE_UNAVAILABLE", path,
                "The selected Bravo labware ID is absent or provisional.", "setup")
            continue
        tip_box = row.get("kind") == "tip_box" or row.get("base_class") == "tip_box"
        if tip_box != source_facts.is_tiprack:
            add("LABWARE_ROLE_MISMATCH", path,
                "The source and Bravo labware disagree on tip-rack versus liquid role.", "setup")
        compatible_heads = row.get("compatible_head_types") or []
        if compatible_heads and bravo_context.get("head_type") not in compatible_heads:
            add("BRAVO_LABWARE_HEAD_INCOMPATIBLE", path,
                "The bound labware does not list the active Bravo head as compatible.", "setup")
        if binding.slot in used_slots:
            add("BRAVO_DECK_COLLISION", path,
                "Two logical labware items use one Bravo position without a reviewed stack.", "setup")
        used_slots.add(binding.slot)
        if (not isinstance(row.get("rows"), int) or not isinstance(row.get("cols"), int)
                or row["rows"] <= 0 or row["cols"] <= 0
                or row.get("wells") != row["rows"] * row["cols"]
                or not _positive(row.get("spacing_x_mm"))
                or not _positive(row.get("spacing_y_mm"))):
            add("BRAVO_LABWARE_GEOMETRY_UNVERIFIED", path,
                "The active catalog has no complete well grid and pitch.", "setup")
            continue
        if any(not _positive(row.get(field)) for field in ("height_mm", "width_mm", "length_mm")):
            add("BRAVO_LABWARE_FOOTPRINT_UNVERIFIED", path,
                "The active catalog has no complete physical dimensions for this labware.", "setup")
        locations[load.id] = binding.slot
        rows_by_load[load.id] = row
        deck[str(binding.slot)] = [DraftedDeckItem(
            labware_id=binding.labware_id, name=str(row.get("name") or binding.labware_id),
            kind=str(row.get("kind") or "sbs_plate"),
            base_class=str(row.get("base_class") or ""), wells=int(row.get("wells") or 0),
            tip_definition_id=binding.tip_definition_id or "",
        )]
    for load_id in selected.labware.keys() - {item.id for item in plan.labware}:
        add("UNUSED_LABWARE_BINDING", f"setup.labware.{load_id}",
            "A Bravo labware binding has no model-authored load.", "setup")

    disposal = selected.disposal
    if any(isinstance(item[0], Drop) for item in expanded):
        if disposal is None:
            add("TIP_DISPOSAL_UNCONFIRMED", "setup.disposal",
                "Explicitly bind an active-catalog waste location for Tips Off.", "setup")
        else:
            waste = labware_rows.get(disposal.labware_id)
            if (waste is None or not any(
                    tag in {waste.get("kind"), waste.get("base_class")}
                    for tag in ("waste", "trash"))
                    or waste.get("provisional") or disposal.slot in used_slots
                    or not isinstance(waste.get("wells"), int) or waste["wells"] < 0
                    or any(not _positive(waste.get(field))
                           for field in ("height_mm", "width_mm", "length_mm"))):
                add("TIP_DISPOSAL_UNCONFIRMED", "setup.disposal",
                    "The selected Bravo waste location is unavailable or occupied.", "setup")
            else:
                used_slots.add(disposal.slot)
                deck[str(disposal.slot)] = [DraftedDeckItem(
                    labware_id=disposal.labware_id, name=str(waste.get("name") or disposal.labware_id),
                    kind=str(waste.get("kind") or "waste"),
                    base_class=str(waste.get("base_class") or "waste"),
                    wells=int(waste.get("wells") or 0),
                )]

    liquid_class = class_rows.get(selected.liquid_class_id)
    if any(isinstance(item[0], (Stroke, Mix)) for item in expanded):
        if liquid_class is None or not isinstance(liquid_class.get("name"), str) or not liquid_class["name"]:
            add("LIQUID_CLASS_UNAVAILABLE", "setup.liquid_class_id",
                "Select one exact liquid-class ID from the active Bravo catalog.", "setup")
        elif (liquid_class.get("head_type") != bravo_context.get("head_type")
              or liquid_class.get("machine_id") != bravo_context.get("machine_id")):
            add("LIQUID_CLASS_INCOMPATIBLE", "setup.liquid_class_id",
                "The liquid class is not calibrated for the active machine and head.", "setup")
        elif (liquid_class.get("execution_ready") is not True
              or liquid_class.get("status") not in {"reviewed", "qualified"}):
            add("LIQUID_CLASS_QUALIFICATION_UNVERIFIED", "setup.liquid_class_id",
                "An exact catalog class does not establish a reviewed, reagent-qualified method.",
                "setup", stops_graph=False)
    profile = bravo_context.get("profile")
    teachpoints = profile.get("teachpoints") if isinstance(profile, Mapping) else None
    for slot in sorted(used_slots):
        point = teachpoints.get(str(slot)) if isinstance(teachpoints, Mapping) else None
        if (not isinstance(point, Mapping)
                or any(not isinstance(point.get(axis), (int, float))
                       or isinstance(point.get(axis), bool)
                       or not math.isfinite(point[axis]) for axis in ("x", "y", "z"))):
            add("BRAVO_TEACHPOINT_UNVERIFIED", f"deck[{slot}]",
                "The active profile has no finite XYZ teachpoint for this selected deck position.",
                "setup")
    if deck:
        add("PHYSICAL_DECK_INVENTORY_UNCONFIRMED", "deck",
            "The active machine context contains catalog definitions, not verified physical labware, fresh tips, or liquid inventory.",
            "setup", stops_graph=False,
            details={"proposed_slots": {slot: stack[0].labware_id for slot, stack in deck.items()}})
    if fatal:
        return result(None, plan_sha)

    source_pipette = plan.pipettes[0] if plan.pipettes else None
    source_limits = (source_pipette_catalog[source_pipette.model] if source_pipette else None)
    maximum = bravo_context.get("head_max_volume_ul")
    if source_pipette and not _positive(maximum):
        add("BRAVO_VOLUME_LIMIT_UNCONFIRMED", "context.head_max_volume_ul",
            "The active head has no trusted maximum stroke volume.", "setup")
    if liquid_class is not None and not _positive(liquid_class.get("tip_capacity_ul")):
        add("LIQUID_CLASS_CAPACITY_UNCONFIRMED", "setup.liquid_class_id",
            "The selected liquid class has no trusted tip capacity.", "setup")
    if fatal:
        return result(None, plan_sha)

    nodes: list[DraftedNode] = []
    picked: dict[str, Any] | None = None
    held_ul = 0.0
    used_tip_cells: set[tuple[str, int, int]] = set()
    used_pickup_bindings: set[str] = set()

    def emit(node_type: str, properties: dict[str, Any], action: Any,
             path: str, original: str, parent_refs: tuple[str, ...]) -> None:
        refs = tuple(dict.fromkeys((*parent_refs, *action.evidence_refs)))
        first = refs[0]
        span = source_spans[first]
        corpus = instruction if span.source == "instruction" else paper or ""
        citation = SourceCitation(paragraph_id=first, excerpt=corpus[span.start:span.end][:500])
        properties["_action_provenance"] = {
            "source_plan_sha256": plan_sha, "action_path": path,
            "source_action_path": original, "evidence_refs": list(refs),
            "source_kind": "model_authored_ordered_action",
            "model": model_id, "source_id": source_id,
            "bravo_context_hash": bravo_context.get("context_hash"),
        }
        node_id = len(nodes) + 2
        properties["_protocol_step_id"] = f"unreviewed-node-{node_id}"
        properties["_protocol_path"] = f"/graph/nodes/{len(nodes) + 1}"
        nodes.append(DraftedNode(id=node_id, type=node_type,
                                 title=node_type.split("/")[-1], properties=properties,
                                 source_citation=citation))

    for action, path, original, parent_refs in expanded:
        if action.kind in _MODULE_KINDS:
            details = action.model_dump(exclude={"evidence_refs"}, mode="json")
            message = ("External OT-2 module operation requires operator review: "
                       + json.dumps(details, sort_keys=True, ensure_ascii=False))
            emit("system/Manual", {"message": message}, action, path, original, parent_refs)
            add("OT2_MODULE_OPERATION_UNSUPPORTED", path,
                "The active Bravo profile has no equivalent OT-2 module action; review the ordered manual handoff.",
                action.kind, stops_graph=False, details=details)
            continue
        if isinstance(action, RefillTips):
            emit("system/Manual", {"message": action.message}, action, path, original, parent_refs)
            add("OT2_TIP_REFILL_UNSUPPORTED", path,
                "Refilling an OT-2 rack does not establish fresh Bravo tip inventory.",
                action.kind, stops_graph=False, details={"message": action.message})
            continue
        if isinstance(action, Comment):
            emit("system/Manual", {"message": f"OT-2 comment for operator review: {action.message}"},
                 action, path, original, parent_refs)
            add("OT2_COMMENT_HAS_NO_BRAVO_LOG_NODE", path,
                "Bravo Manual pauses execution; review this ordered OT-2 comment before using the draft.",
                action.kind, stops_graph=False, details={"message": action.message})
            continue
        if isinstance(action, Pause):
            emit("system/Manual", {"message": action.message}, action, path, original, parent_refs)
            continue
        if isinstance(action, Delay):
            emit("system/Wait", {"duration_s": action.seconds}, action, path, original, parent_refs)
            continue
        if source_pipette is None or action.pipette != source_pipette.id:
            add("SOURCE_PIPETTE_UNAVAILABLE", path,
                "The action has no single trusted source pipette mapped to the active Bravo head.", action.kind)
            continue
        if isinstance(action, Pickup):
            selection = selected.pickup.get(path)
            if selection is None or selection.rack_id not in source_pipette.tip_rack_ids:
                add("TIP_PICKUP_BINDING_MISSING", path,
                    "Specify the source rack and exact fresh-tip anchor for each pickup.", action.kind)
                continue
            used_pickup_bindings.add(path)
            rack = selected.labware.get(selection.rack_id)
            rack_row = rows_by_load.get(selection.rack_id)
            if rack is None or rack_row is None:
                add("TIP_RACK_UNAVAILABLE", path, "The bound tip rack is unavailable.", action.kind)
                continue
            choices = [choice for choice in tip_choices
                       if choice.get("labware_id") == rack.labware_id
                       and choice.get("tip_definition_id") == rack.tip_definition_id
                       and choice.get("execution_ready") is True]
            if not choices or (choices[0].get("required_head_mode")
                               and choices[0]["required_head_mode"] != mode.subset_type):
                add("TIP_HEAD_PAIR_UNVERIFIED", path,
                    "The active catalog does not verify this tip box, tip ID, and head mode.", action.kind)
                continue
            if not _positive(choices[0].get("tip_capacity_ul")):
                add("TIP_CAPACITY_UNVERIFIED", path,
                    "The compatible tip pair has no trusted positive capacity.", action.kind)
                continue
            if (selection.tip_anchor_row >= rack_row["rows"]
                    or selection.tip_anchor_col >= rack_row["cols"]):
                add("TIP_ANCHOR_OUTSIDE_RACK", path,
                    "The selected tip cell is outside the verified rack grid.", action.kind)
                continue
            cell = (selection.rack_id, selection.tip_anchor_row, selection.tip_anchor_col)
            if picked is not None or cell in used_tip_cells:
                add("TIP_LIFECYCLE_UNSAFE", path,
                    "A pickup overlaps an attached tip or repeats a consumed tip cell.", action.kind)
                continue
            used_tip_cells.add(cell)
            picked = {"rack": selection.rack_id, "tip_id": rack.tip_definition_id,
                      "capacity": choices[0]["tip_capacity_ul"]}
            emit("tips/TipsOn", {"location": rack.slot, "head_mode": mode.to_dict(),
                                 "tip_anchor_row": selection.tip_anchor_row,
                                 "tip_anchor_col": selection.tip_anchor_col},
                 action, path, original, parent_refs)
            continue
        if isinstance(action, Drop):
            if picked is None or held_ul > 1e-9:
                add("TIP_LIFECYCLE_UNSAFE", path,
                    "Tips Off requires attached tips with no remaining aspirated liquid.", action.kind)
                continue
            assert disposal is not None
            emit("tips/TipsOff", {"location": disposal.slot}, action, path, original, parent_refs)
            picked = None
            continue
        if not isinstance(action, (Stroke, Mix)):
            add("UNSUPPORTED_ACTION", path, "This action has no Bravo primitive.", action.kind)
            continue
        if picked is None:
            add("TIP_LIFECYCLE_UNSAFE", path,
                "Liquid handling requires a prior explicit Tips On action.", action.kind)
            continue
        load = next((item for item in plan.labware if item.id == action.labware), None)
        row = rows_by_load.get(action.labware)
        source_facts = source_labware_catalog.get(load.load_name) if load else None
        if (row is None or source_facts is None or source_facts.is_tiprack
                or action.well not in source_facts.wells or not _well_in_grid(action.well, row)):
            add("WELL_MAPPING_UNVERIFIED", path,
                "The exact model well must exist in both source and bound Bravo labware.", action.kind)
            continue
        if (not _positive(row.get("well_depth_mm"))
                or selected.distance_from_bottom_mm >= row["well_depth_mm"]):
            add("PIPETTING_HEIGHT_UNVERIFIED", path,
                "The requested height is not inside the bound Bravo well depth.", action.kind)
            continue
        volume = action.volume_ul
        limit = min(float(maximum), float(picked["capacity"]),
                    float(liquid_class["tip_capacity_ul"]))
        if (not source_limits.min_volume_ul <= volume <= source_limits.max_volume_ul
                or volume > limit):
            add("STROKE_VOLUME_OUTSIDE_CATALOG", path,
                "The exact requested volume exceeds the source or active Bravo working range.", action.kind)
            continue
        if (liquid_class.get("tip_id") != picked["tip_id"]
                or (liquid_class.get("head_type") and liquid_class["head_type"] != head.name)):
            add("LIQUID_CLASS_TIP_MISMATCH", path,
                "The selected method is not calibrated for the loaded tip and head.", action.kind)
            continue
        props = {"location": locations[action.labware], "volume": volume,
                 "liquid_class": liquid_class["name"],
                 "distance_from_bottom": selected.distance_from_bottom_mm,
                 "anchor": action.well}
        if isinstance(action, Mix):
            if held_ul > 1e-9:
                add("MIX_WITH_HELD_LIQUID_UNSUPPORTED", path,
                    "Mixing with another liquid already held in the tip needs review.", action.kind)
                continue
            emit("liquid/Mix", {**props, "cycles": action.cycles},
                 action, path, original, parent_refs)
        elif action.kind == "aspirate":
            if held_ul + volume > limit:
                add("TIP_CAPACITY_EXCEEDED", path,
                    "The ordered aspirates exceed the active tip capacity.", action.kind)
                continue
            held_ul += volume
            emit("liquid/Aspirate", props, action, path, original, parent_refs)
        else:
            if volume > held_ul + 1e-9:
                add("DISPENSE_EXCEEDS_HELD_VOLUME", path,
                    "The ordered dispense exceeds the liquid held by the tip.", action.kind)
                continue
            held_ul -= volume
            emit("liquid/Dispense", props, action, path, original, parent_refs)
    for path in selected.pickup.keys() - used_pickup_bindings:
        add("UNUSED_PICKUP_BINDING", path,
            "A pickup selection has no matching model-authored action.", "setup")
    if picked is not None:
        add("TIP_LIFECYCLE_UNSAFE", "actions",
            "The ordered actions end with attached tips.", "plan")
    if fatal:
        return result(None, plan_sha)
    start = DraftedNode(id=1, type="flow/Start", title="Start")
    end = DraftedNode(id=len(nodes) + 2, type="flow/End", title="End")
    ordered = [start, *nodes, end]
    links = [DraftedLink(id=index, origin_id=ordered[index - 1].id, target_id=ordered[index].id,
                         origin_slot=0, target_slot=0)
             for index in range(1, len(ordered))]
    workflow = DraftedWorkflow(
        name=name[:120], description="Unreviewed lowering of model-authored ordered actions.",
        deck=deck, graph=DraftedGraph(nodes=ordered, links=links), library="",
    ).to_designer_json()
    workflow["protocol_generated_draft"] = True
    workflow["protocol_draft_status"] = "unreviewed"
    workflow["protocol_generated_provenance"] = {
        "source_kind": "model_authored_ordered_actions",
        "source_id": source_id,
        "model": model_id,
        "source_plan_sha256": plan_sha,
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "source_paper_sha256": hashlib.sha256(paper.encode("utf-8")).hexdigest() if paper else None,
        "bravo_context_hash": bravo_context.get("context_hash"),
    }
    from pybravo.workflow.storage import assert_safe_generated_draft

    assert_safe_generated_draft(workflow)
    return result(workflow, plan_sha)
