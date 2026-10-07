"""Constrained line edits for model-authored OT-2 protocol repair.

This module never authors protocol code. It asks the local model for small
source edits and applies them mechanically before the normal validation gates.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pybravo.evals.text2wetlab.adapter import EventLog


class PatchError(ValueError):
    """A proposed edit cannot be applied to the exact rejected candidate."""


LINE_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "replacement": {"type": "string"},
                },
                "required": ["start_line", "end_line", "replacement"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["edits"],
    "additionalProperties": False,
}


def numbered_source(source: str) -> str:
    """Show exact 1-based line coordinates without changing the source."""
    return "\n".join(f"{number:04d}| {line}" for number, line in enumerate(source.splitlines(), 1))


def task_allows_tip_refill(instruction: str) -> bool:
    """Read explicit task permission; a simulator failure does not grant it."""
    permission = re.search(
        r"\b(?:tips are unlimited|tip racks? may be refilled|"
        r"(?:may|can|should)\s+refill(?:\s+the)?\s+tip racks?|"
        r"call\s+\w+\.reset_tipracks\(\))",
        instruction, re.IGNORECASE,
    )
    prohibition = re.search(
        r"\b(?:do not|never|cannot|must not)\b[^\n]{0,80}\b(?:refill|reset_tipracks)\b",
        instruction, re.IGNORECASE,
    )
    return bool(permission) and not bool(prohibition)


def line_patch_messages(
    source: str,
    *,
    instruction: str,
    diagnostic: str,
    scientific_source: str | None = None,
) -> list[dict[str, str]]:
    """Ask for localized edits grounded in the original task and failure."""
    if not source.strip() or not instruction.strip() or not diagnostic.strip():
        raise PatchError("Source, task instruction, and failure diagnostic are required.")
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    refill_allowed = task_allows_tip_refill(instruction)
    refill_guidance = (
        "If the task permits fresh-tip refills, add reset_tipracks() only after the "
        "loaded rack is exhausted and fresh tips have been supplied. "
        if refill_allowed else "Do not reset tip racks or reuse spent tips. "
    )
    task = "Benchmark task instruction (experimental facts to preserve):\n" + instruction.strip()
    if scientific_source and scientific_source.strip():
        task += "\n\nSupplied scientific method passage:\n" + scientific_source.strip()
    return [
        {
            "role": "system",
            "content": (
                "Edit a previously generated Opentrons OT-2 Python protocol to correct a specific "
                "validation or simulation failure. Return JSON with only an edits array. Each edit "
                "has 1-based start_line, inclusive end_line, and replacement text. To insert before "
                "line N, set start_line=N and end_line=N-1; line N=len(source)+1 appends. "
                "Make the smallest changes that fix the reported failure. Preserve all task-stated "
                "labware, deck slots, wells, volumes, temperatures, timing, source-to-destination "
                "mapping, and contamination policy. Do not remove a required operation to silence a "
                "failure. Keep the existing aspirate, dispense, transfer, and mix action count, "
                "volumes, order, and direct locations. For a pipette working-range failure, "
                "reassign the same liquid action to a suitable loaded pipette and add fresh-tip "
                "cycles if needed; do not lower its volume. Do not return used tips. "
                + refill_guidance + "Do not use benchmark reference output or add "
                "simulation-only branches. "
                "Do not return a complete protocol or Markdown. The proposed edits will be applied "
                "mechanically and revalidated; invalid line ranges or broad rewrites are rejected."
            ),
        },
        {"role": "user", "content": task},
        {
            "role": "user",
            "content": (
                f"Rejected protocol SHA-256: {source_digest}\n"
                f"Failure diagnostic:\n{diagnostic[:4000]}\n\n"
                "Rejected protocol with exact 1-based line numbers:\n" + numbered_source(source)
            ),
        },
    ]


def apply_line_patch(source: str, payload: dict[str, Any]) -> str:
    """Apply a bounded set of nonoverlapping model edits to *source*.

    Editing is text-only. Callers must re-run static, simulator, event, and
    task-fact checks before accepting the resulting protocol.
    """
    edits = payload.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise PatchError("Expected one to eight line edits.")
    lines = source.splitlines()
    line_count = len(lines)
    normalized: list[tuple[int, int, str]] = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"start_line", "end_line", "replacement"}:
            raise PatchError("Each edit needs only start_line, end_line, and replacement.")
        start, end, replacement = edit["start_line"], edit["end_line"], edit["replacement"]
        if type(start) is not int or type(end) is not int or not isinstance(replacement, str):
            raise PatchError("Line coordinates must be integers and replacement must be text.")
        if not 1 <= start <= line_count + 1 or not 0 <= end <= line_count or start > end + 1:
            raise PatchError("Edit line range is outside the rejected protocol.")
        if "\x00" in replacement or "\r" in replacement or len(replacement) > 12_000:
            raise PatchError("Replacement contains unsupported characters or is too large.")
        if start <= end and end - start + 1 > 60:
            raise PatchError("A patch cannot replace more than 60 original lines at once.")
        normalized.append((start, end, replacement))
    normalized.sort()
    for (left_start, left_end, _), (right_start, right_end, _) in zip(normalized, normalized[1:]):
        if right_start <= max(left_end + 1, left_start):
            raise PatchError("Edits overlap or share an ambiguous insertion boundary.")
        if left_start == left_end + 1 and right_start == right_end + 1 and left_start == right_start:
            raise PatchError("Two edits insert at the same line boundary.")
    result = list(lines)
    for start, end, replacement in reversed(normalized):
        replacement_lines = replacement.splitlines()
        result[start - 1:end] = replacement_lines
    patched = "\n".join(result) + "\n"
    if patched == source:
        raise PatchError("The proposed edits did not change the protocol.")
    return patched


def _liquid_actions(log: EventLog) -> list[tuple[str, str, str, str, float, int]]:
    return [
        (str(event.get("kind")), str(event.get("instrument")),
         str(event.get("labware")), str(event.get("well")),
         float(event.get("volume") or 0), int(event.get("channels") or 1))
        for event in log.events if event.get("kind") in {"aspirate", "dispense"}
    ]


def _timed_module_actions(log: EventLog) -> Counter[str]:
    return Counter(
        str(event.get("text")) for event in log.events
        if event.get("kind") in {"thermocycler", "temperature", "magnet"}
        and "hold time" in str(event.get("text"))
    )


def preserve_existing_task_actions(baseline: EventLog, candidate: EventLog) -> str | None:
    """Protect existing transfers, deck bindings, and timed steps in a process repair."""
    if candidate.labware != baseline.labware:
        return "The patch changed the loaded labware catalog or deck mapping."
    if _liquid_actions(candidate) != _liquid_actions(baseline):
        return "The patch changed existing liquid transfers, volumes, wells, or order."
    if _timed_module_actions(candidate) != _timed_module_actions(baseline):
        return "The patch removed or changed a timed module action."
    return None


_LIQUID_METHODS = {"aspirate", "dispense", "transfer", "distribute", "consolidate", "mix"}
_RESOURCE_METHODS = {"load_labware", "load_module", "load_instrument", "load_adapter"}
_PROTECTED_CONTROL = (ast.For, ast.AsyncFor, ast.While, ast.If, ast.IfExp,
                      ast.Try, ast.Break, ast.Continue, ast.Return)
_TIMED_METHODS = {"delay", "pause", "set_block_temperature", "set_lid_temperature",
                  "set_temperature", "engage", "disengage"}
_TIP_MOTION_METHODS = {"blow_out", "touch_tip", "air_gap"}


def _call_method(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _volume_argument(call: ast.Call, method: str) -> ast.expr | None:
    position = 1 if method == "mix" else 0
    if len(call.args) > position:
        return call.args[position]
    keyword_names = ("volume", "volume_ul") if method != "mix" else ("volume", "volume_ul")
    return next((item.value for item in call.keywords if item.arg in keyword_names), None)


def _program_facts(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    calls = [node for node in nodes if isinstance(node, ast.Call)]
    liquids = [(method, _volume_argument(call, method), call) for call in calls
               if (method := _call_method(call)) in _LIQUID_METHODS]
    volume_names = {
        name.id for _, volume, _ in liquids if volume is not None
        for name in ast.walk(volume) if isinstance(name, ast.Name)
    }


    volume_bindings: list[tuple[str, str]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            volume_bindings.extend((target.id, ast.dump(node.value, include_attributes=False))
                                   for target in node.targets if isinstance(target, ast.Name)
                                   and target.id in volume_names)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            if node.target.id in volume_names:
                volume_bindings.append((node.target.id, ast.dump(node.value, include_attributes=False)))
    literal_wells: Counter[str] = Counter()
    reagent_bindings: list[tuple[str, str]] = []
    index_nodes = {id(child) for node in nodes if isinstance(node, ast.Subscript)
                   for child in ast.walk(node.slice)}
    numeric_literals: Counter[tuple[str, int | float]] = Counter(
        (type(node.value).__name__, node.value) for node in nodes
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}
        and id(node) not in index_nodes
    )
    for method, volume, call in liquids:
        locations = [arg for arg in call.args if arg is not volume]
        locations.extend(keyword.value for keyword in call.keywords
                         if keyword.value is not volume)
        for location in locations:
            literal_wells.update(node.value for node in ast.walk(location)
                                 if isinstance(node, ast.Constant) and isinstance(node.value, str))
    for node in nodes:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Subscript)
                and isinstance(node.value.slice, ast.Constant)
                and isinstance(node.value.slice.value, str)):
            reagent_bindings.extend((target.id, ast.dump(node.value, include_attributes=False))
                                    for target in node.targets if isinstance(target, ast.Name))
    return {
        "resources": [(target.id, ast.dump(node.value, include_attributes=False))
                      for node in nodes if isinstance(node, ast.Assign)
                      and isinstance(node.value, ast.Call)
                      and _call_method(node.value) in _RESOURCE_METHODS
                      for target in node.targets if isinstance(target, ast.Name)],
        "liquids": [(method, ast.dump(volume, include_attributes=False) if volume else None,
                     ast.dump(call.args[0], include_attributes=False)
                     if method == "mix" and call.args else None)
                    for method, volume, call in liquids],
        "locations": [(method, [ast.dump(arg, include_attributes=False)
                                for arg in call.args if arg is not volume],
                      [(keyword.arg, ast.dump(keyword.value, include_attributes=False))
                       for keyword in call.keywords
                       if keyword.arg in {"source", "dest", "destination", "location", "well",
                                          "source_well", "dest_well"}])
                      for method, volume, call in liquids if method != "mix"],
        "volume_bindings": volume_bindings,
        "reagent_bindings": reagent_bindings,
        "literal_wells": literal_wells,
        "numeric_literals": numeric_literals,
        "control": [(type(node).__name__, ast.dump(node.iter if isinstance(node, (ast.For, ast.AsyncFor))
                                                else node.test, include_attributes=False)
                     if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.If, ast.IfExp))
                     else type(node).__name__)
                    for node in nodes if isinstance(node, _PROTECTED_CONTROL)],
        "timed": [ast.dump(call, include_attributes=False) for call in calls
                  if _call_method(call) in _TIMED_METHODS],
        "tip_motion": [ast.dump(call, include_attributes=False) for call in calls
                       if _call_method(call) in _TIP_MOTION_METHODS],
        "tip_counts": Counter(_call_method(call) for call in calls
                              if _call_method(call) in {"pick_up_tip", "drop_tip",
                                                        "return_tip", "reset_tipracks"}),
    }


def _refill_guard(node: ast.AST) -> bool:
    """Only a side-effect-free loop-counter check may guard one rack reset."""
    if not isinstance(node, ast.If) or len(node.body) != 1 or node.orelse:
        return False
    statement = node.body[0]
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    if _call_method(call) != "reset_tipracks" or call.args or call.keywords:
        return False
    return not any(isinstance(child, (ast.Call, ast.NamedExpr, ast.Attribute, ast.Subscript))
                   for child in ast.walk(node.test))


def _refill_control_facts(source: str) -> tuple[list[str], Counter[str], Counter[tuple[str, int | float]]]:
    tree = ast.parse(source)
    controls: list[str] = []
    guards: Counter[str] = Counter()
    guard_numbers: Counter[tuple[str, int | float]] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, _PROTECTED_CONTROL):
            continue
        if _refill_guard(node):
            guards[ast.dump(node, include_attributes=False)] += 1
            guard_numbers.update((type(child.value).__name__, child.value)
                                 for child in ast.walk(node.test)
                                 if isinstance(child, ast.Constant)
                                 and type(child.value) in {int, float})
        else:
            controls.append(ast.dump(node, include_attributes=False) if isinstance(node, ast.If)
                            else str(type(node).__name__) + ":" + (
                                ast.dump(node.iter if isinstance(node, (ast.For, ast.AsyncFor))
                                         else node.test, include_attributes=False)
                                if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.IfExp)) else ""))
    return controls, guards, guard_numbers


def preserve_simulator_repair_facts(
    source: str, candidate: str, *, allow_tip_refill: bool = False,
) -> str | None:
    """Reject a simulator fix that changes recorded experimental work.

    The original has no usable event log, so this guard compares static facts.
    It intentionally permits changes to well-index expressions and explicit
    mix targets while preserving the liquid action/volume sequence. The normal
    validator, simulator, and event safety gate remain mandatory afterward.
    """
    before, after = _program_facts(source), _program_facts(candidate)
    if before["resources"] != after["resources"]:
        return "The patch changed loaded hardware, labware, or deck bindings."
    if before["liquids"] != after["liquids"] or before["volume_bindings"] != after["volume_bindings"]:
        return "The patch changed liquid actions or their declared volumes."
    if before["locations"] != after["locations"] or before["reagent_bindings"] != after["reagent_bindings"]:
        return "The patch changed an existing liquid source, destination, or reagent binding."
    if before["control"] != after["control"] or (
        after["tip_counts"]["reset_tipracks"] > before["tip_counts"]["reset_tipracks"]
    ):
        before_control, before_guards, before_guard_numbers = _refill_control_facts(source)
        after_control, after_guards, after_guard_numbers = _refill_control_facts(candidate)
        added_guards = after_guards - before_guards
        if (not allow_tip_refill or before_control != after_control
                or before_guards - after_guards
                or sum(added_guards.values()) !=
                after["tip_counts"]["reset_tipracks"] - before["tip_counts"]["reset_tipracks"]):
            return "The patch changed protocol loop or control-flow structure."
    if before["timed"] != after["timed"]:
        return "The patch changed an incubation, module command, or manual pause."
    if before["tip_motion"] != after["tip_motion"]:
        return "The patch changed a liquid-contact motion setting."
    earlier_tips, patched_tips = before["tip_counts"], after["tip_counts"]
    if (any(patched_tips[name] < earlier_tips[name] for name in ("pick_up_tip", "drop_tip"))
            or patched_tips["return_tip"] != earlier_tips["return_tip"]
            or patched_tips["reset_tipracks"] < earlier_tips["reset_tipracks"]
            or (patched_tips["reset_tipracks"] > earlier_tips["reset_tipracks"]
                and not allow_tip_refill)):
        return "The patch removed a tip change or added tip return/refill behavior."
    if before["numeric_literals"] != after["numeric_literals"]:
        if (not allow_tip_refill or before["numeric_literals"] - after["numeric_literals"]
                or after["numeric_literals"] - before["numeric_literals"] !=
                after_guard_numbers - before_guard_numbers):
            return "The patch changed a non-index numeric setting or task quantity."
    if before["literal_wells"] - after["literal_wells"]:
        return "The patch removed an existing liquid-operation well or material reference."
    return None
