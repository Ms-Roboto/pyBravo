"""Build a non-executable designer diagram for a chat-derived protocol plan.

A preview shows scientific intent even when setup and quantities are missing.
It does not invoke validation, compilation, catalog lookup or robot operations.
Repeats remain visible containers with one copy of each child, so drawing the
plan never expands a model-provided repetition count into an execution graph.
"""
from __future__ import annotations

from typing import Any

from .models import ProtocolPlan, ProtocolStep

# Leave two node slots for Start and End in the 500-node designer bound.
MAX_PREVIEW_STEPS = 498
MAX_PREVIEW_DEPTH = 8
PREVIEW_NODE_TYPE = "review/ProtocolStep"
_STEP_TITLES = {
    "transfer": "Transfer", "mix": "Mix", "manual": "Manual checkpoint",
    "wait": "Wait", "move_plate": "Move plate", "destack_plate": "Destack plate",
    "stack_plate": "Stack plate", "repeat": "Repeat block",
}
_PARAMETER_FIELDS = (
    "source", "destination", "material", "source_anchor", "destination_anchor",
    "anchor", "volume_ul", "cycles", "duration_s", "destination_slot", "message",
)
_REQUIRED_FIELDS = {
    "transfer": ("source", "destination", "source_anchor", "destination_anchor", "volume_ul"),
    "mix": ("material", "anchor", "volume_ul", "cycles"),
    "manual": ("message",), "wait": ("duration_s",),
    "move_plate": ("material", "destination_slot"),
    "destack_plate": ("material", "destination_slot"),
    "stack_plate": ("material", "destination_slot"),
    "repeat": ("repeat",),
}


def _source_map(sources: Any) -> dict[str, dict]:
    if hasattr(sources, "model_dump"):
        sources = sources.model_dump()
    if isinstance(sources, dict) and "paragraphs" in sources:
        sources = sources["paragraphs"]
    if isinstance(sources, dict):
        return {str(key): ({"text": value} if isinstance(value, str) else value)
                for key, value in sources.items() if isinstance(value, (str, dict))}
    return {str(paragraph.get("id") or paragraph.get("paragraph_id")): paragraph
            for paragraph in (sources or []) if isinstance(paragraph, dict)}


def _quantity(value: int | float | None, unit: str) -> str:
    return f"{value:g} {unit}" if value is not None else f"unspecified {unit}"


def _proposed_deck(plan: ProtocolPlan) -> dict[str, list[dict]]:
    """Show stated positions in the read-only chat draft, pending validation.

    These entries never create executable tasks. Unknown catalog IDs remain
    visible as proposed names, and uncertain stack levels keep plan order until
    the scientist corrects them in Protocol Assistant.
    """
    grouped: dict[str, list[tuple[int, int | None, dict]]] = {}
    for index, material in enumerate(plan.materials):
        slot = material.deck_slot
        if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 9:
            continue
        entry = {
            "material_id": material.id,
            "labware_id": material.labware_id or "",
            "name": material.name,
            "kind": {"liquid": "sbs_plate", "tips": "tip_box", "waste": "tip_trash_bin"}[material.role],
            "proposed": True,
        }
        if material.role == "tips":
            entry["tip_definition_id"] = material.tip_definition_id or ""
            entry["tip_inventory_status"] = (
                "unconfirmed" if material.available_tips is None
                else "full" if material.available_tips == "full"
                else "empty" if material.available_tips == [] else "specified"
            )
            if material.available_tips == "full":
                entry["tipbox_fill_state"] = "full"
            elif material.available_tips == []:
                entry["tipbox_fill_state"] = "empty"
        grouped.setdefault(str(slot), []).append((index, material.stack_order, entry))
    deck = {}
    for slot, items in grouped.items():
        # The Designer and runtime use bottom-to-top array order.
        ordered = sorted(items, key=lambda item: (
            item[1] if isinstance(item[1], int) and not isinstance(item[1], bool) and item[1] >= 0 else float("inf"),
            item[0],
        ))
        deck[slot] = [entry for _, _, entry in ordered]
    return deck


def _summary(step: ProtocolStep, material_names: dict[str, str]) -> str:
    def material(identity: str | None) -> str:
        return material_names.get(identity, identity) if identity else "unspecified material"

    if step.kind == "transfer":
        summary = (f"Transfer {_quantity(step.volume_ul, 'uL/channel')} from {material(step.source)} "
                   f"({step.source_anchor or 'well unspecified'}) to {material(step.destination)} "
                   f"({step.destination_anchor or 'well unspecified'}).")
    elif step.kind == "mix":
        summary = (f"Mix {_quantity(step.volume_ul, 'uL/channel')} in {material(step.material)} "
                   f"({step.anchor or 'well unspecified'}) for {_quantity(step.cycles, 'cycles')}.")
    elif step.kind == "manual":
        summary = step.message or "Manual instructions are unspecified."
        if step.duration_s is not None:
            summary += f" Required duration: {_quantity(step.duration_s, 'seconds')}."
    elif step.kind == "wait":
        summary = f"Wait {_quantity(step.duration_s, 'seconds')}; this does not specify temperature control."
    elif step.kind in {"move_plate", "destack_plate", "stack_plate"}:
        action = {"move_plate": "Move", "destack_plate": "Destack", "stack_plate": "Stack"}[step.kind]
        summary = (f"{action} {material(step.material)} to deck slot "
                   f"{step.destination_slot if step.destination_slot is not None else 'unspecified'}.")
    else:
        summary = f"Repeat the following {len(step.steps)} child step(s) {_quantity(step.repeat, 'times')}; the body is shown once."
    if step.kind != "repeat" and step.repeat != 1:
        summary += f" Repeat {_quantity(step.repeat, 'times')}."
    return summary


def build_chat_preview(
    plan: ProtocolPlan | dict,
    session_id: str,
    revision: int,
    sources: Any = None,
) -> dict:
    """Return a marked LiteGraph workflow containing only structural review nodes.

    Missing scientific values remain missing. The proposed deck is displayed
    for visual review only; model-proposed identifiers, slots, and stacking
    levels are not validated instrument setup.
    """
    plan = plan if isinstance(plan, ProtocolPlan) else ProtocolPlan.model_validate(plan)
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Chat preview requires a session identifier.")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("Chat preview requires a positive integer revision.")
    paragraphs = _source_map(sources)
    names = {material.id: material.name for material in plan.materials}
    questions = [question.model_dump(mode="json") for question in plan.questions]
    specs: list[dict] = []

    def visit(steps: list[ProtocolStep], prefix: str, numbering: tuple[int, ...], parent: str | None) -> None:
        depth = len(numbering)
        if depth > MAX_PREVIEW_DEPTH:
            raise ValueError(f"Chat preview exceeds {MAX_PREVIEW_DEPTH} nested step levels.")
        for index, step in enumerate(steps):
            if len(specs) >= MAX_PREVIEW_STEPS:
                raise ValueError(f"Chat preview exceeds {MAX_PREVIEW_STEPS} steps; narrow the selected protocol.")
            path = f"{prefix}/{index}"
            label = ".".join(str(number) for number in (*numbering, index + 1))
            citations = []
            for paragraph_id in step.source_paragraph_ids:
                passage = paragraphs.get(paragraph_id)
                citations.append({"paragraph_id": paragraph_id,
                    "excerpt": str((passage or {}).get("text", ""))[:500],
                    "page": (passage or {}).get("page") or (passage or {}).get("page_no"),
                    "found": passage is not None})
            missing = [field for field in _REQUIRED_FIELDS[step.kind] if getattr(step, field) in (None, "")]
            if step.repeat is None and "repeat" not in missing:
                missing.append("repeat")
            if step.kind == "repeat" and not step.steps:
                missing.append("steps")
            properties = {
                "step_id": step.id, "kind": step.kind, "summary": _summary(step, names),
                "_protocol_step_id": step.id, "_protocol_path": path,
                "description": step.description, "path": path, "depth": depth,
                "parent_step_id": parent, "repeat": step.repeat,
                "parameters": {field: getattr(step, field) for field in _PARAMETER_FIELDS if getattr(step, field) is not None},
                "missing_fields": missing, "source_paragraph_ids": list(step.source_paragraph_ids),
                "source_values": [value.model_dump(mode="json") for value in step.source_values],
                "citations": citations,
                "questions": [q for q in questions if q["path"] == path or (
                    q["path"].startswith(path + "/") and "/steps/" not in q["path"][len(path):])],
            }
            if citations:
                properties["_source_citation"] = {
                    "paragraph_id": citations[0]["paragraph_id"], "excerpt": citations[0]["excerpt"],
                    "page": citations[0]["page"], "paragraph_ids": list(step.source_paragraph_ids),
                }
            specs.append({"type": PREVIEW_NODE_TYPE, "title": f"{label}. {_STEP_TITLES[step.kind]}",
                          "properties": properties, "depth": depth})
            # Preserve all supplied children visibly, even if a later validator
            # will reject children on a non-repeat step. Do not discard intent.
            if step.steps:
                visit(step.steps, path + "/steps", (*numbering, index + 1), step.id)

    visit(plan.steps, "/steps", (), None)
    specs.insert(0, {"type": "flow/Start", "title": "Protocol draft", "properties": {}, "depth": 0})
    specs.append({"type": "flow/End", "title": "End of draft", "properties": {}, "depth": 0})
    nodes, links = [], []
    next_y = 80.0
    columns = 3 if len(specs) > 8 else 1
    for index, spec in enumerate(specs):
        identity = index + 1
        size = [420, 170] if spec["type"] == PREVIEW_NODE_TYPE else [180, 70]
        if columns == 1:
            position = [80.0 + spec["depth"] * 70.0, next_y]
            next_y += size[1] + 60.0
        else:
            row, order_in_row = divmod(index, columns)
            column = order_in_row if row % 2 == 0 else columns - 1 - order_in_row
            position = [80.0 + column * 500.0 + spec["depth"] * 40.0, 80.0 + row * 230.0]
        nodes.append({"id": identity, "type": spec["type"], "title": spec["title"],
            "pos": position, "size": size,
            "order": index, "mode": 0, "properties": spec["properties"],
            "inputs": [] if index == 0 else [{"name": "flow", "type": -1, "link": index}],
            "outputs": [] if index == len(specs) - 1 else [{"name": "flow", "type": -1, "links": [identity]}]})
        if index:
            links.append([index, index, 0, identity, 0, -1])
    return {
        "name": plan.name, "description": plan.description, "deck": _proposed_deck(plan),
        "graph": {"last_node_id": len(nodes), "last_link_id": len(links), "nodes": nodes, "links": links,
                  "groups": [], "config": {}, "extra": {}, "version": 0.4},
        "protocol_chat_draft": True, "protocol_chat_session_id": session_id, "protocol_revision": revision,
        "protocol_materials": [material.model_dump(mode="json") for material in plan.materials],
        "protocol_questions": questions,
        "questions": questions,
    }
