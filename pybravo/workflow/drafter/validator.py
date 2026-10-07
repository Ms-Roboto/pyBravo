"""Post-generation sanity check on a DraftedWorkflow.

Schema-level hallucination (invalid node types, wrong value types) is
already caught by Pydantic when the LLM response is parsed. This
module catches what Pydantic can't:

* Graph topology (exactly one Start, at least one End, no orphans, no
  dangling link endpoints).
* Physical sanity of property references (locations 1-9, barcode reads
  target plates that exist on the deck, etc.).
* Unit-like sanity (volumes in a plausible range).

Returns a list of :class:`ValidationIssue` — empty means the draft
passes. The drafter's retry loop re-prompts the LLM with the issue
list when non-empty; beyond a retry budget the issues surface to the
operator as warnings without blocking the draft from opening in a tab.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from pybravo.workflow.drafter.schema import DraftedWorkflow


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a drafted workflow."""

    severity: str  # "error" | "warning"
    code: str       # short machine-readable identifier
    message: str    # human-readable; included verbatim in repair prompts
    node_id: int | None = None

    def __str__(self) -> str:
        prefix = f"[{self.severity.upper()} {self.code}]"
        if self.node_id is not None:
            prefix += f" node#{self.node_id}"
        return f"{prefix} {self.message}"


# ── Property-key expectations per node type ───────────────────────────
# Used by _check_required_properties. Keys listed here are *required*;
# anything else the LLM includes is forwarded untouched so per-node
# type validation in the runtime (executor) catches deeper issues.

_REQUIRED_PROPERTIES: dict[str, tuple[str, ...]] = {
    "plate/PickPlace": ("pick_location", "place_location"),
    # NOTE: Stack uses `base_location` everywhere else — designer
    # node, executor, bravo API, 3D viewer.  The validator previously
    # required `target_location`, which no caller ever produced and
    # so silently never triggered MISSING_PROPERTY errors for Stack
    # nodes drafted without the right key.  Fixed to match reality.
    "plate/Stack":   ("source_location", "base_location"),
    "plate/Destack": ("source_location", "destination_location"),
    "plate/Mount":   ("source_location", "base_location"),
    "plate/Unmount": ("source_location", "destination_location"),
    "plate/Delid":   ("location",),
    "plate/Relid":   ("location",),
    "liquid/Aspirate": ("location", "volume", "liquid_class"),
    "liquid/Dispense": ("location", "volume", "liquid_class"),
    "liquid/Mix":      ("location", "volume", "liquid_class"),
    "tips/TipsOn":     ("location",),
    "tips/TipsOff":    ("location",),
    "sensor/ReadBarcode":     ("location",),
    "sensor/ScanStackHeight": ("location",),
    "flow/Loop":       ("count",),
    "logic/Script":    ("script",),
    "system/Manual":  ("message",),
    "system/Wait":    ("duration_s",),
}


_LOCATION_PROPERTY_KEYS: tuple[str, ...] = (
    "location",
    "pick_location",
    "place_location",
    "source_location",
    "destination_location",
    "target_location",
    "base_location",
)


def _as_location_int(value: Any) -> int | None:
    """Coerce a location property into an int 1-9, or None if not a plain int.

    Accepts ``iter:1,2,3,4`` (runtime expands these from the loop index)
    by returning ``None`` — the validator skips physical-existence checks
    on iter: references; the runtime handles them.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.startswith("iter:") or value.startswith("var:"):
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


# ── Individual checks ─────────────────────────────────────────────────


def _check_start_end_counts(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    starts = [n for n in wf.graph.nodes if n.type == "flow/Start"]
    ends = [n for n in wf.graph.nodes if n.type == "flow/End"]
    if len(starts) == 0:
        issues.append(ValidationIssue(
            severity="error", code="NO_START",
            message="Workflow has no flow/Start node. Add exactly one.",
        ))
    elif len(starts) > 1:
        issues.append(ValidationIssue(
            severity="error", code="MULTIPLE_START",
            message=f"Workflow has {len(starts)} flow/Start nodes; exactly one is required.",
        ))
    if len(ends) == 0:
        issues.append(ValidationIssue(
            severity="error", code="NO_END",
            message="Workflow has no flow/End node. Add at least one.",
        ))
    return issues


def _check_unique_ids(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen_ids: set[int] = set()
    for n in wf.graph.nodes:
        if n.id in seen_ids:
            issues.append(ValidationIssue(
                severity="error", code="DUPLICATE_NODE_ID",
                message=f"Duplicate node id {n.id}. Every node id must be unique.",
                node_id=n.id,
            ))
        seen_ids.add(n.id)
    seen_links: set[int] = set()
    for link in wf.graph.links:
        if link.id in seen_links:
            issues.append(ValidationIssue(
                severity="error", code="DUPLICATE_LINK_ID",
                message=f"Duplicate link id {link.id}. Every link id must be unique.",
            ))
        seen_links.add(link.id)
    return issues


def _check_link_endpoints(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    node_ids = {n.id for n in wf.graph.nodes}
    for link in wf.graph.links:
        if link.origin_id not in node_ids:
            issues.append(ValidationIssue(
                severity="error", code="DANGLING_LINK_ORIGIN",
                message=(
                    f"Link {link.id} originates at node {link.origin_id} "
                    "which does not exist in the graph."
                ),
            ))
        if link.target_id not in node_ids:
            issues.append(ValidationIssue(
                severity="error", code="DANGLING_LINK_TARGET",
                message=(
                    f"Link {link.id} targets node {link.target_id} "
                    "which does not exist in the graph."
                ),
            ))
    return issues


def _check_required_properties(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for n in wf.graph.nodes:
        required = _REQUIRED_PROPERTIES.get(n.type, ())
        for key in required:
            if key == "liquid_class" and n.properties.get("liquid_class_unresolved") is not None:
                # The catalog check validates the structured unresolved marker.
                continue
            if key not in n.properties or n.properties[key] is None or n.properties[key] == "":
                issues.append(ValidationIssue(
                    severity="error", code="MISSING_PROPERTY",
                    message=f"Node of type {n.type} has an unresolved required property '{key}'. Obtain the scientist's value; do not guess.",
                    node_id=n.id,
                ))
    return issues


def _check_manual_wait(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for node in wf.graph.nodes:
        if node.type not in {"system/Manual", "system/Wait"}:
            continue
        value = node.properties.get("duration_s")
        if value is None:
            continue
        try:
            valid = math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            issues.append(ValidationIssue(
                severity="error", code="INVALID_DURATION", node_id=node.id,
                message="Duration must be a positive finite number of seconds. Obtain missing values from the scientist.",
            ))
    return issues


def _check_location_sanity(
    wf: DraftedWorkflow,
    strict_deck: bool,
) -> list[ValidationIssue]:
    """Every location property must be 1-9.

    If ``strict_deck`` is True, locations referenced by nodes that expect
    labware (PickPlace, Aspirate, Dispense, ReadBarcode, ...) must also
    exist in ``wf.deck``. We leave this off by default because a draft
    workflow may legitimately prepare a deck stack up-front (tip boxes
    starting empty, destacked plates appearing at run time) — deferring
    deep physical checks to the runtime's live deck state.
    """
    issues: list[ValidationIssue] = []
    for n in wf.graph.nodes:
        for key in _LOCATION_PROPERTY_KEYS:
            if key not in n.properties:
                continue
            loc = _as_location_int(n.properties[key])
            if loc is None:
                continue  # iter: / var: — skip
            if not 1 <= loc <= 9:
                issues.append(ValidationIssue(
                    severity="error", code="LOCATION_OUT_OF_RANGE",
                    message=(
                        f"Property '{key}' on node {n.id} ({n.type}) references "
                        f"location {loc}; only 1-9 are valid deck positions."
                    ),
                    node_id=n.id,
                ))
                continue
            if strict_deck and key in ("pick_location", "source_location") and str(loc) not in wf.deck:
                issues.append(ValidationIssue(
                    severity="warning", code="LOCATION_EMPTY",
                    message=(
                        f"Node {n.id} ({n.type}) picks from location {loc} "
                        "but the deck configuration has nothing there at "
                        "workflow start. This may be fine if an upstream "
                        "node places labware there first; otherwise a "
                        "runtime error will fire."
                    ),
                    node_id=n.id,
                ))
    return issues


def _check_volume_sanity(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for n in wf.graph.nodes:
        if n.type not in ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix"):
            continue
        vol = n.properties.get("volume")
        # Allow var: / iter: string placeholders.
        if isinstance(vol, str) and (vol.startswith("var:") or vol.startswith("iter:")):
            continue
        if vol is None:
            continue  # _check_required_properties already caught this
        try:
            v = float(vol)
        except (TypeError, ValueError):
            issues.append(ValidationIssue(
                severity="error", code="VOLUME_NOT_NUMERIC",
                message=f"Volume on node {n.id} is not a number: {vol!r}.",
                node_id=n.id,
            ))
            continue
        if v <= 0:
            issues.append(ValidationIssue(
                severity="error", code="VOLUME_NOT_POSITIVE",
                message=f"Volume on node {n.id} must be > 0 uL; got {v}.",
                node_id=n.id,
            ))
        elif v > 5000:
            # Only flag volumes that are almost certainly a unit error
            # (> 5 mL).  Legitimate bulk-reagent steps (e.g. 1–2 mL
            # washes, 1200 µL deep-well plates) sit between 200–5000 µL,
            # and scientific papers may describe protocols at that scale
            # even when they can't be executed on a Bravo as-is.
            issues.append(ValidationIssue(
                severity="warning", code="VOLUME_HIGH",
                message=(
                    f"Volume on node {n.id} is {v} uL (>{v/1000:.0f} mL). "
                    "Verify the unit — if the paper cited millilitres this "
                    "value needs to be converted before running."
                ),
                node_id=n.id,
            ))
    return issues


_LIQUID_TYPES = ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix")


def quarantine_unverified_liquid_classes(
    wf: DraftedWorkflow, *, context: Mapping[str, Any] | None,
) -> None:
    """Keep an unverified drafted reference out of the executable field.

    This does not select a replacement method. It only retains the model's
    wording for review, making the draft visibly unresolved. Existing saved
    workflows are unaffected; callers opt in on newly generated drafts.
    """
    machine = str(context.get("machine_id") or "") if context is not None else ""
    head = str(context.get("head_type") or "") if context is not None else ""
    identities = {
        (row.get("name"), row.get("liquid_class_id"))
        for row in context.get("liquid_classes") or []
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
        and isinstance(row.get("liquid_class_id"), str)
        and row.get("machine_id") == machine and row.get("head_type") == head
    } if context is not None else set()
    for node in wf.graph.nodes:
        if node.type not in _LIQUID_TYPES:
            continue
        props = node.properties
        if props.get("liquid_class_unresolved") is not None:
            continue
        name = props.get("liquid_class")
        class_id = props.get("liquid_class_id")
        if isinstance(name, str) and isinstance(class_id, str) and (name, class_id) in identities:
            continue
        requested = str(name or class_id or props.get("reagent_text") or "").strip()
        if not requested:
            continue  # Missing-property validation will report this.
        props["liquid_class"] = ""
        props.pop("liquid_class_id", None)
        props["liquid_class_unresolved"] = {
            "requested_reference": requested,
            "reason": (
                f"No exact name/id identity for selected machine {machine} and head {head}; "
                "the drafted reference needs operator review."
                if context is not None else
                "No selected machine/head catalog was supplied to verify the drafted reference."
            ),
        }
        # Keep the model's original wording visible without claiming it was
        # present in an external source passage.
        if not props.get("reagent_text") and isinstance(name, str) and name.strip():
            props["reagent_text"] = name


def _check_catalog_references(
    wf: DraftedWorkflow, *, context: Mapping[str, Any] | None,
    require_catalog: bool,
) -> list[ValidationIssue]:
    """Keep reagent intent distinct from an executable class identity.

    A missing catalog is an error only for generation paths that explicitly
    require grounding. Legacy standalone validation remains usable offline.
    """
    issues: list[ValidationIssue] = []
    machine = str(context.get("machine_id") or "") if context is not None else ""
    head = str(context.get("head_type") or "") if context is not None else ""
    catalog = [row for row in context.get("liquid_classes") or []
               if isinstance(row, Mapping) and row.get("machine_id") == machine
               and row.get("head_type") == head] if context is not None else []
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in catalog:
        if isinstance(row.get("name"), str) and row["name"]:
            by_name.setdefault(row["name"], []).append(row)

    selected_tips = {item.tip_definition_id for stack in wf.deck.values()
                     for item in stack if item.tip_definition_id}
    if require_catalog and not selected_tips and any(
        node.type in _LIQUID_TYPES for node in wf.graph.nodes
    ):
        issues.append(ValidationIssue(
            "warning", "UNRESOLVED_TIP_SELECTION",
            "Liquid actions have no selected catalog tip definition on the draft deck.",
        ))
    tips_by_id = (
        {str(row.get("tip_id")): row for row in context.get("tip_definitions") or []
         if isinstance(row, Mapping) and row.get("tip_id")}
        if context is not None else {}
    )
    for node in wf.graph.nodes:
        if node.type not in _LIQUID_TYPES:
            continue
        props = node.properties
        name = props.get("liquid_class")
        class_id = props.get("liquid_class_id")
        unresolved = props.get("liquid_class_unresolved")
        if unresolved is not None:
            valid_marker = (isinstance(unresolved, Mapping)
                            and isinstance(unresolved.get("requested_reference"), str)
                            and unresolved["requested_reference"].strip()
                            and isinstance(unresolved.get("reason"), str)
                            and unresolved["reason"].strip())
            if not valid_marker or name not in ("", None) or class_id not in ("", None):
                issues.append(ValidationIssue(
                    "error", "INVALID_UNRESOLVED_LIQUID_CLASS",
                    "An unresolved liquid class needs an empty class/name ID and "
                    "nonempty requested_reference and reason strings.", node.id,
                ))
                continue
            if not isinstance(props.get("reagent_text"), str) or not props["reagent_text"].strip():
                issues.append(ValidationIssue(
                    "error", "MISSING_REAGENT_TEXT",
                    "Preserve the reagent/material wording in reagent_text.", node.id,
                ))
                continue
            issues.append(ValidationIssue(
                "warning", "UNRESOLVED_LIQUID_CLASS",
                f"Requested {unresolved['requested_reference']!r} has no selected "
                f"executable class: {unresolved['reason']}", node.id,
            ))
            continue
        if not isinstance(name, str) or not name:
            continue  # The required-property check reports this omission.
        if context is None:
            if require_catalog:
                issues.append(ValidationIssue(
                    "error", "LIQUID_CATALOG_UNAVAILABLE",
                    f"Liquid class {name!r} cannot be verified without a selected machine/head catalog; "
                    "mark it unresolved and keep reagent_text.", node.id,
                ))
            continue
        matches = by_name.get(name, [])
        if not matches:
            issues.append(ValidationIssue(
                "error", "UNKNOWN_LIQUID_CLASS",
                f"Liquid class {name!r} is absent for {machine} / {head}; "
                "preserve these words as reagent_text and mark the class unresolved.", node.id,
            ))
            continue
        if not isinstance(class_id, str) or not class_id:
            issues.append(ValidationIssue(
                "error", "MISSING_LIQUID_CLASS_ID",
                f"Liquid class {name!r} needs its exact catalog liquid_class_id.", node.id,
            ))
            continue
        matches = [row for row in matches if row.get("liquid_class_id") == class_id]
        if len(matches) != 1:
            issues.append(ValidationIssue(
                "error", "AMBIGUOUS_LIQUID_CLASS",
                f"Liquid class {name!r} needs the exact matching liquid_class_id "
                "for this machine/head.", node.id,
            ))
            continue
        selected = matches[0]
        class_tip = selected.get("tip_id")
        if class_tip and selected_tips and class_tip not in selected_tips:
            issues.append(ValidationIssue(
                "error", "LIQUID_CLASS_TIP_MISMATCH",
                f"Liquid class {name!r} is bound to tip {class_tip!r}, absent "
                "from this draft's selected tip boxes.", node.id,
            ))
        class_capacity = selected.get("tip_capacity_ul")
        if selected_tips and isinstance(class_capacity, (int, float)) and not isinstance(class_capacity, bool):
            capacities = {tips_by_id[tip].get("capacity_ul") for tip in selected_tips
                          if tip in tips_by_id}
            if capacities and class_capacity not in capacities:
                issues.append(ValidationIssue(
                    "error", "LIQUID_CLASS_TIP_CAPACITY_MISMATCH",
                    f"Liquid class {name!r} records a {class_capacity:g} µL tip "
                    "capacity, which does not match any selected tip.", node.id,
                ))

    if context is None:
        if require_catalog and selected_tips:
            issues.append(ValidationIssue(
                "error", "TIP_CATALOG_UNAVAILABLE",
                "Selected tip IDs cannot be verified without a machine/head catalog.",
            ))
        return issues
    labware = {str(row.get("id")): row for row in context.get("labware") or []
               if isinstance(row, Mapping) and row.get("id")}
    pairs = {(str(row.get("labware_id")), str(row.get("tip_definition_id"))): row
             for row in context.get("tipbox_choices") or []
             if isinstance(row, Mapping) and row.get("labware_id")
             and row.get("tip_definition_id")}
    for slot, stack in wf.deck.items():
        for item in stack:
            row = labware.get(item.labware_id)
            if row is None:
                issues.append(ValidationIssue(
                    "error", "UNKNOWN_CATALOG_LABWARE",
                    f"Deck location {slot} names labware {item.labware_id!r} absent "
                    "from the selected catalog.",
                ))
                continue
            is_tip_box = "tip_box" in {row.get("base_class"), row.get("kind")}
            if item.tip_definition_id and not is_tip_box:
                issues.append(ValidationIssue(
                    "error", "TIP_ID_ON_NON_TIPBOX",
                    f"Deck location {slot} assigns a tip ID to non-tip-box labware.",
                ))
            elif is_tip_box and item.tip_definition_id and (
                item.labware_id, item.tip_definition_id,
            ) not in pairs:
                issues.append(ValidationIssue(
                    "error", "INCOMPATIBLE_TIPBOX_TIP_PAIR",
                    f"Tip {item.tip_definition_id!r} is not a catalog-compatible "
                    f"pair with rack {item.labware_id!r} for {head}.",
                ))
            elif is_tip_box and item.tip_definition_id and not pairs[
                item.labware_id, item.tip_definition_id,
            ].get("execution_ready"):
                issues.append(ValidationIssue(
                    "error", "TIPBOX_NOT_EXECUTION_READY",
                    f"Tip {item.tip_definition_id!r} and rack {item.labware_id!r} "
                    "are catalog-compatible but still lack required tip geometry.",
                ))
            elif is_tip_box and not item.tip_definition_id:
                issues.append(ValidationIssue(
                    "warning", "UNRESOLVED_TIP_SELECTION",
                    f"Tip box at location {slot} has no selected catalog tip ID.",
                ))
    return issues


def _check_tips_lifecycle(wf: DraftedWorkflow) -> list[ValidationIssue]:
    """Every liquid action MUST be bracketed by tips on / tips off.

    Two checks:

    1. Coarse — if ANY aspirate/dispense/mix exists in the workflow, at
       least one tips/TipsOn AND at least one tips/TipsOff MUST exist
       somewhere in the graph. Missing either is a hard error.
    2. Linear-flow walk from Start, tracking tip state along each path.
       Hitting a liquid action while tips_on is False is a hard error;
       that catches the case of a workflow that happens to have a
       TipsOn node but in the wrong place (e.g. after the dispense).

    Loops and branches: when the flow enters a Loop body, the initial
    state at the body's entry is whatever it was at the Loop node; the
    walker treats the loop body as an unrolled linear segment for
    checking purposes. This misses some clever cross-iteration tip
    reuse patterns but catches every case of "forgot tips entirely"
    and "put them in the wrong order", which is the 99% failure mode.
    """
    issues: list[ValidationIssue] = []
    nodes_by_id = {n.id: n for n in wf.graph.nodes}
    liquid_ids = [n.id for n in wf.graph.nodes if n.type in _LIQUID_TYPES]
    if not liquid_ids:
        return issues  # no liquid actions → tips not required

    has_tips_on = any(n.type == "tips/TipsOn" for n in wf.graph.nodes)
    has_tips_off = any(n.type == "tips/TipsOff" for n in wf.graph.nodes)
    if not has_tips_on:
        issues.append(ValidationIssue(
            severity="error", code="MISSING_TIPS_ON",
            message=(
                "The workflow has aspirate/dispense/mix nodes but no "
                "tips/TipsOn node. Add a tips/TipsOn before the first "
                "liquid action."
            ),
        ))
    if not has_tips_off:
        issues.append(ValidationIssue(
            severity="error", code="MISSING_TIPS_OFF",
            message=(
                "The workflow has aspirate/dispense/mix nodes but no "
                "tips/TipsOff node. Add a tips/TipsOff after the last "
                "liquid action so tips are ejected."
            ),
        ))

    # Linear walk (flow-link adjacency, -1 type links only).
    adj: dict[int, list[int]] = {}
    for link in wf.graph.links:
        if link.link_type != -1:
            continue
        adj.setdefault(link.origin_id, []).append(link.target_id)

    start_nodes = [n for n in wf.graph.nodes if n.type == "flow/Start"]
    if not start_nodes:
        return issues
    # Walk once, tracking tip state per visited node. State transitions:
    #   TipsOn  -> tips_on = True
    #   TipsOff -> tips_on = False
    # A node visited twice with conflicting states is flagged once only.
    state: dict[int, bool] = {}  # node_id -> tips_on at entry
    stack: list[tuple[int, bool]] = [(start_nodes[0].id, False)]
    reported: set[int] = set()
    while stack:
        nid, tips_on = stack.pop()
        prior = state.get(nid)
        if prior is True and tips_on is False:
            # Conflicting states across paths — we don't try to reconcile;
            # if any path has tips off at a liquid action, we'll flag it.
            tips_on = False
        elif prior is False and tips_on is True:
            tips_on = False
        elif prior is not None:
            continue  # already visited with same state
        state[nid] = tips_on
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        if node.type in _LIQUID_TYPES and not tips_on and nid not in reported:
            issues.append(ValidationIssue(
                severity="error", code="LIQUID_WITHOUT_TIPS",
                message=(
                    f"Node {nid} ({node.type}) executes without tips attached. "
                    "Insert a tips/TipsOn upstream of this node on the flow path."
                ),
                node_id=nid,
            ))
            reported.add(nid)
        # Compute outgoing tip state based on this node's type.
        if node.type == "tips/TipsOn":
            out_state = True
        elif node.type == "tips/TipsOff":
            out_state = False
        else:
            out_state = tips_on
        for tgt in adj.get(nid, []):
            stack.append((tgt, out_state))
    return issues


def _check_start_end_reachability(wf: DraftedWorkflow) -> list[ValidationIssue]:
    """Warn on orphan nodes not reachable from Start via flow links.

    Script / sensor data-only connections are fine; this only walks
    flow-typed links (link_type == -1 in LiteGraph). Frame nodes have
    no flow ports and are expected to be orphans in the flow graph.
    """
    issues: list[ValidationIssue] = []
    start_nodes = [n for n in wf.graph.nodes if n.type == "flow/Start"]
    if not start_nodes:
        return issues  # already reported by _check_start_end_counts
    # Build flow adjacency
    adj: dict[int, list[int]] = {}
    for link in wf.graph.links:
        if link.link_type != -1:
            continue
        adj.setdefault(link.origin_id, []).append(link.target_id)
    reachable: set[int] = set()
    stack = [start_nodes[0].id]
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        for tgt in adj.get(nid, []):
            stack.append(tgt)
    for n in wf.graph.nodes:
        if n.type == "flow/Frame":
            continue  # Frames have no flow ports by design
        if n.id not in reachable:
            issues.append(ValidationIssue(
                severity="warning", code="ORPHAN_NODE",
                message=(
                    f"Node {n.id} ({n.type}) is not reachable from flow/Start "
                    "via flow links. It will never execute."
                ),
                node_id=n.id,
            ))
    return issues


# ── Entrypoint ────────────────────────────────────────────────────────


# ── Citation coverage (Phase 3: PDF → workflow pipeline) ──────────────


_STRUCTURAL_NODE_TYPES: tuple[str, ...] = (
    "flow/Start", "flow/End", "flow/Loop", "flow/IfElse", "flow/Frame",
)


def _check_citations(
    wf: DraftedWorkflow,
    *,
    valid_fact_ids: set[str] | None,
    valid_paragraph_ids: set[str] | None,
) -> list[ValidationIssue]:
    """When the drafter was fed a PaperFacts list (Pass 2 of the PDF
    pipeline), every non-structural node MUST carry a source_citation
    whose fact_id and paragraph_id both reference that fact list.

    No-op when both id sets are None — that means this validator was
    invoked on a NL-drafted workflow where citations aren't expected.
    """
    if valid_fact_ids is None and valid_paragraph_ids is None:
        return []
    issues: list[ValidationIssue] = []
    facts = valid_fact_ids or set()
    paras = valid_paragraph_ids or set()
    for n in wf.graph.nodes:
        if n.type in _STRUCTURAL_NODE_TYPES:
            continue
        c = n.source_citation
        if c is None:
            issues.append(ValidationIssue(
                severity="error", code="MISSING_CITATION",
                message=(
                    f"Non-structural node of type {n.type} has no "
                    "source_citation. Every drafted-from-paper node "
                    "must cite the fact_id + paragraph_id it came from."
                ),
                node_id=n.id,
            ))
            continue
        if facts and c.fact_id and c.fact_id not in facts:
            issues.append(ValidationIssue(
                severity="error", code="UNKNOWN_FACT_ID",
                message=(
                    f"source_citation.fact_id={c.fact_id!r} on node {n.id} "
                    "is not in the facts list Pass 1 produced. Never cite "
                    "a fact that wasn't extracted."
                ),
                node_id=n.id,
            ))
        if paras and c.paragraph_id not in paras:
            issues.append(ValidationIssue(
                severity="error", code="UNKNOWN_PARAGRAPH_ID",
                message=(
                    f"source_citation.paragraph_id={c.paragraph_id!r} on "
                    f"node {n.id} is not in the paper. Use only IDs that "
                    "appear in the extracted facts list."
                ),
                node_id=n.id,
            ))
    return issues


def validate_drafted_workflow(
    wf: DraftedWorkflow,
    *,
    strict_deck: bool = False,
    valid_fact_ids: set[str] | None = None,
    valid_paragraph_ids: set[str] | None = None,
    catalog_context: Mapping[str, Any] | None = None,
    require_catalog: bool = False,
) -> list[ValidationIssue]:
    """Run every check on a drafted workflow.

    Args:
        strict_deck: defaults to False. When True, every location
            referenced by a pick/aspirate/etc. node must already have
            labware on the deck at workflow start. Useful from test
            harnesses; too strict for mid-workflow drafts.
        valid_fact_ids / valid_paragraph_ids: supplied by Pass 2 of the
            PDF pipeline. When both are present, every non-structural
            node must carry a source_citation whose ids reference these
            sets. When both are None, citation checking is skipped
            (NL-prompt drafts don't require citations).
        catalog_context: selected machine/head catalog snapshot. Exact
            class identities and compatible tip/rack pairs are checked
            when provided.
        require_catalog: reject executable class/tip selections when no
            selected machine/head catalog was supplied.
    """
    issues: list[ValidationIssue] = []
    issues.extend(_check_start_end_counts(wf))
    issues.extend(_check_unique_ids(wf))
    issues.extend(_check_link_endpoints(wf))
    issues.extend(_check_required_properties(wf))
    issues.extend(_check_manual_wait(wf))
    issues.extend(_check_location_sanity(wf, strict_deck=strict_deck))
    issues.extend(_check_volume_sanity(wf))
    issues.extend(_check_catalog_references(
        wf, context=catalog_context, require_catalog=require_catalog,
    ))
    issues.extend(_check_tips_lifecycle(wf))
    issues.extend(_check_start_end_reachability(wf))
    issues.extend(_check_citations(
        wf,
        valid_fact_ids=valid_fact_ids,
        valid_paragraph_ids=valid_paragraph_ids,
    ))
    return issues


def format_issues_for_repair(issues: list[ValidationIssue]) -> str:
    """Render an issues list as a repair prompt fragment.

    Used by the drafter's retry loop: the LLM sees the previous draft
    plus this block and is asked to fix each issue. Only ERROR-severity
    issues are included; warnings don't block the draft.
    """
    errors = [i for i in issues if i.severity == "error"]
    if not errors:
        return ""
    lines = ["The previous draft had the following problems — please fix them:"]
    for i in errors:
        lines.append(f"  - {i}")
    return "\n".join(lines)
