"""Assemble the system prompt for the LLM drafter.

The prompt has seven sections, in this order:

1. Role framing + hard rules ("never invent a node type", etc).
2. Node-type catalog — every supported type and its required properties.
3. Labware catalog excerpt — id → name → base_class, filtered to
   microplates + tip boxes by default to keep context tight.
4. Deck state — the workflow currently open in the designer (so the LLM
   can refer to "the 384 PP plate at loc 7" by exact labware_id).
5. Library snippets — the pre-authored snippet registry so the LLM
   prefers the Ask-Operator / Barcode-fallback / Kaldor-send patterns
   over open-coding similar logic.
6. The selected machine/head execution catalog, when supplied.
7. Few-shot exemplars — full workflows showing the target JSON shape.
8. The operator's natural-language description.

Assembly is deterministic. The resulting prompt is ~3-4k tokens before
exemplars, ~5-7k including them — well within the 200k window of
Claude 3.5 / Sonnet 4 / GPT-4o.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from pybravo.workflow.drafter.schema import SUPPORTED_NODE_TYPES

# ── Hand-written node-catalog docstrings ──────────────────────────────
# Grouped so the LLM sees the affordance structure, not a flat list.
# Properties are what the executor reads off the node at dispatch time
# (see NODE_TYPE_MAP in pybravo/workflow/executor.py + _build_task_params
# at the same site).

_NODE_CATALOG: tuple[tuple[str, dict[str, Any]], ...] = (
    # (type, descriptor)
    ("flow/Start", {
        "desc": "Entry point. Every workflow has exactly one. No properties. Output slot 0 is flow.",
        "required": (),
        "optional": (),
    }),
    ("flow/End", {
        "desc": "Exit point. At least one required. No properties. Input slot 0 is flow.",
        "required": (),
        "optional": (),
    }),
    ("flow/Loop", {
        "desc": (
            "Executes its body flow (output slot 0) `count` times, then "
            "continues via `done` flow (output slot 1). Nodes inside the "
            "body can reference the current iteration index via `iter:v1,v2,...` "
            "property values (expands to the Nth comma-separated value on "
            "iteration N-1)."
        ),
        "required": ("count",),
        "optional": (),
    }),
    ("flow/IfElse", {
        "desc": (
            "Branches flow on a condition. True path = output slot 0, "
            "False path = output slot 1. The `data` input accepts the "
            "value to test; the `condition` property is a Python "
            "expression evaluated against it."
        ),
        "required": ("condition",),
        "optional": (),
    }),
    ("flow/Frame", {
        "desc": (
            "Visual-only container that groups related nodes. No flow "
            "ports. `properties.member_ids` is the list of node ids it "
            "wraps. Used to tidy up the canvas; not traversed at runtime."
        ),
        "required": ("title", "member_ids"),
        "optional": ("color", "collapsed", "expanded_bbox"),
    }),
    ("plate/PickPlace", {
        "desc": "Grip + move a plate from one deck location to another.",
        "required": ("pick_location", "place_location"),
        "optional": (),
    }),
    ("plate/Stack", {
        "desc": (
            "Move a plate from `source_location` onto a growing stack whose "
            "base is `base_location`. `target_location` names the final "
            "resting position; typically equal to base_location unless "
            "the stack is being built elsewhere."
        ),
        "required": ("source_location", "base_location", "target_location"),
        "optional": (),
    }),
    ("plate/Destack", {
        "desc": (
            "Remove the top plate from the stack at `source_location` and "
            "place it at `destination_location`. `target_location` is the "
            "final resting position (usually equals destination_location)."
        ),
        "required": ("source_location", "destination_location", "target_location"),
        "optional": (),
    }),
    ("plate/Delid", {
        "desc": "Remove a lid from the plate at `location`.",
        "required": ("location",),
        "optional": (),
    }),
    ("plate/Relid", {
        "desc": "Replace a previously-removed lid on the plate at `location`.",
        "required": ("location",),
        "optional": (),
    }),
    ("liquid/Aspirate", {
        "desc": (
            "Aspirate `volume` uL from the plate at `location` using the "
            "exact catalog `liquid_class`. Keep the source material words in "
            "`reagent_text` separately. `anchor` selects the starting well "
            "(e.g. \"A1\"); optional `quadrant` selects a 1536 quadrant."
        ),
        "required": ("location", "volume", "liquid_class"),
        "optional": (
            "liquid_class_id", "liquid_class_unresolved", "reagent_text", "reagent_family",
            "pipette_technique", "pre_aspirate_volume", "post_aspirate_volume",
            "distance_from_bottom", "dynamic_tip_extension", "tip_touch",
            "anchor", "quadrant", "wells",
        ),
    }),
    ("liquid/Dispense", {
        "desc": "Dispense `volume` uL at `location`. Same parameter shape as Aspirate.",
        "required": ("location", "volume", "liquid_class"),
        "optional": (
            "liquid_class_id", "liquid_class_unresolved", "reagent_text", "reagent_family",
            "pipette_technique", "blowout_volume", "empty_tips",
            "distance_from_bottom", "dynamic_tip_retraction", "tip_touch",
            "anchor", "quadrant", "wells",
        ),
    }),
    ("liquid/Mix", {
        "desc": "Aspirate + dispense in place to mix.",
        "required": ("location", "volume", "liquid_class"),
        "optional": ("cycles", "distance_from_bottom", "anchor",
                     "liquid_class_id", "liquid_class_unresolved", "reagent_text", "reagent_family"),
    }),
    ("tips/TipsOn", {
        "desc": (
            "Pick up fresh tips from a clean supply box. `location` is the tip-box deck "
            "position (use `iter:1,2,3,4` to cycle through multiple boxes "
            "on successive loop iterations). `head_mode` is a dict with "
            "`subset_type` (all_barrels|row|column|rectangle|single_barrel), "
            "`subset_config` (back_left|back_right|front_left|front_right), "
            "and optional `row_count`/`column_count`. `tip_anchor_row` / "
            "`tip_anchor_col` suggest the starting cells of the tip box "
            "(0-indexed; ignored when using the whole head). An anchor is "
            "not proof that fresh tips exist there."
        ),
        "required": ("location",),
        "optional": ("head_mode", "tip_anchor_row", "tip_anchor_col"),
    }),
    ("tips/TipsOff", {
        "desc": (
            "Eject used tips at `location` into a designated spent-tip return "
            "box or compatible tip waste. Returned tips remain spent and "
            "must not be counted as clean supply. The head_mode is INHERITED from the "
            "most recent upstream Tips On node (head can't reconfigure "
            "mid-cycle), so DO NOT emit `head_mode` on Tips Off. "
            "`tip_anchor_row` / `tip_anchor_col` choose which cells of "
            "the destination box receive the tips (0-indexed); these are "
            "starting hints, not a new fresh-tip inventory."
        ),
        "required": ("location",),
        "optional": ("tip_anchor_row", "tip_anchor_col"),
    }),
    ("sensor/ReadBarcode", {
        "desc": (
            "Read the barcode of the plate currently at `location`. "
            "`store_as` optionally names a vars[...] key to write the "
            "scanned barcode into (e.g. store_as=\"plateFivebc\" makes "
            "the scanned value available as vars[\"plateFivebc\"] to "
            "downstream Script nodes)."
        ),
        "required": ("location",),
        "optional": ("store_as",),
    }),
    ("sensor/ScanStackHeight", {
        "desc": (
            "Probe a stack at `location` with the gripper plate sensor "
            "to verify the expected number of plates. `expected_count` "
            "triggers an operator-prompt retry/ignore/abort modal if the "
            "measured count differs."
        ),
        "required": ("location",),
        "optional": ("expected_count", "store_as"),
    }),
    ("logic/Script", {
        "desc": (
            "Runs user-authored Python at this point in the flow. Sees "
            "`data` (upstream input), `vars` (blackboard dict), `plates` "
            "(live deck accessor), `result` (assign to publish), `log`, "
            "`prompt_user(msg, default=\"\")` (opens an operator-input "
            "modal — set timeout=0 when using it), and any helpers from "
            "the workflow Library. `store_as` optionally writes the "
            "script's `result` into vars[that_key]."
        ),
        "required": ("script",),
        "optional": ("timeout", "store_as"),
    }),
    ("system/Initialize", {
        "desc": "Initialize the robot (home axes, verify hardware). Usually first node after Start.",
        "required": (),
        "optional": (),
    }),
    ("system/Home", {
        "desc": "Home the specified axes.",
        "required": (),
        "optional": ("axes",),
    }),
    ("system/DockGripper", {
        "desc": "Return the gripper to its nesting position.",
        "required": (),
        "optional": (),
    }),
    ("system/Manual", {
        "desc": "Pause for the scientist to perform the specified manual or external-instrument operation and confirm completion. No generated Python is needed.",
        "required": ("message",),
        "optional": ("duration_s",),
    }),
    ("system/Wait", {
        "desc": "Wait for a positive number of seconds. This does not control incubation temperature or external equipment.",
        "required": ("duration_s",),
        "optional": (),
    }),
)


def _format_node_catalog() -> str:
    lines = [
        "## Node type catalog",
        "Every `type` value MUST be one of these exact strings — do not "
        "invent variants. Required properties MUST be present; optional "
        "ones can be omitted.",
        "",
    ]
    for type_name, d in _NODE_CATALOG:
        lines.append(f"### `{type_name}`")
        lines.append(d["desc"])
        if d["required"]:
            lines.append(f"**Required:** {', '.join(d['required'])}")
        if d["optional"]:
            lines.append(f"**Optional:** {', '.join(d['optional'])}")
        lines.append("")
    return "\n".join(lines)


# ── Labware catalog excerpt ───────────────────────────────────────────


def _load_labware_catalog(
    catalog_path: Path | None = None,
    base_classes: Iterable[str] = ("microplate", "tip_box"),
) -> list[dict[str, Any]]:
    """Return a filtered summary of the labware catalog.

    Only returns the fields that matter to the drafter (id / name /
    base_class / wells / is_lidded / is_sealed). Full mechanical
    dimensions are irrelevant to NL → JSON translation.
    """
    if catalog_path is None:
        catalog_path = Path(__file__).resolve().parent.parent.parent.parent / "config" / "labware_catalog.snapshot.yaml"
    if not catalog_path.exists():
        return []
    try:
        import yaml
    except ImportError:
        return []
    with catalog_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    filtered: list[dict[str, Any]] = []
    for entry in data.get("labware", []) or []:
        base = entry.get("base_class", "")
        if base not in base_classes:
            continue
        filtered.append({
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "base_class": base,
            "wells": entry.get("wells", 0),
        })
    return filtered


def _format_labware_catalog(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = [
        "## Labware catalog (available labware_ids for deck placement)",
        "Use these exact `labware_id` strings in any deck entry. Do not "
        "invent new ones — the executor looks them up by id in the "
        "catalog at run time.",
        "",
        "| labware_id | name | base_class | wells |",
        "|---|---|---|---|",
    ]
    for e in entries:
        lines.append(f"| `{e['id']}` | {e['name']} | {e['base_class']} | {e['wells']} |")
    lines.append("")
    return "\n".join(lines)


def _format_execution_catalog(context: Mapping[str, Any] | None) -> str:
    """Expose exact identities for the selected machine/head, never aliases."""
    if context is None:
        return (
            "## Execution catalog\nNo selected machine/head catalog was supplied. "
            "All liquid classes and tip choices remain unresolved."
        )
    machine = str(context.get("machine_id") or "")
    head = str(context.get("head_type") or "")
    lines = [f"## Execution catalog for machine `{machine}` / head `{head}`",
             "Names here identify stored methods; they do not establish reagent "
             "suitability or physical loading.", "",
             "Exact liquid classes (name and id must identify the same row):"]
    classes = [row for row in context.get("liquid_classes") or []
               if isinstance(row, Mapping) and row.get("name") and row.get("liquid_class_id")
               and row.get("machine_id") == machine and row.get("head_type") == head]
    if classes:
        for row in sorted(classes, key=lambda item: (str(item["name"]), str(item["liquid_class_id"]))):
            lines.append(
                f"- {row['name']!r} | id={row['liquid_class_id']} | "
                f"tip_id={row.get('tip_id') or '(unbound)'} | "
                f"tip_capacity_ul={row.get('tip_capacity_ul')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "Compatible catalog tip-box/tip pairs for this head:"])
    choices = [row for row in context.get("tipbox_choices") or []
               if isinstance(row, Mapping) and row.get("labware_id")
               and row.get("tip_definition_id")]
    if choices:
        for row in sorted(choices, key=lambda item: (
            str(item["labware_id"]), str(item["tip_definition_id"]),
        )):
            lines.append(
                f"- labware_id={row['labware_id']} | tip_definition_id="
                f"{row['tip_definition_id']} | tip_capacity_ul="
                f"{row.get('tip_capacity_ul')} | execution_ready="
                f"{bool(row.get('execution_ready'))}"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)


# ── Snippet-registry excerpt ──────────────────────────────────────────


def _format_snippet_registry() -> str:
    """Describe the pre-authored Script snippets the LLM can leverage.

    We paraphrase each snippet's purpose rather than dumping full code
    — the full code lives in the exemplars and in the Library on
    actually-run workflows. Prompt-token-budget matters.
    """
    try:
        from pybravo.workflow.script_snippets import get_snippets
    except ImportError:
        return ""
    snippets = get_snippets()
    if not snippets:
        return ""
    lines = [
        "## Available Script-node snippet patterns",
        "When a Script node needs to do one of these, emit the snippet's "
        "code verbatim — don't reinvent.",
        "",
    ]
    for s in snippets:
        lines.append(f"- **{s.get('label', s.get('id', '?'))}** ({s.get('category', '?')}): {s.get('description', '')}")
    lines.append("")
    return "\n".join(lines)


# ── Exemplars ─────────────────────────────────────────────────────────


_EXEMPLARS_DIR = Path(__file__).resolve().parent / "exemplars"


def _load_exemplars() -> list[dict[str, Any]]:
    """Load all exemplar workflows, sorted by filename so order is stable."""
    if not _EXEMPLARS_DIR.exists():
        return []
    exemplars: list[dict[str, Any]] = []
    for p in sorted(_EXEMPLARS_DIR.glob("*.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                exemplars.append(json.load(f))
        except Exception:
            continue
    return exemplars


def _format_exemplars(exemplars: list[dict[str, Any]]) -> str:
    if not exemplars:
        return ""
    lines = [
        "## Exemplar workflows (target JSON shape)",
        "Your output MUST match this JSON structure exactly. These are "
        "format examples — mimic field names, nesting, link tuple "
        "ordering, node positioning style, etc. Their catalog choices "
        "are not evidence for the selected machine/head.",
        "",
    ]
    for ex in exemplars:
        name = ex.get("name", "unnamed")
        desc = ex.get("description", "")
        lines.append(f"### Example: {name}")
        if desc:
            lines.append(f"*{desc}*")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(ex, indent=2))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# ── Hard rules ────────────────────────────────────────────────────────


_ROLE_AND_RULES = """You are the pyBravo workflow drafter. Your job is to
translate a lab operator's natural-language description of an experiment
into a valid pyBravo workflow JSON.

## Hard rules (non-negotiable)

1. The `type` field of every node MUST be one of the exact strings in
   the "Node type catalog" section below. Never invent a new type,
   never misspell an existing one, never use a type that isn't listed.
2. Every workflow MUST have exactly one `flow/Start` node and at least
   one `flow/End` node. Flow MUST originate at Start and reach End.
3. Deck-location property values (`location`, `pick_location`,
   `source_location`, etc.) MUST be integers in 1-9, OR the special
   string `iter:v1,v2,...` (for loop iteration), OR `var:NAME` (for
   blackboard lookup). Never use 0, 10+, or arbitrary strings.
4. Volumes are in microliters (uL), as numbers (int or float).
   A liquid action must fit the selected head, tip and class limits.
   Never emit mL or silently change a source quantity to fit a limit.
5. `labware_id` values MUST come from the Labware catalog section. If
   the operator names a labware not in the catalog, omit its deck entry
   and describe the unresolved labware in `description`. Never substitute
   a similar plate or invent an ID.
6. Node `id` and link `id` values MUST be unique positive integers
   within the graph.
7. Link tuples connect slot 0 of one node's outputs to slot 0 of the
   next node's inputs, using `link_type: -1` for flow. (Typed data
   slots use "string", "number", etc.)
8. If the operator asks for something you cannot do (e.g. a non-
   existent node type, a location outside 1-9, an unsupported head
   mode), preserve it as an explicitly described manual handoff or
   unresolved requirement in `description`. Never silently replace it
   with a different operation or invent new schema fields.
9. Prefer the pre-authored Script snippets (see catalog below) over
   writing new Python. Snippets are vetted; novel code isn't.
10. `pos` can be left at [0, 0] for all nodes — the designer's
    Auto-Arrange button will lay them out properly.
11. Every `liquid/Aspirate`, `liquid/Dispense`, and `liquid/Mix` MUST
    be flanked by tips: a `tips/TipsOn` before the first liquid action
    in a tip lifecycle, and a `tips/TipsOff` after the last one. The
    only exception is when the operator EXPLICITLY says "skip tips"
    or "tips already on" — otherwise always include them. If the
    operator doesn't name a tip-box location, leave `location` null and
    explain the missing setup in `description`. Never assume slot 1.
12. Missing numeric quantities, locations, or well mappings MUST remain
    null and be described as unresolved in `description`. A scientifically
    meaningful missing value is not a default. Preserve manual steps and
    external instrument handoffs.
13. `liquid_class` is an executable catalog method name, NOT a reagent or
    material label. Use an exact name from the selected machine/head catalog
    below, with its matching `liquid_class_id`; never copy liquid-class names
    from examples unless they appear in that catalog. If no exact,
    applicable class is established, set `liquid_class` to an empty string
    and `liquid_class_unresolved` to an object with nonempty
    `requested_reference` and `reason` strings. Preserve source wording in
    `reagent_text` separately; set `reagent_family` only when the source states it.
    Describe the unresolved choice in `description`. This is an unexecutable
    draft that must fail strict preflight before robot dispatch. A matching
    class name is not proof of reagent suitability.
14. A proposed tip box must use a `labware_id` + `tip_definition_id` pair
    listed for the selected head with `execution_ready=true`. Leave the tip
    ID empty and explain the unresolved choice if no ready pair is
    established. Do not infer a tip from the rack name, capacity, or an
    exemplar.
15. In a repeated operation that requires fresh tips each iteration,
    include a complete pickup/use/eject lifecycle within the loop body.
    Treat every returned set as spent even if it is physically present in
    a rack. Keep the clean pickup supply distinct from a spent-tip return
    rack or compatible waste; never return to a rack that a later
    `tips/TipsOn` will treat as clean, and never repeat a pickup/return
    anchor pair as if those returned tips became fresh again. A catalog
    rack or numeric loop count does not establish actual fresh inventory.
    Propose only catalog-compatible rack roles and locations supported by
    the supplied deck. Set `deck[location][item].tipbox_fill_state` to
    `full` for proposed clean supply and `empty` for a proposed return
    rack. Preserve that initial state in the output. Leave missing fresh-well count, return capacity,
    disposal location, and physical loading explicitly unresolved for
    operator confirmation. If the source calls for deliberate tip reuse,
    preserve that intent for review instead of silently labeling it fresh.
16. The saved native graph must pass mechanical readiness and strict
    physical rehearsal before review. A visual walkthrough is only an
    illustration. Resolve head/tip/rack compatibility, empty liquid-task
    locations, repeated anchors, capacity and missing geometry first.
    SuperDex checks actual simulated axis commands and stops on modeled
    collisions; never invent geometry or motion settings to bypass it.
17. When the device has a gripper, put a native `system/DockGripper`
    primitive before tip handling, after any required initialization.
    Initialization alone does not guarantee the gripper is recessed. A
    protruding finger can hit the tip rack as a subset moves across it.
    Keep docking visible in the graph; do not rely on a cosmetic animation
    or an invisible movement to supply this clearance.

## Output format

Emit ONLY a single JSON object matching the DraftedWorkflow schema.
No prose, no code fences, no commentary outside the JSON.
"""


# ── Public assembler ──────────────────────────────────────────────────


def build_system_prompt(
    *,
    current_deck: dict[str, Any] | None = None,
    catalog_context: Mapping[str, Any] | None = None,
    include_exemplars: bool = True,
) -> str:
    """Assemble the full system prompt.

    Args:
        current_deck: The deck configuration currently loaded in the
            designer (from the active tab). Forwarded to the LLM so it
            can reuse exact labware_ids already on the deck. Pass None
            to omit (the LLM gets only the catalog, not the live deck).
        catalog_context: Selected machine/head catalog from
            ``machine_context``. Omitting it leaves executable class and
            tip identities unresolved.
        include_exemplars: Skip the few-shot JSON dumps for tiny tests.

    Returns:
        A single string ready for the ``system`` role of an Anthropic
        or OpenAI chat call.
    """
    sections: list[str] = [_ROLE_AND_RULES, _format_node_catalog()]

    labware = ([{"id": row.get("id"), "name": row.get("name"),
                 "base_class": row.get("base_class"), "wells": row.get("wells")}
                for row in catalog_context.get("labware") or []
                if isinstance(row, Mapping) and row.get("id")]
               if catalog_context is not None else _load_labware_catalog())
    sections.append(_format_labware_catalog(labware))
    sections.append(_format_execution_catalog(catalog_context))

    if current_deck:
        sections.append("## Current deck configuration")
        sections.append(
            "The designer currently has this deck loaded. Reuse these "
            "exact labware entries when the operator's description "
            "refers to \"the plate at loc N\" or similar. Overwrite "
            "only when the description clearly implies a different "
            "plate there."
        )
        sections.append("```json")
        sections.append(json.dumps(current_deck, indent=2))
        sections.append("```")
        sections.append("")

    sections.append(_format_snippet_registry())

    if include_exemplars:
        sections.append(_format_exemplars(_load_exemplars()))

    sections.append(
        "Allowed node type values for the `type` field (also listed in "
        "the catalog above): "
        + ", ".join(f"`{t}`" for t in SUPPORTED_NODE_TYPES)
    )

    return "\n\n".join(s for s in sections if s)
