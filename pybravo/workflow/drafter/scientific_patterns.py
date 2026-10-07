"""Conservative scientific-pattern checks for unreviewed Designer graphs.

These checks inspect the actions the Bravo executor can actually perform. They
do not infer a protocol from a paper, repair a graph, or claim that a workflow
is safe to run. The returned dictionaries match generated-draft review issues.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pybravo.head_mode import normalize_head_mode
from pybravo.types import HeadType

_LIQUID = {"liquid/Aspirate", "liquid/Dispense", "liquid/Mix"}
_WELL = re.compile(r"^[A-Z]{1,2}[1-9][0-9]*$", re.IGNORECASE)
_COUNT = re.compile(
    r"\b(?:for\s+)?(\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:[A-Za-z]+(?:-[A-Za-z]+)*\s+){0,4}"
    r"(?:samples?|colonies|plasmids?|transformations?|reactions?)\b",
    re.IGNORECASE,
)
_VOLUME = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:µ|μ|u)l\b", re.IGNORECASE)
_WELL_RANGE = re.compile(r"\b([A-Z])([1-9][0-9]*)\s*:\s*([A-Z])([1-9][0-9]*)\b", re.IGNORECASE)
_NUMBER_WORDS = {name: count for count, name in enumerate(
    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"))}
_NUMBERED = re.compile(r"^\s*(\d{1,3})[.)]\s+(.+)$", re.MULTILINE)
_MODULES = {
    "thermocycler": re.compile(r"\bthermocycl(?:er|ing|e)\b", re.IGNORECASE),
    "magnet": re.compile(r"\bmagnet(?:ic)?\b", re.IGNORECASE),
    "temperature module": re.compile(r"\btemperature\s+module\b|\btempdeck\b", re.IGNORECASE),
    "centrifuge": re.compile(r"\bcentrifug(?:e|ation|ing)\b", re.IGNORECASE),
    "heater/shaker": re.compile(r"\b(?:heater[ -]?shaker|shaking incubator)\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class StageRequirement:
    """An externally grounded stage in execution order.

    ``kind`` is ``transfer``, ``mix``, ``manual``, or ``wait``. ``marker`` is
    a word or phrase that must occur in an action node's title/message for a
    manual stage. A transfer may specify a per-well volume in microlitres.
    Callers can provide requirements from a reviewed plan; this module also
    extracts simple numbered stages from explicit task instructions.
    """

    kind: str
    label: str
    marker: str = ""
    volume_ul: float | None = None
    cycles: int | None = None


def _issue(code: str, message: str, path: str = "/graph", *, severity: str = "error") -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message, "path": path}


def _nodes(workflow: Mapping[str, Any]) -> list[dict[str, Any]]:
    graph = workflow.get("graph") or {}
    return [node for node in graph.get("nodes") or [] if isinstance(node, dict)]


def _ordered_nodes(workflow: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Flatten an unambiguous flow path, including Loop body before done.

    Branches or malformed cycles return None so order-sensitive checks do not
    pretend to know which path runs. A conventional Loop has a body output
    (slot 0) ending at a terminal node and a done output (slot 1).
    """
    graph = workflow.get("graph") or {}
    by_id = {node.get("id"): node for node in _nodes(workflow)}
    edges: dict[Any, dict[int, list[Any]]] = {}
    for link in graph.get("links") or []:
        if not isinstance(link, (list, tuple)) or len(link) < 6 or link[5] != -1:
            continue
        edges.setdefault(link[1], {}).setdefault(link[2], []).append(link[3])
    starts = [node for node in by_id.values() if node.get("type") == "flow/Start"]
    if len(starts) != 1:
        return None
    ordered: list[dict[str, Any]] = []
    visited: set[Any] = set()

    def walk(node_id: Any, active: set[Any]) -> bool:
        if node_id in active or node_id not in by_id or node_id in visited:
            return False
        node = by_id[node_id]
        if node.get("type") == "flow/IfElse":
            return False
        visited.add(node_id)
        ordered.append(node)
        outgoing = edges.get(node_id, {})
        if node.get("type") == "flow/Loop":
            if any(len(targets) != 1 for targets in outgoing.values()):
                return False
            for slot in (0, 1):
                for target in outgoing.get(slot, []):
                    if not walk(target, active | {node_id}):
                        return False
            return True
        targets = [target for values in outgoing.values() for target in values]
        if len(targets) > 1:
            return False
        return not targets or walk(targets[0], active | {node_id})

    return ordered if walk(starts[0]["id"], set()) else None


def _scientific_text(source_instruction: str) -> str:
    """Omit generic simulator constraints that only name example modules."""
    return re.split(r"^##\s+Tools and constraints\b", source_instruction, maxsplit=1,
                    flags=re.IGNORECASE | re.MULTILINE)[0]


def numbered_stage_requirements(source_instruction: str) -> list[StageRequirement]:
    """Read only explicit numbered protocol steps, without inventing steps.

    This intentionally requires a protocol section heading. Paper summaries
    and numbered tool constraints are too ambiguous for automatic extraction.
    """
    match = re.search(r"^##\s+(?:The\s+)?protocol to implement\b", source_instruction,
                      re.IGNORECASE | re.MULTILINE)
    if not match:
        return []
    section = re.split(r"^##\s+", source_instruction[match.end():], maxsplit=1,
                       flags=re.MULTILINE)[0]
    requirements: list[StageRequirement] = []
    for number, text in _NUMBERED.findall(section):
        label = f"Step {number}: {text.strip()[:110]}"
        volume_match = _VOLUME.search(text)
        volume = float(volume_match.group(1)) if volume_match else None
        events: list[tuple[int, StageRequirement]] = []
        for pattern, kind, marker in (
            (r"\b(?:transfer|add|remove|wash|elute|dispense)\b", "transfer", ""),
            (r"\bmix\b", "mix", ""),
            (r"\bincubat\w*\b", "wait", ""),
            (r"\b(?:air\s+dry|dry\s+beads)\b", "manual", "dry"),
            (r"\b(?:disengage|remove\s+from)\s+(?:the\s+)?magnet(?:ic\s+module)?\b", "manual", "magnet_off"),
            (r"\b(?:re-)?engage\s+(?:the\s+)?magnet(?:ic\s+module)?\b", "manual", "magnet_on"),
            (r"\bthermocycl\w*\b", "manual", "thermocycl"),
            (r"\bheat\s+shock\b", "manual", "heat shock"),
            (r"\b(?:unseal|seal)\b", "manual", "seal"),
            (r"\bcentrifug\w*\b", "manual", "centrifug"),
        ):
            if found := re.search(pattern, text, re.IGNORECASE):
                events.append((found.start(), StageRequirement(
                    kind, label, marker=marker,
                    volume_ul=volume if kind == "transfer" else None,
                    cycles=(int(cycle.group(1)) if kind == "mix" and (
                        cycle := re.search(r"\bmix\s+(\d+)\s+times\b", text, re.IGNORECASE)) else None),
                )))
        requirements.extend(stage for _, stage in sorted(events, key=lambda item: item[0]))
    return requirements


def _stage_match(node: Mapping[str, Any], requirement: StageRequirement) -> bool:
    kind = node.get("type", "")
    props = node.get("properties") or {}
    if requirement.kind == "transfer":
        if kind != "liquid/Dispense":
            return False
        if requirement.volume_ul is None:
            return True
        try:
            return abs(float(props.get("volume")) - requirement.volume_ul) < 1e-6
        except (TypeError, ValueError):
            return False
    if requirement.kind == "mix":
        if kind != "liquid/Mix":
            return False
        if requirement.cycles is None:
            return True
        try:
            return int(props.get("cycles")) == requirement.cycles
        except (TypeError, ValueError):
            return False
    if requirement.kind == "wait":
        return kind in {"system/Manual", "system/Wait"} and bool(
            re.search(r"\b(?:incubat\w*|wait\w*|hold\w*)\b", _node_text(node), re.IGNORECASE))
    if requirement.kind == "manual":
        if kind != "system/Manual":
            return False
        text = _node_text(node)
        if requirement.marker == "magnet_on":
            return bool(re.search(r"\b(?:engage|activate|place\s+(?:plate\s+)?on|move\s+(?:plate\s+)?to)\b"
                                  r"[^.\n]{0,80}\bmagnet", text, re.IGNORECASE))
        if requirement.marker == "magnet_off":
            return bool(re.search(r"\b(?:disengage|remove\s+(?:plate\s+)?from|turn\s+off)\b"
                                  r"[^.\n]{0,80}\bmagnet", text, re.IGNORECASE))
        return bool(re.search(r"\b" + re.escape(requirement.marker) + r"\w*\b", text, re.IGNORECASE))
    return False


def _node_text(node: Mapping[str, Any]) -> str:
    props = node.get("properties") or {}
    return " ".join(str(part) for part in (node.get("title", ""), props.get("message", "")) if part)


def _stage_issues(ordered: list[dict[str, Any]] | None,
                  requirements: Sequence[StageRequirement]) -> list[dict[str, str]]:
    if not requirements:
        return []
    if ordered is None:
        return [_issue("STAGE_ORDER_UNPROVEN", "The flow branches or cycles, so required scientific stage order cannot be checked.")]
    issues: list[dict[str, str]] = []
    prior_index = -1
    prior_label = ""
    prior_offset = -1
    for requirement in requirements:
        matches = [index for index, node in enumerate(ordered) if _stage_match(node, requirement)]
        later = next((index for index in matches
                      if index > prior_index or (
                          index == prior_index and requirement.label == prior_label
                          and _stage_offset(ordered[index], requirement) > prior_offset)), None)
        if later is None:
            code = "STAGE_OUT_OF_ORDER" if matches else "SOURCE_STAGE_UNACCOUNTED"
            issues.append(_issue(code, f"{requirement.label} has no matching action after the preceding required stage."))
        else:
            prior_index = later
            prior_label = requirement.label
            prior_offset = _stage_offset(ordered[later], requirement)
    return issues


def _stage_offset(node: Mapping[str, Any], requirement: StageRequirement) -> int:
    """Position inside a combined Manual handoff for stages on one source line."""
    text = _node_text(node)
    patterns = {
        "magnet_on": r"\b(?:engage|activate|place|move)\b",
        "magnet_off": r"\b(?:disengage|remove|turn\s+off)\b",
        "dry": r"\bdry\b",
        "seal": r"\b(?:unseal|seal)\b",
        "thermocycl": r"\bthermocycl\w*\b",
        "heat shock": r"\bheat\s+shock\b",
        "centrifug": r"\bcentrifug\w*\b",
    }
    pattern = (r"\b(?:incubat\w*|wait\w*|hold\w*)\b" if requirement.kind == "wait"
               else patterns.get(requirement.marker, ""))
    match = re.search(pattern, text, re.IGNORECASE) if pattern else None
    return match.start() if match else 0


def _sample_count(source_instruction: str) -> int | None:
    scientific = _scientific_text(source_instruction)
    lead = scientific.split("##", 1)[0]

    def counts(text: str) -> list[int]:
        return [int(match.group(1)) if match.group(1).isdigit()
                else _NUMBER_WORDS[match.group(1).lower()] for match in _COUNT.finditer(text)]

    leading = counts(lead)
    if leading:
        return max(leading)
    # A range populated with separate specimens implies repeated handling,
    # even when the prose says only "each well" rather than a numeric count.
    for line in scientific.splitlines():
        if "sample" not in line.lower() or "well" not in line.lower():
            continue
        match = _WELL_RANGE.search(line)
        if match:
            rows = abs(ord(match.group(3).upper()) - ord(match.group(1).upper())) + 1
            columns = abs(int(match.group(4)) - int(match.group(2))) + 1
            if rows * columns > 1:
                return rows * columns
    remaining = counts(scientific)
    return remaining[0] if remaining else None


def _declared_well_count(wells: Any) -> int | None:
    if isinstance(wells, list):
        return len(wells) if wells else None
    if not isinstance(wells, str) or not wells.strip():
        return None
    if match := _WELL_RANGE.fullmatch(wells.strip()):
        return (abs(ord(match.group(3).upper()) - ord(match.group(1).upper())) + 1) * (
            abs(int(match.group(4)) - int(match.group(2))) + 1)
    parts = [part.strip() for part in wells.split(",")]
    return len(parts) if all(_WELL.fullmatch(part) for part in parts) else None


def _iter_length(anchor: Any) -> int:
    if not isinstance(anchor, str) or not anchor.startswith("iter:"):
        return 0
    values = [part.strip() for part in anchor[5:].split(",")]
    return len(values) if values and all(_WELL.fullmatch(value) for value in values) else 0


def _mapping_issues(workflow: Mapping[str, Any], source_instruction: str,
                    ordered: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    count = _sample_count(source_instruction)
    if not count or count <= 1:
        return []
    nodes = ordered or _nodes(workflow)
    aspirates = [node for node in nodes if node.get("type") == "liquid/Aspirate"]
    dispenses = [node for node in nodes if node.get("type") == "liquid/Dispense"]
    if not aspirates or not dispenses:
        return []  # Overall action completeness is checked elsewhere.
    source_anchors = [str((node.get("properties") or {}).get("anchor", "")) for node in aspirates]
    dest_anchors = [str((node.get("properties") or {}).get("anchor", "")) for node in dispenses]
    explicit_sources = {anchor.upper() for anchor in source_anchors if _WELL.fullmatch(anchor)}
    explicit_dests = {anchor.upper() for anchor in dest_anchors if _WELL.fullmatch(anchor)}
    loops = [int((node.get("properties") or {}).get("count", 0)) for node in nodes
             if node.get("type") == "flow/Loop" and str((node.get("properties") or {}).get("count", "")).isdigit()]
    dynamic_coverage = bool(loops and max(loops) >= count
                            and any(_iter_length(anchor) >= count for anchor in source_anchors + dest_anchors))
    explicit_coverage = max(len(explicit_sources), len(explicit_dests)) >= count
    if dynamic_coverage or explicit_coverage:
        return []
    return [_issue(
        "SAMPLE_MAPPING_UNPROVEN",
        f"The source names {count} separate specimens, but executable anchors do not establish coverage of {count} wells. A wells property is documentation only; use checked head geometry or a loop with iter: anchors.",
    )]


def _resolve_head_type(head_type: str | HeadType | None) -> HeadType | None:
    if isinstance(head_type, HeadType):
        return head_type
    if isinstance(head_type, str):
        return HeadType.__members__.get(head_type)
    return None


def _liquid_and_tip_issues(workflow: Mapping[str, Any], ordered: list[dict[str, Any]] | None,
                           head_type: str | HeadType | None) -> list[dict[str, str]]:
    nodes = _nodes(workflow)
    node_index = {id(node): index for index, node in enumerate(nodes)}
    issues: list[dict[str, str]] = []
    resolved_head = _resolve_head_type(head_type)
    ordered_nodes = ordered or nodes
    active_mode: Mapping[str, Any] = {}
    tips_on = False
    held_ul = 0.0
    last_source: tuple[str, str] | None = None
    for node in ordered_nodes:
        kind = node.get("type")
        props = node.get("properties") or {}
        index = node_index[id(node)]
        path = f"/graph/nodes/{index}"
        if kind == "tips/TipsOn":
            if tips_on and ordered is not None:
                issues.append(_issue("TIPS_ON_WHILE_ATTACHED", "A new tip pickup occurs before the previous tips are removed.", path))
            tips_on, held_ul, last_source = True, 0.0, None
            active_mode = props.get("head_mode") or active_mode
            continue
        if kind == "tips/TipsOff":
            if not tips_on and ordered is not None:
                issues.append(_issue("TIPS_OFF_WITHOUT_TIPS", "Tips Off occurs with no attached tips.", path))
            tips_on, held_ul, last_source = False, 0.0, None
            continue
        if kind not in _LIQUID:
            continue
        if not tips_on and ordered is not None:
            issues.append(_issue("LIQUID_WITHOUT_TIPS", "Liquid handling occurs without an attached tip lifecycle.", path))
        mode = props.get("head_mode") or active_mode
        wells = props.get("wells")
        declared_count = _declared_well_count(wells)
        if wells:
            issues.append(_issue(
                "NON_EXECUTABLE_WELL_LIST",
                "Bravo liquid nodes do not execute properties.wells. Only anchor, iter: expansion, and head mode select wells; review the intended mapping.",
                path + "/properties/wells",
            ))
        if resolved_head is not None and isinstance(mode, Mapping) and mode:
            try:
                footprint = normalize_head_mode(
                    resolved_head, mode.get("subset_type"), mode.get("subset_config"),
                    mode.get("row_count"), mode.get("column_count"),
                ).num_channels
            except (TypeError, ValueError):
                footprint = 0
            if footprint:
                if declared_count and footprint != declared_count:
                    issues.append(_issue(
                        "HEAD_FOOTPRINT_MISMATCH",
                        f"The selected head mode addresses {footprint} wells per action, while the ignored wells property names {declared_count}. Confirm an executable head mode and well mapping.",
                        path + "/properties/wells",
                    ))
                location = str(props.get("location", ""))
                stack = (workflow.get("deck") or {}).get(location) or []
                if stack and isinstance(stack[-1], Mapping):
                    try:
                        labware_wells = int(stack[-1].get("wells", 0))
                    except (TypeError, ValueError):
                        labware_wells = 0
                    if labware_wells and footprint > labware_wells:
                        issues.append(_issue(
                            "HEAD_EXCEEDS_LABWARE",
                            f"The selected head mode addresses {footprint} wells on labware with {labware_wells} wells at Bravo location {location}.",
                            path,
                        ))
        try:
            volume = float(props.get("volume"))
        except (TypeError, ValueError):
            volume = 0.0
        if kind == "liquid/Aspirate":
            source = (str(props.get("location", "")), str(props.get("anchor", "")))
            if ordered is not None and last_source and source != last_source:
                issues.append(_issue(
                    "CROSS_SOURCE_TIP_REVIEW",
                    "One tip lifecycle aspirates from different source positions or wells; confirm stock and sample isolation or use fresh tips.",
                    path,
                ))
            last_source = source
            held_ul += max(volume, 0.0)
        elif kind == "liquid/Dispense" and volume > held_ul + 1e-6 and ordered is not None:
            issues.append(_issue(
                "DISPENSE_EXCEEDS_ASPIRATED",
                f"This tip lifecycle dispenses {volume:g} µL with only {held_ul:g} µL accounted for since the last aspiration.",
                path,
            ))
        elif kind == "liquid/Dispense":
            held_ul = max(0.0, held_ul - max(volume, 0.0))
    if tips_on and ordered is not None:
        issues.append(_issue("TIPS_REMAIN_AT_END", "The workflow ends with tips still attached."))
    return issues


def _loop_tip_isolation_issues(workflow: Mapping[str, Any]) -> list[dict[str, str]]:
    """A per-sample loop must include pickup and release in its body."""
    graph = workflow.get("graph") or {}
    nodes = _nodes(workflow)
    by_id = {node.get("id"): node for node in nodes}
    adjacency: dict[Any, list[Any]] = {}
    bodies: dict[Any, list[Any]] = {}
    for link in graph.get("links") or []:
        if not isinstance(link, (list, tuple)) or len(link) < 6 or link[5] != -1:
            continue
        adjacency.setdefault(link[1], []).append(link[3])
        if link[2] == 0:
            bodies.setdefault(link[1], []).append(link[3])
    issues: list[dict[str, str]] = []
    for loop in nodes:
        if loop.get("type") != "flow/Loop":
            continue
        loop_id = loop.get("id")
        count = (loop.get("properties") or {}).get("count", 0)
        try:
            if int(count) <= 1:
                continue
        except (TypeError, ValueError):
            continue
        reached: set[Any] = set()
        pending = list(bodies.get(loop_id, ()))
        while pending:
            node_id = pending.pop()
            if node_id == loop_id or node_id in reached:
                continue
            reached.add(node_id)
            pending.extend(adjacency.get(node_id, ()))
        body = [by_id[node_id] for node_id in reached if node_id in by_id]
        sample_aspirate = any(
            node.get("type") == "liquid/Aspirate"
            and _iter_length((node.get("properties") or {}).get("anchor")) > 1
            for node in body
        )
        if sample_aspirate and not ({"tips/TipsOn", "tips/TipsOff"} <= {node.get("type") for node in body}):
            issues.append(_issue(
                "REUSED_SAMPLE_TIP_IN_LOOP",
                f"Loop {loop_id} changes sample source wells across iterations without a complete tip pickup/release cycle in its body.",
                f"/graph/nodes/{nodes.index(loop)}",
            ))
    return issues


def _module_issues(workflow: Mapping[str, Any], source_instruction: str) -> list[dict[str, str]]:
    scientific = _scientific_text(source_instruction)
    nodes = _nodes(workflow)
    manual_text = " ".join(_node_text(node) for node in nodes if node.get("type") == "system/Manual")
    issues: list[dict[str, str]] = []
    for label, pattern in _MODULES.items():
        alternative = (label == "temperature module" and bool(re.search(
            r"\b(?:keep|hold|cool|chill)\b[^.\n]{0,45}\b(?:4\s*°?\s*C|cold|chilled)\b",
            manual_text, re.IGNORECASE)))
        if pattern.search(scientific) and not pattern.search(manual_text) and not alternative:
            issues.append(_issue(
                "MODULE_HANDOFF_MISSING",
                f"The source names a {label} stage, but no explicit system/Manual handoff names it.",
            ))
    for index, node in enumerate(nodes):
        if node.get("type") in {"system/Manual", "flow/Start", "flow/End", "flow/Loop", "flow/IfElse", "flow/Frame"}:
            continue
        text = _node_text(node)
        for label, pattern in _MODULES.items():
            if pattern.search(text):
                issues.append(_issue(
                    "UNSUPPORTED_MODULE_ACTION",
                    f"{node.get('type')} claims a {label} action. Bravo requires an explicit operator handoff for this stage.",
                    f"/graph/nodes/{index}",
                ))
                break
    return issues


def _output_phase_issues(workflow: Mapping[str, Any], source_instruction: str) -> list[dict[str, str]]:
    """Flag an explicitly requested eluate recovery with no action for it."""
    scientific = _scientific_text(source_instruction)
    recovery = re.search(r"\b(?:recover|transfer|move|elute)\b[^.\n]{0,150}"
                         r"\b(?:eluate|elution)[_ ]?", scientific, re.IGNORECASE)
    if not recovery:
        return []
    if any(re.search(r"\b(?:elut\w*|eluate\w*)\b", _node_text(node), re.IGNORECASE)
           for node in _nodes(workflow)
           if node.get("type") in _LIQUID or (
               node.get("type") == "system/Manual"
               and not re.search(r"\bsetup\b", str(node.get("title", "")), re.IGNORECASE))):
        return []
    return [_issue(
        "ELUTION_STAGE_UNACCOUNTED",
        "The source explicitly requests eluate recovery, but no liquid action or operator handoff names that stage.",
    )]


def audit_scientific_patterns(
    workflow: Mapping[str, Any], *, source_instruction: str = "",
    head_type: str | HeadType | None = None,
    expected_stages: Sequence[StageRequirement] | None = None,
) -> list[dict[str, str]]:
    """Return non-mutating review issues for a Designer workflow.

    ``head_type`` is the active Bravo profile enum/name, if known. Passing a
    reviewed ``expected_stages`` sequence checks their order; otherwise simple
    numbered steps in an explicit protocol section are used. Free-form paper
    text is never converted into invented protocol requirements.
    """
    ordered = _ordered_nodes(workflow)
    requirements = (list(expected_stages) if expected_stages is not None
                    else numbered_stage_requirements(source_instruction))
    return (
        _liquid_and_tip_issues(workflow, ordered, head_type)
        + _loop_tip_isolation_issues(workflow)
        + _mapping_issues(workflow, source_instruction, ordered)
        + _module_issues(workflow, source_instruction)
        + _output_phase_issues(workflow, source_instruction)
        + _stage_issues(ordered, requirements)
    )
