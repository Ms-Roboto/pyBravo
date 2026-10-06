"""Deterministic protocol checks against authoritative machine/catalog data.

No hardware is contacted here. The validator accounts for every addressed well,
physical tip footprint and deck move in execution order. Only an accepted plan
has node specifications available for the compiler.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from pybravo.head_mode import (
    TipSelection,
    head_geometry_for_type,
    legal_tipbox_anchors,
    normalize_head_mode,
    plate_footprint_wells,
    selected_tip_wells,
    tipbox_selection,
)
from pybravo.types import HeadType

from .dead_volume import catalog_dead_volume, scientist_confirmed_dead_volume
from .models import ProtocolPlan, ProtocolSetup, ProtocolStep
from .preview import GUIDED_SETUP_QUESTION_PATHS
from .tipbox_choices import catalog_tip_ids, rack_tip_stride, selected_tip_id

MAX_OPERATIONS = 1000
MAX_NESTING = 8
_EPS = 1e-8
_UNIT_FACTORS = {"uL": 1.0, "mL": 1000.0, "nL": .001, "s": 1.0,
                 "min": 60.0, "h": 3600.0, "count": 1.0}
_UNIT_PATTERNS = {"uL": r"(?:[uµμ]l|microlit(?:er|re)s?)", "mL": r"(?:ml|millilit(?:er|re)s?)",
                  "nL": r"(?:nl|nanolit(?:er|re)s?)", "s": r"(?:s|sec(?:ond)?s?)",
                  "min": r"(?:min(?:ute)?s?)", "h": r"(?:h|hrs?|hours?)"}


def well_name(cell: tuple[int, int]) -> str:
    row, col = cell
    label = ""
    row += 1
    while row:
        row, remainder = divmod(row - 1, 26)
        label = chr(65 + remainder) + label
    return f"{label}{col + 1}"


def well_cell(name: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", name)
    if not match:
        raise ValueError("Use an explicit uppercase well anchor such as A1.")
    row = 0
    for char in match[1]:
        row = row * 26 + ord(char) - 64
    return row - 1, int(match[2]) - 1


def _positive(value: Any, *, zero: bool = False) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and (value >= 0 if zero else value > 0)


def _commanded_volume(liquid_class: dict | None, volume_ul: float) -> float | None:
    """Use the executor's calibration calculation before choosing a split stroke."""
    if liquid_class is None:
        return None
    from pybravo.state_machine.tasks import _evaluate_volume_polynomial, _interpolate_control_points

    try:
        equation = dict(liquid_class.get("equation") or {})
        points = list(equation.get("control_points") or [])
        commanded = (_interpolate_control_points(points, volume_ul) if points else
                     _evaluate_volume_polynomial(list(equation.get("coefficients") or [0.0, 1.0]), volume_ul))
        return commanded if math.isfinite(commanded) and commanded > 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _volume_command_matches(actual: float, expected: float) -> bool:
    # Differences below 0.05 uL or 1% are below this planning guard's resolution.
    return math.isclose(actual, expected, rel_tol=0.01, abs_tol=0.05)


def _calibration_supports(liquid_class: dict | None, volume_ul: float) -> bool:
    if liquid_class is None:
        return False
    points = ((liquid_class.get("equation") or {}).get("control_points") or [])
    if not points:
        return True
    try:
        bounds = [float(point["desired_ul"]) for point in points]
    except (KeyError, TypeError, ValueError):
        return False
    return bool(bounds) and min(bounds) - _EPS <= volume_ul <= max(bounds) + _EPS


def _is_tip_box(definition: dict) -> bool:
    """Imported vendor racks often use an SBS kind with a tip-box base class."""
    return "tip_box" in {definition.get("kind"), definition.get("base_class")}


def _catalog(context: dict, key: str) -> dict[str, dict]:
    rows = context.get(key) or []
    if isinstance(rows, dict):
        return {str(k): v for k, v in rows.items() if isinstance(v, dict)}
    catalog = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = row.get("id") or row.get("tip_id") or row.get("liquid_class_id") or row.get("name")
        catalog[str(identity)] = row
        if key == "liquid_classes" and row.get("name"):
            catalog[str(row["name"])] = row
    return catalog


def _sources(sources: Any) -> dict[str, dict]:
    if hasattr(sources, "model_dump"):
        sources = sources.model_dump()
    if isinstance(sources, dict) and "paragraphs" in sources:
        sources = sources["paragraphs"]
    if isinstance(sources, dict):
        return {str(k): ({"text": v} if isinstance(v, str) else v) for k, v in sources.items()}
    return {str(p.get("paragraph_id") or p.get("id")): p for p in (sources or []) if isinstance(p, dict)}


def _pointer(document: Any, path: str) -> Any:
    try:
        for key in path.strip("/").split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            document = document[int(key)] if isinstance(document, list) else document[key]
        return document
    except (KeyError, IndexError, TypeError, ValueError):
        return None


@dataclass
class PreparedProtocol:
    plan: ProtocolPlan | None = None
    setup: ProtocolSetup | None = None
    report: dict = field(default_factory=lambda: {"ok": False, "valid": False, "issues": [], "questions": [], "summary": {}, "run_sheet": {}})
    operations: list[dict] = field(default_factory=list)
    deck: dict = field(default_factory=dict)

    def issue(self, code: str, path: str, message: str, question: str | None = None, *, severity: str = "error") -> None:
        entry = {"severity": severity, "code": code, "path": path, "message": message}
        if question:
            entry["question"] = question
            candidate = {"id": f"{code}:{path}", "path": path, "prompt": question}
            if candidate not in self.report["questions"]:
                self.report["questions"].append(candidate)
        if entry not in self.report["issues"]:
            self.report["issues"].append(entry)


def _numeric_grounding(result: PreparedProtocol, step: ProtocolStep, path: str, paragraphs: dict, decisions: dict) -> None:
    # A source citation is membership-checked independently of numerical support.
    for paragraph_id in step.source_paragraph_ids:
        if paragraph_id not in paragraphs:
            result.issue("unknown_source", path + "/source_paragraph_ids", f"Unknown source paragraph {paragraph_id!r}.")
    if not step.source_paragraph_ids and not any(d.startswith(path + "/") for d in decisions):
        result.issue("missing_source", path + "/source_paragraph_ids", "This step has no source citation or scientist decision.", "Which source paragraph supports this step?")
    required = {"transfer": ["volume_ul"], "mix": ["volume_ul", "cycles"], "wait": ["duration_s"]}.get(step.kind, [])
    if step.repeat != 1:
        required = required + ["repeat"]
    if step.kind == "manual" and step.duration_s is not None:
        required = required + ["duration_s"]
    evidence_by_field: dict[str, list] = {}
    for evidence in step.source_values:
        evidence_by_field.setdefault(evidence.field, []).append(evidence)
        ep = f"{path}/source_values"
        if evidence.paragraph_id not in step.source_paragraph_ids or evidence.paragraph_id not in paragraphs:
            result.issue("unknown_evidence_source", ep, "Numerical evidence must refer to one of this step's existing source paragraphs.")
            continue
        expected_units = {"volume_ul": {"uL", "mL", "nL"}, "duration_s": {"s", "min", "h"}, "cycles": {"count"}, "repeat": {"count"}}
        if evidence.unit not in expected_units[evidence.field]:
            result.issue("evidence_unit", ep, f"{evidence.field} cannot use {evidence.unit} evidence.")
            continue
        text = str(paragraphs[evidence.paragraph_id].get("text", ""))
        # Parse rather than substring match: '5' must never substantiate '50'.
        suffix = (r"\s*(?:times|cycles?|repetitions?)\b" if evidence.unit == "count"
                  else r"\s*" + _UNIT_PATTERNS[evidence.unit] + r"\b")
        matches = re.finditer(r"(?<![\w.])([+-]?(?:\d+(?:\.\d*)?|\.\d+))" + suffix, text, re.I)
        if not any(math.isclose(float(m[1]), evidence.value, rel_tol=1e-9, abs_tol=_EPS) for m in matches):
            result.issue("ungrounded_source_value", ep, f"Source paragraph does not contain {evidence.value:g} {evidence.unit}.")
    for field_name in required:
        value = getattr(step, field_name)
        if value is None:
            continue
        decision = decisions.get(f"{path}/{field_name}")
        if decision is not None and not isinstance(decision.value, bool) and decision.value == value:
            continue
        evidence = evidence_by_field.get(field_name, [])
        if not any(math.isclose(e.value * _UNIT_FACTORS[e.unit], float(value), rel_tol=1e-9, abs_tol=_EPS) for e in evidence):
            result.issue("ungrounded_parameter", f"{path}/{field_name}", f"{field_name}={value} needs matching source evidence or a recorded scientist decision.", f"Confirm {field_name} and record its source or reason.")
    if step.kind == "distribute":
        for index, target in enumerate(step.dispenses):
            target_path = f"{path}/dispenses/{index}/volume_ul"
            decision = decisions.get(target_path)
            if decision is not None and not isinstance(decision.value, bool) and decision.value == target.volume_ul:
                continue
            evidence = evidence_by_field.get("volume_ul", [])
            if not any(math.isclose(e.value * _UNIT_FACTORS[e.unit], target.volume_ul, rel_tol=1e-9, abs_tol=_EPS)
                       for e in evidence):
                result.issue("ungrounded_parameter", target_path,
                             f"Dispense volume {target.volume_ul:g} uL needs matching source evidence or a recorded scientist decision.",
                             "Confirm this dispense volume and record its source or reason.")


def prepare_protocol(plan: ProtocolPlan | dict, setup: ProtocolSetup | dict, context: dict, *, sources: Any = None) -> PreparedProtocol:
    result = PreparedProtocol()
    for name, value, model in (("plan", plan, ProtocolPlan), ("setup", setup, ProtocolSetup)):
        try:
            parsed = value if isinstance(value, model) else model.model_validate(value)
            setattr(result, name, parsed)
        except ValidationError as error:
            for issue in error.errors(include_url=False):
                prefix = "/setup" if name == "setup" else ""
                path = prefix + "/" + "/".join(str(x) for x in issue["loc"])
                result.issue("schema", path, issue["msg"])
    if result.plan is None or result.setup is None:
        return result
    plan, setup = result.plan, result.setup
    paragraphs = _sources(sources)
    decisions = {d.path: d for d in plan.decisions}
    doc = {**plan.model_dump(), "setup": setup.model_dump()}
    for question in plan.questions:
        if question.path in GUIDED_SETUP_QUESTION_PATHS:
            # The head, rack order, and legacy liquid class have their own
            # structured validator checks. A stale extraction prompt must not
            # add a second, free-text blocker after those fields are resolved.
            continue
        if question.id.startswith("catalog-tipbox:"):
            match = re.fullmatch(r"/materials/(\d+)/labware_id", question.path)
            material = plan.materials[int(match[1])] if match and int(match[1]) < len(plan.materials) else None
            if material is None or material.role != "tips" or material.id != question.id.removeprefix("catalog-tipbox:"):
                result.issue("tipbox_confirmation", question.path, "The catalog tipbox recommendation no longer identifies its original tip material.")
                continue
            pair = (material.labware_id, material.tip_definition_id)
            choices = context.get("tipbox_choices") or []
            if not any((choice.get("labware_id"), choice.get("tip_definition_id")) == pair
                       for choice in choices if isinstance(choice, dict)):
                result.issue("tipbox_confirmation", question.path, "The recommended rack and tip are no longer a verified choice for the active head.")
                continue
            tip_path = question.path.removesuffix("/labware_id") + "/tip_definition_id"
            if (decisions.get(question.path) is None or decisions[question.path].value != pair[0]
                    or decisions.get(tip_path) is None or decisions[tip_path].value != pair[1]):
                result.issue("tipbox_confirmation", question.path,
                             "Confirm the recommended rack and tip together before validation.", question.prompt)
            continue
        answer = _pointer(doc, question.path)
        if answer is None or answer == "" or answer == []:
            if question.path not in decisions:
                result.issue("unresolved_question", question.path, question.prompt, question.prompt)
    limit = min(MAX_OPERATIONS, max(1, int(context.get("max_operations") or MAX_OPERATIONS)))
    expanded: list[tuple[ProtocolStep, str]] = []
    seen_steps: set[str] = set()

    def expand(steps: list[ProtocolStep], prefix: str, depth: int = 0) -> list[tuple[ProtocolStep, str]]:
        if depth > MAX_NESTING:
            result.issue("nesting_limit", prefix, f"Repeat nesting exceeds {MAX_NESTING} levels.")
            return []
        out: list[tuple[ProtocolStep, str]] = []
        for index, step in enumerate(steps):
            path = f"{prefix}/{index}"
            parameter_fields = {"source", "destination", "material", "source_anchor", "destination_anchor", "anchor",
                                "volume_ul", "cycles", "duration_s", "destination_slot", "message",
                                "reagent_id", "reagent_family"}
            allowed_fields = {
                "transfer": {"source", "destination", "source_anchor", "destination_anchor", "volume_ul",
                             "reagent_id", "reagent_family"},
                "distribute": {"source", "source_anchor", "reagent_id", "reagent_family"},
                "mix": {"material", "anchor", "volume_ul", "cycles", "reagent_id", "reagent_family"},
                "wait": {"duration_s"}, "manual": {"message", "duration_s"},
                "move_plate": {"material", "destination_slot"},
                "stack_plate": {"material", "destination_slot"},
                "destack_plate": {"material", "destination_slot"},
                "repeat": set(),
            }[step.kind]
            for unused in parameter_fields - allowed_fields:
                if getattr(step, unused) is not None:
                    result.issue("unexpected_parameter", path + "/" + unused, f"{unused} does not apply to {step.kind}; it would otherwise be silently ignored.")
            if step.kind != "distribute" and step.dispenses:
                result.issue("unexpected_parameter", path + "/dispenses", "Only distribute may specify dispense targets.")
            if step.kind not in {"transfer", "distribute", "mix"} and step.method_ref is not None:
                result.issue("unexpected_parameter", path + "/method_ref", "A pipetting method applies only to liquid steps.")
            if step.id in seen_steps:
                result.issue("duplicate_step_id", path + "/id", f"Duplicate step id {step.id!r}.")
            seen_steps.add(step.id)
            _numeric_grounding(result, step, path, paragraphs, decisions)
            if step.repeat is None or not 1 <= step.repeat <= limit:
                result.issue("repeat_count", path + "/repeat", f"Repeat count must be between 1 and {limit}.", "How many times should this step run?")
                continue
            if step.kind == "repeat":
                if not step.steps:
                    result.issue("empty_repeat", path + "/steps", "A repeat block must contain steps.")
                body = expand(step.steps, path + "/steps", depth + 1)
            else:
                if step.steps:
                    result.issue("unexpected_children", path + "/steps", "Only repeat blocks may contain child steps.")
                body = [(step, path)]
            if len(out) + len(body) * step.repeat > limit:
                result.issue("operation_limit", path, f"Expanded protocol exceeds {limit} steps.")
                return out
            out.extend(body * step.repeat)
        return out

    expanded = expand(plan.steps, "/steps")
    if not expanded:
        result.issue("empty_protocol", "/steps", "A protocol must contain at least one step.")
    liquid_steps = [s for s, _ in expanded if s.kind in {"transfer", "distribute", "mix"}]
    aspirated_material_ids = {
        step.material if step.kind == "mix" else step.source
        for step in liquid_steps
    }
    labware = _catalog(context, "labware")
    tips = _catalog(context, "tip_definitions")
    materials: dict[str, Any] = {}
    definitions: dict[str, dict] = {}
    locations: dict[str, int] = {}
    # Each deck slot holds material IDs in bottom-to-top order.
    occupancy: dict[int, list[str]] = {}
    slot_materials: dict[int, list[tuple[int, Any]]] = {}
    volumes: dict[str, dict[tuple[int, int], float]] = {}
    known_volume_cells: dict[str, set[tuple[int, int]]] = {}
    fresh_tips: dict[str, set[tuple[int, int]]] = {}
    unconfirmed_tip_inventory: set[str] = set()
    discarded_tips: dict[str, set[tuple[int, int]]] = {}
    deck_entries: dict[str, dict] = {}
    for i, material in enumerate(plan.materials):
        path = f"/materials/{i}"
        if material.id in materials:
            result.issue("duplicate_material_id", path + "/id", f"Duplicate material id {material.id!r}.")
        materials[material.id] = material
        definition = labware.get(material.labware_id)
        if definition is None:
            result.issue("unknown_labware", path + "/labware_id", "Choose labware from the active catalog.", f"Which catalog labware holds {material.name}?")
            continue
        definitions[material.id] = definition
        if definition.get("provisional"):
            result.issue("provisional_labware", path + "/labware_id",
                         "Provisional labware is for visual review only and cannot be used in an executable protocol.",
                         f"Which verified catalog labware replaces {material.name}?")
        slot = material.deck_slot
        if slot is None or not 1 <= slot <= 9:
            result.issue("deck_slot", path + "/deck_slot", "Deck slot must be an integer from 1 to 9.", f"Which deck slot holds {material.name}?")
        else:
            locations[material.id] = slot
            slot_materials.setdefault(slot, []).append((i, material))
            deck_entries[material.id] = {"labware_id": material.labware_id, "name": material.name,
                "kind": definition.get("kind", ""), "base_class": definition.get("base_class", ""),
                "wells": definition.get("wells", 0), "is_lidded": False, "is_sealed": False,
                "tip_definition_id": selected_tip_id(material.tip_definition_id, definition) or ""}
            if _is_tip_box(definition):
                if material.role == "waste" or material.available_tips is not None:
                    deck_entries[material.id].update(
                        tipbox_fill_state="empty" if material.role == "waste" or material.available_tips == [] else "full",
                    )
                    if isinstance(material.available_tips, list):
                        deck_entries[material.id]["available_tips"] = material.available_tips
                else:
                    deck_entries[material.id]["tip_inventory_status"] = "unconfirmed"
        rows, cols = int(definition.get("rows") or 0), int(definition.get("cols") or 0)
        if material.role == "liquid":
            if rows <= 0 or cols <= 0 or not _positive(definition.get("well_volume_ul")):
                result.issue("catalog_geometry", path + "/labware_id", "Liquid labware needs catalog rows, columns and positive well capacity.")
                continue
            for field_name in ("initial_volume_ul", "dead_volume_ul"):
                # Dead volume constrains aspiration. A receive-only plate has
                # no such use, so an absent value is not a scientific question
                # or a reason to invent zero. If it later becomes a source or
                # mix vessel, the requirement applies automatically.
                if (field_name == "dead_volume_ul" and material.id not in aspirated_material_ids
                        and material.dead_volume_ul is None):
                    continue
                if not _positive(getattr(material, field_name), zero=True):
                    result.issue("missing_volume", f"{path}/{field_name}", f"{field_name} must be explicitly supplied and nonnegative.", f"What is the per-well {field_name} for {material.name} (uL)?")
            catalog_dead = catalog_dead_volume(definition)
            if (material.id in aspirated_material_ids and _positive(material.dead_volume_ul, zero=True)
                    and catalog_dead is not None
                    and (catalog_dead[1] == "placeholder" or material.dead_volume_ul != catalog_dead[0])
                    and not scientist_confirmed_dead_volume(
                        plan.decisions, i, material.id, material.labware_id, material.dead_volume_ul
                    )):
                if catalog_dead[1] == "placeholder":
                    result.issue(
                        "unreviewed_catalog_dead_volume", path + "/dead_volume_ul",
                        "The selected plate has only an unreviewed catalog dead-volume placeholder. Confirm this source estimate for the plate before validation; review liquid, tip, and method suitability separately.",
                        f"Confirm {material.dead_volume_ul:g} uL as the dead volume for {material.name}, or enter a measured value.",
                    )
                else:
                    result.issue(
                        "catalog_dead_volume_override", path + "/dead_volume_ul",
                        "This source dead volume differs from the reviewed catalog starting estimate. Confirm the value for its plate before validation; review liquid, tip, and method suitability separately.",
                        f"Confirm {material.dead_volume_ul:g} uL as the dead volume for {material.name}.",
                    )
            initial_is_known = _positive(material.initial_volume_ul, zero=True)
            # Zero is only a lower bound when the starting volume is missing.
            # It must not produce a per-well shortage finding in that case.
            initial = material.initial_volume_ul if initial_is_known else 0.0
            volumes[material.id] = {(r, c): initial for r in range(rows) for c in range(cols)}
            known_volume_cells[material.id] = set(volumes[material.id]) if initial_is_known else set()
            for anchor, value in material.well_volumes_ul.items():
                try:
                    cell = well_cell(anchor)
                    if cell not in volumes[material.id] or not _positive(value, zero=True):
                        raise ValueError("Well override must name an existing well and a nonnegative volume.")
                    volumes[material.id][cell] = value
                    known_volume_cells[material.id].add(cell)
                except ValueError as error:
                    result.issue("well_volume", path + "/well_volumes_ul", str(error))
            if any(v > definition["well_volume_ul"] + _EPS for v in volumes[material.id].values()):
                result.issue("initial_capacity", path + "/initial_volume_ul", "Starting volume exceeds the catalog well capacity.")
            if material.dead_volume_ul is not None and material.dead_volume_ul > definition["well_volume_ul"]:
                result.issue("dead_volume_capacity", path + "/dead_volume_ul", "Dead volume exceeds the catalog well capacity.")
        elif material.role == "tips":
            if not _is_tip_box(definition) or rows <= 0 or cols <= 0 or rows * cols != definition.get("wells"):
                result.issue("tip_labware", path + "/labware_id", "Tip supplies must use a catalog tip box with valid geometry.")
            allowed_cells = {(r, c) for r in range(rows) for c in range(cols)}
            cells: set[tuple[int, int]] = set()
            if material.available_tips is None:
                unconfirmed_tip_inventory.add(material.id)
                result.issue("tip_inventory_unconfirmed", path + "/available_tips",
                             "A scientist must confirm the actual fresh-tip wells before this protocol can be compiled.",
                             f"Which fresh tip wells are present in {material.name}?")
            elif material.available_tips == "full":
                cells = allowed_cells
            else:
                chosen: set[tuple[int, int]] = set()
                for anchor in material.available_tips:
                    try:
                        cell = well_cell(anchor)
                        if cell not in allowed_cells or cell in chosen:
                            raise ValueError("Tip inventory must name distinct existing wells.")
                        chosen.add(cell)
                    except ValueError as error:
                        result.issue("tip_inventory", path + "/available_tips", str(error))
                cells = chosen
            fresh_tips[material.id] = cells
        elif _is_tip_box(definition):
            if material.available_tips != []:
                result.issue("disposal_not_empty", path + "/available_tips", "A tip box used for disposal must be explicitly empty (available_tips=[]).")
            discarded_tips[material.id] = set()
        elif not any(x in str(definition.get("kind", "")) + " " + str(definition.get("base_class", "")) for x in ("trash", "waste")):
            result.issue("waste_labware", path + "/labware_id", "Tip disposal requires a catalog waste receptacle or an explicitly empty tip box.")
    for slot, members in slot_materials.items():
        if len(members) == 1:
            index, material = members[0]
            if material.stack_order not in (None, 0):
                result.issue("stack_order", f"/materials/{index}/stack_order", "A single plate must have stack_order 0 or omit it.")
            ordered = members
        else:
            orders = [material.stack_order for _, material in members]
            if sorted(order for order in orders if order is not None and order >= 0) != list(range(len(members))) or len(set(orders)) != len(members):
                if all(order is None for order in orders):
                    result.issue("deck_collision", f"/materials/{members[1][0]}/deck_slot",
                                 f"Deck slot {slot} has multiple materials without an explicit stack order.")
                for index, _ in members:
                    result.issue("stack_order", f"/materials/{index}/stack_order", f"Slot {slot} needs unique stack_order values 0 through {len(members) - 1}, bottom to top.")
                ordered = members
            else:
                ordered = sorted(members, key=lambda pair: pair[1].stack_order)
            for index, material in members:
                definition = definitions[material.id]
                if material.role != "liquid" or _is_tip_box(definition):
                    result.issue("stack_labware", f"/materials/{index}/deck_slot", "Only liquid plates may share a deck slot as a stack.")
                if material.labware_id != members[0][1].labware_id:
                    result.issue("stack_labware", f"/materials/{index}/labware_id", "A reviewed stack must use the same catalog plate type throughout.")
                if not _positive(definition.get("height_mm")) or not _positive(definition.get("stack_height_mm")):
                    result.issue("stack_geometry", f"/materials/{index}/labware_id", "Stacked plates need catalog height and stacking height before execution.")
        occupancy[slot] = [material.id for _, material in ordered]
        result.deck[str(slot)] = [deck_entries[material.id] for _, material in ordered]
    mode, head_type, liquid_class = None, None, None
    legacy_liquid_steps = any(step.method_ref is None for step in liquid_steps)
    if liquid_steps:
        try:
            ht = context.get("head_type")
            head_type = HeadType[ht] if isinstance(ht, str) else HeadType(ht)
            if not head_type.is_disposable:
                raise ValueError("Only a configured disposable-tip pipetting head is supported by this protocol compiler.")
        except (ValueError, KeyError, TypeError) as error:
            result.issue("unsupported_head", "/setup/head_mode", str(error))
        if setup.head_mode is None:
            result.issue("head_mode", "/setup/head_mode", "Select the active pipetting channels.", "Which head channels should the protocol use?")
        elif head_type is not None:
            spec = setup.head_mode
            geometry = head_geometry_for_type(head_type)
            required_counts = {"row": ["row_count"], "column": ["column_count"], "rectangle": ["row_count", "column_count"]}.get(spec.subset_type, [])
            valid_mode = True
            for field_name in required_counts:
                count = getattr(spec, field_name)
                upper = geometry.rows if field_name == "row_count" else geometry.columns
                if count is None or not 1 <= count <= upper:
                    valid_mode = False
                    result.issue("head_count", f"/setup/head_mode/{field_name}", f"Specify {field_name} between 1 and {upper}.")
            if valid_mode:
                mode = normalize_head_mode(head_type, spec.subset_type, spec.subset_config, spec.row_count, spec.column_count)
                if mode.subset_type != spec.subset_type or mode.subset_config != spec.subset_config:
                    result.issue("head_mode_normalization", "/setup/head_mode", "This head mode is not supported exactly as specified by the configured head.")
                for field_name in ("row_count", "column_count"):
                    count = getattr(spec, field_name)
                    if count is not None and count != getattr(mode, field_name):
                        result.issue("head_count", f"/setup/head_mode/{field_name}", "The specified channel count does not match this head mode.")
        if setup.tip_strategy is None:
            result.issue("tip_strategy", "/setup/tip_strategy", "Select an explicit tip strategy.", "Use fresh tips for each step or each source, or intentionally reuse them?")
        if setup.tip_strategy in {"reuse_all", "fresh_each_source"} and not (setup.tip_reuse_reason or "").strip():
            result.issue("tip_reuse_reason", "/setup/tip_reuse_reason", "Tip reuse requires a scientist's contamination assessment.", "Why is reusing these tips acceptable for this procedure?")
        liquid_classes = _catalog(context, "liquid_classes")
        liquid_class = liquid_classes.get(setup.liquid_class)
        if legacy_liquid_steps and (not setup.liquid_class or setup.liquid_class not in liquid_classes):
            result.issue("liquid_class", "/setup/liquid_class", "Choose an approved liquid class from the active catalog.", "Which approved liquid class should be used?")
        if legacy_liquid_steps and not _positive(setup.distance_from_bottom_mm, zero=True):
            result.issue("pipetting_height", "/setup/distance_from_bottom_mm", "Specify a nonnegative pipetting height above the well bottom.", "What approved distance above the well bottom should be used (mm)?")
        if not setup.tip_rack_ids:
            result.issue("tip_supply", "/setup/tip_rack_ids", "Select at least one tip supply material.", "Which tip racks should the workflow consume?")
        if len(set(setup.tip_rack_ids)) != len(setup.tip_rack_ids):
            result.issue("duplicate_tip_supply", "/setup/tip_rack_ids", "Tip rack IDs must be distinct.")
        for material_id in setup.tip_rack_ids:
            if material_id not in fresh_tips:
                result.issue("tip_supply", "/setup/tip_rack_ids", f"{material_id!r} is not a valid tip supply.")
        if setup.tip_disposal_id == "return_to_source_rack":
            if setup.tip_strategy != "fresh_each_source":
                result.issue("tip_disposal", "/setup/tip_disposal_id", "Returning used tips to their own supply racks requires fresh_each_source, so spent tips cannot be reused for another source.")
        else:
            disposal = materials.get(setup.tip_disposal_id)
            if disposal is None or disposal.role != "waste" or disposal.id not in locations:
                result.issue("tip_disposal", "/setup/tip_disposal_id", "Select a configured waste material or return_to_source_rack for used tips.", "Where should used tips be discarded?")
    tips_used = 0
    loaded_tip: dict | None = None
    returned_tips: dict[str, set[tuple[int, int]]] = {}
    rack_owner: dict[str, str] = {}
    consumed: dict[str, float] = {}
    run_steps: list[dict] = []
    returned_supply_racks = (
        [material_id for material_id in setup.tip_rack_ids if material_id in fresh_tips and material_id in locations]
        if setup.tip_disposal_id == "return_to_source_rack" and liquid_steps else []
    )

    def operator_checkpoint(phase: str, message: str) -> None:
        result.operations.append({"type": "system/Manual", "properties": {"message": message},
                                  "step_id": f"tip-rack-{phase}", "path": "/setup/tip_rack_ids",
                                  "description": "Fresh-tip check" if phase == "preflight" else "Spent-tip removal",
                                  "source_paragraph_ids": []})
        run_steps.append({"id": f"tip-rack-{phase}", "kind": "manual", "message": message,
                          "source_paragraph_ids": []})

    if returned_supply_racks:
        rack_labels = ", ".join(f"{material_id} (slot {locations[material_id]})" for material_id in returned_supply_racks)
        operator_checkpoint("preflight", "Before every run, inspect fresh tip racks " + rack_labels +
                            ". Confirm the recorded fresh-tip inventory and that none contains tips returned from a prior run.")

    def rack_selections(material_id: str, occupied: set[tuple[int, int]], purpose: str) -> list[TipSelection]:
        definition = definitions[material_id]
        rows, cols = int(definition.get("rows") or 0), int(definition.get("cols") or 0)
        stride = rack_tip_stride(head_type, definition)
        issue_path = "/setup/tip_rack_ids" if purpose == "pickup" else "/setup/tip_disposal_id"
        if stride is None:
            result.issue("tip_pitch" if purpose == "pickup" else "disposal_pitch", issue_path,
                         "Tip rack grid and pitch must support the configured head.")
            return []
        if stride == 2:
            if mode.subset_type != "all_barrels" or (mode.row_count, mode.column_count) != (8, 12):
                result.issue("interleaved_head_mode", "/setup/head_mode", "A 96ST head uses a 384 tip rack only with all 96 barrels and alternate-well addressing.")
                return []
            selections = [TipSelection(location=locations[material_id], row=row, col=col, row_count=8,
                                       column_count=12, row_stride=2, col_stride=2)
                          for row, col in ((0, 0), (0, 1), (1, 0), (1, 1))]
            return [selection for selection in selections
                    if (set(selected_tip_wells(rows, cols, selection)) <= occupied if purpose == "pickup"
                        else not set(selected_tip_wells(rows, cols, selection)) & occupied)]
        return [tipbox_selection(locations[material_id], anchor.row, anchor.col, mode)
                for anchor in legal_tipbox_anchors(rows, cols, mode, occupied, purpose=purpose)]

    def tip_properties(selection: TipSelection) -> dict:
        properties = {"tip_anchor_row": selection.row, "tip_anchor_col": selection.col}
        if selection.row_stride != 1 or selection.col_stride != 1:
            properties.update(row_stride=selection.row_stride, col_stride=selection.col_stride)
        return properties

    def add(node_type: str, properties: dict, step: ProtocolStep, path: str) -> None:
        result.operations.append({"type": node_type, "properties": properties, "step_id": step.id, "path": path,
                                  "description": step.description, "source_paragraph_ids": list(step.source_paragraph_ids)})

    def pick_tips(step: ProtocolStep, path: str, source_id: str | None) -> None:
        nonlocal loaded_tip, tips_used
        if loaded_tip is not None or mode is None or head_type is None:
            return
        for material_id in setup.tip_rack_ids:
            if setup.tip_strategy == "fresh_each_source" and material_id in rack_owner and rack_owner[material_id] != source_id:
                continue
            definition = definitions.get(material_id)
            if definition is None or material_id not in locations or material_id not in fresh_tips:
                continue
            declared_heads = definition.get("compatible_head_types") or []
            if declared_heads and head_type.name not in declared_heads:
                result.issue("tip_rack_head_compatibility", "/setup/tip_rack_ids",
                             f"Tip rack {material_id!r} is not approved for {head_type.name}.")
                continue
            rows, cols = int(definition.get("rows") or 0), int(definition.get("cols") or 0)
            selections = rack_selections(material_id, fresh_tips[material_id], "pickup")
            if not selections:
                continue
            material = materials[material_id]
            tip_id = selected_tip_id(material.tip_definition_id, definition)
            tip = tips.get(tip_id)
            if tip is None:
                material_path = f"/materials/{plan.materials.index(material)}/tip_definition_id"
                result.issue("tip_definition", material_path, f"Tip rack {material_id!r} needs an explicitly selected catalog tip definition.",
                             f"Which tip definition is loaded in {material.name}?")
                continue
            head_memberships = [tip.get(key) for key in ("compatible_heads", "supported_head_types") if tip.get(key)]
            if not head_memberships or any(head_type.name not in membership for membership in head_memberships):
                result.issue("tip_head_compatibility", "/setup/tip_rack_ids", f"Tip {tip_id!r} has no explicit compatibility with {head_type.name}.")
                continue
            if tip_id not in catalog_tip_ids(definition):
                result.issue("tip_rack_compatibility", "/setup/tip_rack_ids", f"Tip {tip_id!r} is not explicitly linked to rack {material_id!r}.")
                continue
            if tip.get("kind", "tip") != "tip":
                result.issue("tip_kind", "/setup/tip_rack_ids", "Disposable-tip protocols require a pipette tip definition.")
                continue
            if not _positive(tip.get("length_mm")):
                result.issue("tip_length", f"/materials/{plan.materials.index(material)}/tip_definition_id",
                             f"Selected tip {tip_id!r} needs a finite positive catalog length before execution.")
                continue
            capacity = tip.get("capacity_ul")
            if not _positive(capacity):
                result.issue("tip_capacity", "/setup/tip_rack_ids", "Tip capacity must be present and positive in the catalog.")
                continue
            if _positive(tip.get("overflow_ul")):
                capacity = min(capacity, tip["overflow_ul"])
            if _positive(context.get("head_max_volume_ul")):
                capacity = min(capacity, context["head_max_volume_ul"])
            if liquid_class and step.method_ref is None:
                class_tip = liquid_class.get("tip_id")
                class_capacity = liquid_class.get("tip_capacity_ul")
                if class_tip and class_tip != tip_id or (
                    not class_tip and _positive(class_capacity) and not math.isclose(class_capacity, tip["capacity_ul"])
                ):
                    result.issue("liquid_class_tip", "/setup/liquid_class", f"The liquid class is not calibrated for tip {tip_id!r}.")
                if liquid_class.get("head_type") and liquid_class["head_type"] != head_type.name:
                    result.issue("liquid_class_head", "/setup/liquid_class", "The liquid class is not calibrated for this head.")
                if liquid_class.get("machine_id") and liquid_class["machine_id"] != context.get("machine_id"):
                    result.issue("liquid_class_machine", "/setup/liquid_class", "The liquid class is not calibrated for this instrument.")
            selection = selections[0]
            cells = selected_tip_wells(rows, cols, selection)
            fresh_tips[material_id].difference_update(cells)
            tips_used += len(cells)
            loaded_tip = {"capacity_ul": capacity, "rack": material_id, "tip_id": tip_id,
                          "selection": selection, "cells": set(cells), "source": source_id}
            if setup.tip_strategy == "fresh_each_source" and source_id is not None:
                rack_owner[material_id] = source_id
            add("tips/TipsOn", {"location": locations[material_id], "head_mode": mode.to_dict(), **tip_properties(selection)}, step, path)
            return
        # An unconfirmed rack may still contain a legal footprint. Its explicit
        # inventory blocker is the actionable finding until the inventory is known.
        if not unconfirmed_tip_inventory.intersection(setup.tip_rack_ids):
            result.issue("tip_inventory_exhausted", path, "No compatible legal tip footprint remains in the selected supplies.")

    def discard(step: ProtocolStep, path: str) -> None:
        nonlocal loaded_tip
        if loaded_tip is None:
            return
        material_id = loaded_tip["rack"] if setup.tip_disposal_id == "return_to_source_rack" else setup.tip_disposal_id
        if material_id not in locations or material_id not in definitions:
            loaded_tip = None
            return
        properties = {"location": locations[material_id]}
        if setup.tip_disposal_id == "return_to_source_rack":
            # A full-head pickup empties its original positions. Return only
            # to those exact positions; fresh_tips stays consumed permanently.
            cells = loaded_tip["cells"]
            already_returned = returned_tips.setdefault(material_id, set())
            if cells & (already_returned | fresh_tips[material_id]):
                result.issue("disposal_capacity", path, "The original rack wells are not empty for this used-tip return.")
            already_returned.update(cells)
            properties.update(tip_properties(loaded_tip["selection"]))
        elif material_id in discarded_tips and mode is not None:
            definition = definitions[material_id]
            tip_id = loaded_tip["tip_id"]
            explicit = materials[material_id].tip_definition_id
            assigned = deck_entries[material_id]
            if tip_id not in catalog_tip_ids(definition) or (explicit and explicit != tip_id):
                result.issue("disposal_tip_compatibility", "/setup/tip_disposal_id", f"Disposal rack does not support the selected tip {tip_id!r}.")
            elif discarded_tips[material_id] and assigned.get("tip_definition_id") != tip_id:
                result.issue("disposal_tip_type", "/setup/tip_disposal_id", "Different tip definitions cannot share one tracked return rack; select a waste receptacle instead.")
            else:
                # This rack starts empty; bind its inventory to the actual tip
                # being returned, without replacing the scientific selection.
                assigned["tip_definition_id"] = tip_id
            rows, cols = int(definition.get("rows") or 0), int(definition.get("cols") or 0)
            selections = rack_selections(material_id, discarded_tips[material_id], "return")
            if not selections:
                result.issue("disposal_capacity", path, "The used-tip receptacle has no legal remaining footprint.")
            else:
                selection = selections[0]
                properties.update(tip_properties(selection))
                cells = selected_tip_wells(rows, cols, selection)
                discarded_tips[material_id].update(cells)
        add("tips/TipsOff", properties, step, path)
        loaded_tip = None

    methods_used: dict[tuple[str, str], dict[str, str]] = {}

    def resolve_method(step: ProtocolStep, path: str, source_id: str | None,
                       destination_ids: list[str]) -> dict | None:
        """Resolve a pinned, technically complete method; report adaptations separately."""
        if step.method_ref is None:
            return None
        from .methods import get_method

        method_path = path + "/method_ref"
        try:
            method = get_method(context, step.method_ref.method_id, step.method_ref.revision)
        except (OSError, ValueError) as error:
            result.issue("method_registry", method_path, f"Method registry cannot be read: {error}")
            return None
        if method is None:
            result.issue("method_reference", method_path,
                         "Pinned method is missing or its revision changed; select and review a current method.")
            return None
        if not method.get("execution_ready") or method.get("status") not in {"reviewed", "qualified"}:
            missing = ", ".join(method.get("missing_fields") or []) or "scientist review"
            result.issue("method_incomplete", method_path,
                         f"Method {method['method_id']!r} cannot compile until its record is complete: {missing}.")
            return None
        applies = method.get("applicability") or {}
        tip_id = loaded_tip["tip_id"] if loaded_tip is not None else None
        for applicability_field, current in (("machine_ids", context.get("machine_id")),
                                             ("head_types", head_type.name if head_type else None),
                                             ("tip_ids", tip_id)):
            if current not in (applies.get(applicability_field) or []):
                result.issue("method_incompatible", method_path,
                             f"Method {method['method_id']!r} does not support {applicability_field}={current!r}.")
        expected_strategy = {"fresh_per_transfer": "fresh_each_step", "fresh_per_source": "fresh_each_source",
                             "reuse_within_source": "fresh_each_source"}.get(method.get("tip_policy"))
        if expected_strategy and setup.tip_strategy != expected_strategy:
            result.issue("method_tip_policy", method_path,
                         f"Method requires {expected_strategy}, but setup selects {setup.tip_strategy!r}.")
        if step.kind == "distribute" and method.get("tip_policy") == "fresh_per_transfer":
            result.issue("method_tip_policy", method_path,
                         "A distribute step reuses tips across destinations; this method requires fresh tips per transfer.")

        source = materials.get(source_id)
        rack = definitions.get(loaded_tip["rack"]) if loaded_tip is not None else None
        actual = {
            "tipbox_ids": rack.get("id") if rack else None,
            "source_labware_ids": source.labware_id if source else None,
            "reagent_families": step.reagent_family if step.reagent_family is not None else
                                source.reagent_family if source else None,
            "operations": step.kind,
        }
        differences = []
        for applicability_field, value in actual.items():
            accepted = applies.get(applicability_field) or []
            if value not in accepted:
                differences.append({"field": applicability_field, "actual": value, "method": accepted})
        for destination_id in destination_ids:
            target = materials.get(destination_id)
            value = target.labware_id if target else None
            accepted = applies.get("destination_labware_ids") or []
            if value not in accepted:
                differences.append({"field": "destination_labware_ids", "destination": destination_id,
                                    "actual": value, "method": accepted})
        resolved_classes = {}
        for phase_name in ("aspirate", "dispense"):
            phase = method.get(phase_name) or {}
            ref = phase.get("liquid_class_ref") or {}
            selected = liquid_classes.get(ref.get("id"))
            if selected is None:
                result.issue("method_liquid_class", method_path,
                             f"Method {phase_name} liquid class is absent from the active catalog.")
                continue
            if tip_id and selected.get("tip_id") and selected["tip_id"] != tip_id:
                result.issue("method_liquid_class", method_path,
                             f"Method {phase_name} liquid class is not calibrated for tip {tip_id!r}.")
            resolved_classes[phase_name] = selected
        audit = {"method_id": method["method_id"], "version": method["version"],
                 "revision": method["revision"], "digest": method["revision"], "status": method["status"]}
        methods_used[(method["method_id"], method["revision"])] = audit
        return {**method, "_classes": resolved_classes, "_audit": audit, "_differences": differences}

    def footprint(material_id: str | None, anchor: str | None, field_name: str, step: ProtocolStep, path: str,
                  *, height_mm: float | None = None) -> list[tuple[int, int]]:
        material = materials.get(material_id)
        definition = definitions.get(material_id)
        if material is None or material.role != "liquid" or definition is None or material_id not in locations:
            result.issue("liquid_material", path + "/" + field_name, "Choose a configured liquid material.", f"Which material is used as {field_name}?")
            return []
        slot = locations[material_id]
        if not occupancy.get(slot) or occupancy[slot][-1] != material_id:
            result.issue("buried_plate", path + "/" + field_name,
                         f"{material.name} is not the top plate at slot {slot}; destack the plates above it first.")
            return []
        # Agilent's Bravo compatibility guidance excludes ST70 tips from
        # 1536-well microplates. The rack may support both ST10 and ST70, so
        # the selected tip identity, not the box name, determines this check.
        if definition.get("wells") == 1536 and loaded_tip is not None and loaded_tip["tip_id"] == "st_70ul":
            result.issue("tip_plate_compatibility", path + "/" + field_name,
                         "Agilent ST70 tips are not compatible with 1536-well plates; select an approved smaller ST tip.")
            return []
        anchor_path = path + "/" + ({"source": "source_anchor", "destination": "destination_anchor"}.get(field_name, "anchor"))
        try:
            cell = well_cell(anchor or "")
        except ValueError as error:
            result.issue("well_anchor", anchor_path, str(error), "Which explicit starting well should the active head address?")
            return []
        if mode is None or head_type is None:
            return []
        cells = plate_footprint_wells(head_type, mode, int(definition.get("rows") or 0), int(definition.get("cols") or 0), float(definition.get("spacing_x_mm") or 0), float(definition.get("spacing_y_mm") or 0), *cell)
        if not cells:
            result.issue("unreachable_wells", anchor_path, "The active head footprint cannot reach these wells at this labware pitch.")
        depth = definition.get("well_depth_mm")
        height = setup.distance_from_bottom_mm if height_mm is None else height_mm
        if _positive(depth) and height is not None and height >= depth:
            height_path = path + "/method_ref" if step.method_ref else "/setup/distance_from_bottom_mm"
            result.issue("pipetting_height", height_path, f"Pipetting height reaches or exceeds the well depth of {material.name}.")
        return cells

    for step, path in expanded:
        details = {"id": step.id, "kind": step.kind, "description": step.description, "source_paragraph_ids": step.source_paragraph_ids}
        run_steps.append(details)
        if step.kind in {"transfer", "distribute", "mix"}:
            if step.kind == "distribute":
                if not step.dispenses:
                    result.issue("distribute_targets", path + "/dispenses", "A distribute step needs at least one destination.", "Which destination wells receive this aspiration?")
                    continue
                bad_targets = [index for index, target in enumerate(step.dispenses) if not _positive(target.volume_ul)]
                for index in bad_targets:
                    result.issue("liquid_volume", f"{path}/dispenses/{index}/volume_ul", "Dispense volume must be finite and positive.")
                if bad_targets:
                    continue
                aspiration_ul = sum(target.volume_ul for target in step.dispenses)
            else:
                aspiration_ul = step.volume_ul
            if not _positive(aspiration_ul):
                result.issue("liquid_volume", path + "/volume_ul", "Pipetting volume must be finite and positive.", "What volume should be pipetted per channel (uL)?")
                continue
            source_id = step.source if step.kind in {"transfer", "distribute"} else step.material
            source_material = materials.get(source_id)
            details["reagent_id"] = (step.reagent_id if step.reagent_id is not None else
                                     source_material.reagent_id if source_material else None)
            details["reagent_family"] = (step.reagent_family if step.reagent_family is not None else
                                         source_material.reagent_family if source_material else None)
            if setup.tip_strategy == "fresh_each_source" and loaded_tip is not None and loaded_tip["source"] != source_id:
                discard(step, path)
            pick_tips(step, path, source_id)
            if loaded_tip is not None:
                details["tip_rack_id"] = loaded_tip["rack"]
            destination_ids = ([step.destination] if step.kind == "transfer" else
                               [target.destination for target in step.dispenses] if step.kind == "distribute" else [])
            method = resolve_method(step, path, source_id, destination_ids)
            if step.method_ref is not None and method is None:
                continue
            if method is not None:
                asp = method["aspirate"]
                dsp = method["dispense"]
                if step.kind == "distribute" and dsp["blowout_ul"] > 0:
                    result.issue("method_distribute_unsupported", path + "/method_ref",
                                 "Empty-tip distribute does not execute a reviewed blowout setting; use a compatible method or transfer intent.")
                if step.kind == "distribute" and (asp["pre_air_ul"] > 0 or asp["post_air_ul"] > 0):
                    result.issue("method_distribute_air_gap", path + "/method_ref",
                                 "The final empty-tip dispense would expel method air gaps into a destination.")
                if step.kind == "mix" and (asp["pre_air_ul"] > 0 or asp["post_air_ul"] > 0):
                    result.issue("method_mix_unsupported", path + "/method_ref",
                                 "Mix does not implement the method's separate pre/post aspiration air gaps.")
                if step.kind == "mix" and asp["dynamic_tip_extension"] > 0:
                    result.issue("method_mix_unsupported", path + "/method_ref",
                                 "Mix scales dynamic tip extension by volume; this method records a fixed-distance extension.")
                classes = method["_classes"]
                if len(classes) != 2:
                    continue
                audit = method["_audit"]
                details["method_ref"] = audit
                aspirate_class = classes["aspirate"]
                dispense_class = classes["dispense"]
                if any(air_ul > 0 and not _calibration_supports(aspirate_class, air_ul)
                       for air_ul in (asp["pre_air_ul"], asp["post_air_ul"])):
                    result.issue("method_calibration_limit", path + "/method_ref",
                                 "A method air gap is outside the aspirate class calibration range.")
                aspirate_props = {"volume": aspiration_ul,
                    "liquid_class": aspirate_class.get("name") or aspirate_class.get("liquid_class_id") or aspirate_class.get("id"),
                    "distance_from_bottom": asp["distance_from_bottom_mm"],
                    "pre_aspirate_volume": asp["pre_air_ul"], "post_aspirate_volume": asp["post_air_ul"],
                    "dynamic_tip_extension": asp["dynamic_tip_extension"], "tip_touch": asp["tip_touch"],
                    "_method_ref": audit}
                dispense_props = {"volume": aspiration_ul,
                    "liquid_class": dispense_class.get("name") or dispense_class.get("liquid_class_id") or dispense_class.get("id"),
                    "distance_from_bottom": dsp["distance_from_bottom_mm"],
                    "blowout_volume": dsp["blowout_ul"],
                    "dynamic_tip_retraction": dsp["dynamic_tip_retraction"], "tip_touch": dsp["tip_touch"],
                    "_method_ref": audit}
            else:
                legacy_props = {"volume": aspiration_ul,
                    "liquid_class": (liquid_class or {}).get("name") or setup.liquid_class,
                    "distance_from_bottom": setup.distance_from_bottom_mm,
                    "pre_aspirate_volume": 0, "post_aspirate_volume": 0,
                    "blowout_volume": 0, "tip_touch": False}
                aspirate_props = legacy_props
                dispense_props = legacy_props
                aspirate_class = liquid_class
                dispense_class = liquid_class
            distribute_strategy = "single_aspiration"
            if step.kind == "distribute" and len(step.dispenses) > 1:
                total_commanded = _commanded_volume(aspirate_class, aspiration_ul)
                dispense_commands = [_commanded_volume(dispense_class, target.volume_ul)
                                     for target in step.dispenses]
                if total_commanded is None or any(command is None for command in dispense_commands):
                    result.issue("distribute_calibration", path + "/dispenses",
                                 "The selected liquid-class correction cannot be checked for multi-dispense.")
                else:
                    fallback_reasons = []
                    if not _volume_command_matches(total_commanded, sum(dispense_commands)):
                        fallback_reasons.append("split_calibration")
                    if loaded_tip is not None:
                        if aspiration_ul > loaded_tip["capacity_ul"] + _EPS:
                            fallback_reasons.append("nominal_tip_capacity")
                        air_ul = (asp["pre_air_ul"] + asp["post_air_ul"]) if method is not None else 0.0
                        if total_commanded + air_ul > loaded_tip["capacity_ul"] + _EPS:
                            fallback_reasons.append("commanded_tip_capacity")
                    if fallback_reasons:
                        distribute_strategy = "paired_fallback"
                        warning = {"severity": "warning", "code": "distribute_calibration_fallback",
                            "path": path + "/dispenses",
                            "message": "Separate aspiration-dispense pairs are required by calibration or tip capacity.",
                            "reasons": fallback_reasons,
                            "commanded_total_ul": total_commanded,
                            "commanded_dispenses_ul": dispense_commands}
                        if warning not in result.report["issues"]:
                            result.report["issues"].append(warning)
                        for index, target in enumerate(step.dispenses):
                            aspiration_command = _commanded_volume(aspirate_class, target.volume_ul)
                            if aspiration_command is None or not math.isclose(
                                    aspiration_command, dispense_commands[index], rel_tol=1e-6, abs_tol=1e-6):
                                result.issue("distribute_calibration", f"{path}/dispenses/{index}",
                                             "A paired aspiration and dispense would leave residual commanded volume in the tip.")
                aspiration_sequence = ([aspiration_ul] if distribute_strategy == "single_aspiration"
                                       else [target.volume_ul for target in step.dispenses])
                if (any(not _calibration_supports(aspirate_class, volume) for volume in aspiration_sequence)
                        or any(not _calibration_supports(dispense_class, target.volume_ul)
                               for target in step.dispenses)):
                    result.issue("distribute_calibration", path + "/dispenses",
                                 "An aspiration or dispense is outside its liquid-class calibration points.")
            elif method is not None and (
                    not _calibration_supports(aspirate_class, aspiration_ul)
                    or not _calibration_supports(dispense_class, aspiration_ul)):
                result.issue("method_calibration_limit", path + "/method_ref",
                             "The requested liquid volume is outside an aspirate or dispense class calibration range.")
            aspiration_strokes = ([aspiration_ul] if step.kind != "distribute"
                                  or distribute_strategy == "single_aspiration"
                                  else [target.volume_ul for target in step.dispenses])
            if method is not None:
                applies = method["applicability"]
                low, high = applies.get("min_volume_ul"), applies.get("max_volume_ul")
                differences = list(method["_differences"])
                dispense_strokes = ([target.volume_ul for target in step.dispenses]
                                    if step.kind == "distribute" else [aspiration_ul])
                for phase, strokes in (("aspirate", aspiration_strokes), ("dispense", dispense_strokes)):
                    for index, stroke_ul in enumerate(strokes):
                        if (low is not None and stroke_ul < low - _EPS) or (high is not None and stroke_ul > high + _EPS):
                            differences.append({"field": "volume_ul", "phase": phase, "target_index": index,
                                                "actual": stroke_ul,
                                                "method": {"min_volume_ul": low, "max_volume_ul": high}})
                details["method_differences"] = differences
                if differences:
                    warning = {"severity": "warning", "code": "method_mismatch", "path": path + "/method_ref",
                        "message": "Selected method differs from its recorded application; scientist acknowledgement is required.",
                        "differences": differences}
                    if warning not in result.report["issues"]:
                        result.report["issues"].append(warning)
            if loaded_tip is not None and aspirate_class is not None:
                pre_air = asp["pre_air_ul"] if method is not None else 0.0
                post_air = asp["post_air_ul"] if method is not None else 0.0
                air_commands = [(_commanded_volume(aspirate_class, air_ul) if air_ul > 0 else 0.0)
                                for air_ul in (pre_air, post_air)]
                for stroke_ul in aspiration_strokes:
                    if stroke_ul > loaded_tip["capacity_ul"] + _EPS:
                        result.issue("tip_volume_exceeded", path + "/dispenses" if step.kind == "distribute"
                                     else path + "/volume_ul",
                                     f"An aspiration exceeds the selected tip/head capacity of {loaded_tip['capacity_ul']:g} uL.")
                        break
                    liquid_command = _commanded_volume(aspirate_class, stroke_ul)
                    if liquid_command is None or any(command is None for command in air_commands):
                        result.issue("calibration_command", path + "/method_ref" if method else path,
                                     "The aspiration or air-gap correction cannot command a positive volume.")
                        break
                    commanded_total = liquid_command + sum(air_commands)
                    if commanded_total > loaded_tip["capacity_ul"] + _EPS:
                        result.issue("tip_volume_exceeded", path + "/method_ref" if method else path,
                                     f"The calibrated aspiration and air gaps command {commanded_total:g} uL, "
                                     f"above the selected tip/head capacity of {loaded_tip['capacity_ul']:g} uL.")
                        break
            if method is not None and dsp["blowout_ul"] > 0:
                if step.kind == "mix":
                    result.issue("method_blowout_unavailable", path + "/method_ref",
                                 "Mix does not aspirate the extra volume needed for the reviewed blowout.")
                elif step.kind == "transfer":
                    aspirated_commands = [_commanded_volume(aspirate_class, aspiration_ul)]
                    aspirated_commands.extend(_commanded_volume(aspirate_class, air_ul)
                                              for air_ul in (asp["pre_air_ul"], asp["post_air_ul"]) if air_ul > 0)
                    needed_command = _commanded_volume(dispense_class, aspiration_ul + dsp["blowout_ul"])
                    if (not _calibration_supports(dispense_class, aspiration_ul + dsp["blowout_ul"])
                            or needed_command is None or any(command is None for command in aspirated_commands)
                            or needed_command > sum(aspirated_commands) + _EPS):
                        result.issue("method_blowout_unavailable", path + "/method_ref",
                                     "The reviewed blowout would be clipped because the aspiration did not command enough volume.")
            source_height = aspirate_props["distance_from_bottom"]
            destination_height = dispense_props["distance_from_bottom"]
            if step.kind == "transfer":
                from_cells = footprint(step.source, step.source_anchor, "source", step, path, height_mm=source_height)
                to_cells = footprint(step.destination, step.destination_anchor, "destination", step, path, height_mm=destination_height)
                if from_cells and to_cells:
                    if step.source == step.destination and set(from_cells).intersection(to_cells):
                        result.issue("overlapping_transfer", path, "A transfer cannot overlap its own source footprint; use an explicit mix step.")
                    for cell in from_cells:
                        available = volumes[step.source][cell]
                        dead = materials[step.source].dead_volume_ul
                        if (cell in known_volume_cells[step.source] and _positive(dead, zero=True)
                                and available - step.volume_ul < dead - _EPS):
                            result.issue("insufficient_reagent", path + "/volume_ul",
                                         f"{step.source}:{well_name(cell)} would fall below its {dead:g} uL dead volume.")
                        volumes[step.source][cell] -= step.volume_ul
                    for cell in to_cells:
                        volumes[step.destination][cell] += step.volume_ul
                        if volumes[step.destination][cell] > definitions[step.destination]["well_volume_ul"] + _EPS:
                            result.issue("destination_capacity", path + "/volume_ul", f"{step.destination}:{well_name(cell)} exceeds well capacity.")
                    consumed[step.source] = consumed.get(step.source, 0) + step.volume_ul * len(from_cells)
                    details.update(volume_ul=step.volume_ul, source=step.source, destination=step.destination,
                                   source_wells=[well_name(c) for c in from_cells], destination_wells=[well_name(c) for c in to_cells])
                    add("liquid/Aspirate", {**aspirate_props, "location": locations[step.source], "anchor": step.source_anchor}, step, path)
                    add("liquid/Dispense", {**dispense_props, "location": locations[step.destination], "anchor": step.destination_anchor}, step, path)
            elif step.kind == "distribute":
                from_cells = footprint(step.source, step.source_anchor, "source", step, path, height_mm=source_height)
                targets: list[tuple[Any, list[tuple[int, int]], str]] = []
                seen_targets: set[tuple[str, str]] = set()
                seen_destination_cells: dict[str, set[tuple[int, int]]] = {}
                for index, target in enumerate(step.dispenses):
                    target_path = f"{path}/dispenses/{index}"
                    key = (target.destination, target.destination_anchor)
                    if key in seen_targets:
                        result.issue("duplicate_dispense", target_path, "A distribute step cannot address the same destination footprint twice.")
                    seen_targets.add(key)
                    cells = footprint(target.destination, target.destination_anchor, "destination", step, target_path,
                                      height_mm=destination_height)
                    if cells:
                        earlier = seen_destination_cells.setdefault(target.destination, set())
                        if earlier.intersection(cells):
                            result.issue("overlapping_dispense", target_path,
                                         "Distribute targets overlap wells already addressed in this step.")
                        earlier.update(cells)
                    if from_cells and cells and len(from_cells) != len(cells):
                        result.issue("footprint_mismatch", target_path,
                                     "Source and destination footprints must address the same number of channels.")
                    if step.source == target.destination and set(from_cells).intersection(cells):
                        result.issue("overlapping_transfer", target_path,
                                     "A distribute destination cannot overlap its source footprint.")
                    targets.append((target, cells, target_path))
                if from_cells and targets and all(cells and len(cells) == len(from_cells) for _, cells, _ in targets):
                    if distribute_strategy == "paired_fallback" and any(
                            volumes[target.destination][cell] > _EPS for target, cells, _ in targets for cell in cells):
                        result.issue("distribute_calibration", path + "/dispenses",
                                     "The paired fallback would return tips from nonempty destination wells to the source.")
                    dead = materials[step.source].dead_volume_ul
                    for cell in from_cells:
                        if (cell in known_volume_cells[step.source] and _positive(dead, zero=True)
                                and volumes[step.source][cell] - aspiration_ul < dead - _EPS):
                            result.issue("insufficient_reagent", path + "/dispenses",
                                         f"{step.source}:{well_name(cell)} would fall below its {dead:g} uL dead volume.")
                        volumes[step.source][cell] -= aspiration_ul
                    consumed[step.source] = consumed.get(step.source, 0) + aspiration_ul * len(from_cells)
                    details.update(source=step.source, source_wells=[well_name(c) for c in from_cells],
                                   aspirate_volume_ul=aspiration_ul, strategy=distribute_strategy,
                                   aspiration_sequence_ul=([aspiration_ul] if distribute_strategy == "single_aspiration"
                                                            else [target.volume_ul for target, _, _ in targets]),
                                   dispenses=[])
                    if distribute_strategy == "single_aspiration":
                        add("liquid/Aspirate", {**aspirate_props, "location": locations[step.source],
                                                "anchor": step.source_anchor}, step, path)
                    for index, (target, cells, target_path) in enumerate(targets):
                        if distribute_strategy == "paired_fallback":
                            add("liquid/Aspirate", {**aspirate_props, "volume": target.volume_ul,
                                                    "location": locations[step.source],
                                                    "anchor": step.source_anchor}, step, target_path)
                        for cell in cells:
                            volumes[target.destination][cell] += target.volume_ul
                            if volumes[target.destination][cell] > definitions[target.destination]["well_volume_ul"] + _EPS:
                                result.issue("destination_capacity", target_path + "/volume_ul",
                                             f"{target.destination}:{well_name(cell)} exceeds well capacity.")
                        details["dispenses"].append({"destination": target.destination,
                            "destination_anchor": target.destination_anchor, "volume_ul": target.volume_ul,
                            "destination_wells": [well_name(cell) for cell in cells]})
                        add("liquid/Dispense", {**dispense_props, "volume": target.volume_ul,
                            "location": locations[target.destination], "anchor": target.destination_anchor,
                            "empty_tips": index == len(targets) - 1}, step, target_path)
            else:
                cells = footprint(step.material, step.anchor, "material", step, path, height_mm=source_height)
                if method is not None:
                    asp_class = classes["aspirate"].get("liquid_class_id") or classes["aspirate"].get("id")
                    dsp_class = classes["dispense"].get("liquid_class_id") or classes["dispense"].get("id")
                    if (asp_class != dsp_class or source_height != destination_height
                            or asp["tip_touch"] != dsp["tip_touch"] or dsp["dynamic_tip_retraction"] != 0):
                        result.issue("method_mix_unsupported", path + "/method_ref",
                                     "Mix methods must use one class, height and tip-touch setting, with no dispense retraction.")
                if step.cycles is None or not 1 <= step.cycles <= limit:
                    result.issue("mix_cycles", path + "/cycles", f"Mix cycles must be between 1 and {limit}.", "How many mixing cycles should run?")
                if cells:
                    dead = materials[step.material].dead_volume_ul
                    if (_positive(dead, zero=True) and any(
                            cell in known_volume_cells[step.material]
                            and volumes[step.material][cell] - step.volume_ul < dead - _EPS for cell in cells)):
                        result.issue("mix_available_volume", path + "/volume_ul", "Mix volume exceeds available liquid above dead volume in at least one addressed well.")
                    details.update(volume_ul=step.volume_ul, cycles=step.cycles, material=step.material, wells=[well_name(c) for c in cells])
                    mix_props = {**aspirate_props, "blowout_volume": dispense_props.get("blowout_volume", 0)}
                    add("liquid/Mix", {**mix_props, "location": locations[step.material], "anchor": step.anchor, "cycles": step.cycles}, step, path)
            if setup.tip_strategy == "fresh_each_step":
                discard(step, path)
        elif step.kind in {"manual", "wait"}:
            # A physical handoff cannot implicitly retain loaded consumables.
            discard(step, path)
            if step.kind == "wait":
                if not _positive(step.duration_s):
                    result.issue("wait_duration", path + "/duration_s", "Wait duration must be finite and positive.", "How many seconds should this wait last?")
                else:
                    add("system/Wait", {"duration_s": step.duration_s}, step, path)
                    details["duration_s"] = step.duration_s
            else:
                if not (step.message or "").strip():
                    result.issue("manual_instruction", path + "/message", "A manual handoff needs explicit operator instructions.", "What must the scientist do before resuming?")
                else:
                    props = {"message": step.message}
                    if step.duration_s is not None:
                        if not _positive(step.duration_s):
                            result.issue("manual_duration", path + "/duration_s", "Manual duration must be finite and positive when supplied.")
                        else:
                            props["duration_s"] = step.duration_s
                    add("system/Manual", props, step, path)
                    details["message"] = step.message
        elif step.kind in {"move_plate", "stack_plate", "destack_plate"}:
            discard(step, path)
            if context.get("has_gripper") is not True:
                result.issue("gripper_unavailable", path, "This instrument has no configured gripper; use a manual handoff instead.")
            if step.material not in locations:
                result.issue("move_material", path + "/material", "Choose a material on the deck to move.")
                continue
            destination = step.destination_slot
            if destination is None or not 1 <= destination <= 9:
                result.issue("move_destination", path + "/destination_slot", "Specify a deck destination between 1 and 9.")
                continue
            source = locations[step.material]
            if source == destination:
                result.issue("move_collision", path + "/destination_slot", "Source and destination slots must differ.")
                continue
            if not occupancy.get(source) or occupancy[source][-1] != step.material:
                result.issue("buried_plate", path + "/material", "Only the top plate of a stack may be moved.")
                continue
            definition = definitions.get(step.material)
            if definition is None or (step.kind != "move_plate" and (materials[step.material].role != "liquid" or _is_tip_box(definition))):
                result.issue("move_material", path + "/material", "Stack and Destack require a configured liquid plate.")
                continue
            destination_stack = occupancy.get(destination, [])
            if step.kind == "stack_plate":
                if not destination_stack:
                    result.issue("stack_destination", path + "/destination_slot", "Stack Plate requires an existing plate at the destination; use Move Plate for the first plate.")
                    continue
                if any(definitions[mid].get("id") != definition.get("id") for mid in destination_stack):
                    result.issue("stack_labware", path + "/destination_slot", "The destination stack must contain the same catalog plate type.")
                    continue
            elif destination_stack:
                result.issue("move_collision", path + "/destination_slot", f"Deck slot {destination} is occupied.")
                continue
            if step.kind == "move_plate" and len(occupancy[source]) != 1:
                result.issue("move_stack", path + "/material", "Move Plate requires a single source plate; use Destack Plate for a source stack.")
                continue
            if step.kind == "destack_plate" and len(occupancy[source]) < 2:
                result.issue("destack_source", path + "/material", "Destack Plate requires at least two source plates; use Move Plate for a single plate.")
                continue
            occupancy[source].pop()
            if not occupancy[source]:
                del occupancy[source]
            occupancy.setdefault(destination, []).append(step.material)
            locations[step.material] = destination
            node_type, properties = {
                "move_plate": ("plate/PickPlace", {"pick_location": source, "place_location": destination}),
                "stack_plate": ("plate/Stack", {"source_location": source, "base_location": destination}),
                "destack_plate": ("plate/Destack", {"source_location": source, "destination_location": destination}),
            }[step.kind]
            add(node_type, properties, step, path)
            details.update(material=step.material, source_slot=source, destination_slot=destination)
    if expanded:
        discard(*expanded[-1])
    if returned_tips:
        spent_labels = ", ".join(f"{material_id} (slot {locations[material_id]})" for material_id in returned_supply_racks)
        operator_checkpoint("postflight", "These racks now contain returned spent tips: " + spent_labels +
                            ". Remove and label them spent; install fresh racks before any later run.")
    # Mix cycles expand to two physical operations each; enforce a workload bound
    # in addition to the serialized graph bound to reject huge nested requests.
    work = sum((2 * int(o["properties"].get("cycles") or 1)) if o["type"] == "liquid/Mix" else 1 for o in result.operations)
    if len(result.operations) + 2 > limit or work > limit:
        result.issue("operation_limit", "/steps", f"Compiled workload exceeds the {limit}-operation limit.")
    result.report["summary"] = {"expanded_steps": len(expanded), "compiled_operations": len(result.operations), "physical_operations": work,
        "tips_required": tips_used, "channels": mode.num_channels if mode else 0, "reagent_consumption_ul": consumed,
        "methods_used": list(methods_used.values()),
        "final_volumes_ul": {mid: {well_name(cell): round(v, 9) for cell, v in wells.items()} for mid, wells in volumes.items()},
        "final_deck": {str(slot): stack[-1] for slot, stack in occupancy.items()},
        "final_deck_stacks": {str(slot): list(stack) for slot, stack in occupancy.items()}, "run_steps": run_steps,
        "spent_tip_rack_ids": [material_id for material_id in returned_supply_racks if returned_tips.get(material_id)],
        "manual_checkpoints": sum(s.kind == "manual" for s, _ in expanded) + int(bool(returned_supply_racks)) + int(bool(returned_tips))}
    result.report["ok"] = not any(i["severity"] == "error" for i in result.report["issues"])
    result.report["valid"] = result.report["ok"]
    result.report["run_sheet"] = result.report["summary"]
    return result


def validate_plan(plan: ProtocolPlan | dict, setup: ProtocolSetup | dict, context: dict, *, sources: Any = None) -> dict:
    """Return blocking questions, machine checks and a per-well run sheet."""
    return prepare_protocol(plan, setup, context, sources=sources).report


def validate_source_grounding(plan: ProtocolPlan | dict, sources: Any) -> list[dict]:
    """Check citations and numerical evidence without requiring an experiment setup."""
    plan = plan if isinstance(plan, ProtocolPlan) else ProtocolPlan.model_validate(plan)
    result = PreparedProtocol(plan=plan)
    paragraphs = _sources(sources)
    decisions = {decision.path: decision for decision in plan.decisions}

    def visit(steps: list[ProtocolStep], prefix: str, depth: int) -> None:
        if depth > MAX_NESTING:
            result.issue("nesting_limit", prefix, f"Repeat nesting exceeds {MAX_NESTING} levels.")
            return
        for index, step in enumerate(steps):
            path = f"{prefix}/{index}"
            _numeric_grounding(result, step, path, paragraphs, decisions)
            visit(step.steps, path + "/steps", depth + 1)

    visit(plan.steps, "/steps", 0)
    return result.report["issues"]
