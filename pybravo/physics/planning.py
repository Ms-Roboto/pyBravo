"""Deterministic mechanical diagnostics for saved native drafts, without motion."""

from __future__ import annotations

import math

from pydantic import ValidationError

from pybravo.workflow.drafter.schema import DraftedWorkflow
from pybravo.workflow.drafter.validator import validate_drafted_workflow


def mechanical_issues(workflow, *, catalog_context):
    candidate = {**workflow, "graph": {**workflow.get("graph", {})}}
    candidate["graph"]["links"] = [
        dict(zip(("id", "origin_id", "origin_slot", "target_id", "target_slot", "link_type"), link))
        if isinstance(link, list)
        else link
        for link in candidate["graph"].get("links", [])
    ]
    candidate["deck"] = {
        str(loc): items if isinstance(items, list) else [items] for loc, items in candidate.get("deck", {}).items()
    }
    try:
        model = DraftedWorkflow.model_validate(candidate)
    except ValidationError as exc:
        return [
            {
                "node_id": None,
                "node_type": "graph",
                "node_title": "Workflow",
                "field": "mechanical_plan",
                "value": "INVALID_NATIVE_GRAPH",
                "reason": f"The saved native workflow cannot be mechanically checked: {exc}",
            }
        ]
    issues = validate_drafted_workflow(model, catalog_context=catalog_context)
    nodes = {node.id: node for node in model.graph.nodes}
    result = [
        {
            "node_id": issue.node_id,
            "node_type": nodes[issue.node_id].type if issue.node_id in nodes else "graph",
            "node_title": nodes[issue.node_id].title if issue.node_id in nodes else "Workflow",
            "field": "mechanical_plan",
            "value": issue.code,
            "reason": issue.message,
        }
        for issue in issues
        if issue.severity == "error"
    ]
    # A proposal can remain editable with unresolved catalog choices, but it
    # cannot become a mechanically checked run by guessing the loaded tip.
    pairs = catalog_context.get("tipbox_choices", [])
    head_limit = catalog_context.get("head_max_volume_ul")
    for node in model.graph.nodes:
        if node.type.startswith("liquid/"):
            volume = node.properties.get("volume")
            if (
                isinstance(volume, (int, float))
                and not isinstance(volume, bool)
                and isinstance(head_limit, (int, float))
                and math.isfinite(head_limit)
                and volume > head_limit
            ):
                result.append(
                    {
                        "node_id": node.id,
                        "node_type": node.type,
                        "node_title": node.title or node.type,
                        "field": "volume",
                        "value": volume,
                        "reason": f"{volume:g} µL exceeds the selected head stroke limit of {head_limit:g} µL.",
                    }
                )
        if node.type not in {"tips/TipsOn", "tips/TipsOff"}:
            continue
        location = node.properties.get("location")
        stack = model.deck.get(str(location), [])
        if not stack or not stack[-1].labware_id:
            continue
        item = stack[-1]
        if catalog_context.get("head_type") and not any(pair.get("labware_id") == item.labware_id for pair in pairs):
            result.append(
                {
                    "node_id": node.id,
                    "node_type": node.type,
                    "node_title": node.title or node.type,
                    "field": "tip_box",
                    "value": item.labware_id,
                    "reason": f"Rack {item.labware_id} has no compatible tip pairing for the selected "
                    f"head {catalog_context['head_type']}. Choose a compatible head/rack/tip setup.",
                }
            )
        elif catalog_context.get("head_type") and not item.tip_definition_id:
            result.append(
                {
                    "node_id": node.id,
                    "node_type": node.type,
                    "node_title": node.title or node.type,
                    "field": "tip_definition_id",
                    "value": "",
                    "reason": f"Position {location} needs the catalog ID of the tips actually loaded; "
                    "a rack identity alone cannot establish tip capacity or clearance.",
                }
            )
    # If the graph contains no labware-moving tasks, absence is definitive.
    # Otherwise occupancy is assessed at each primitive by the native runtime.
    moving = any(node.type.startswith("plate/") for node in model.graph.nodes)
    if not moving:
        for node in model.graph.nodes:
            if not node.type.startswith(("liquid/", "tips/")):
                continue
            loc = node.properties.get("location")
            if isinstance(loc, str) and loc.isdecimal():
                loc = int(loc)
            if isinstance(loc, int) and not candidate["deck"].get(str(loc)):
                result.append(
                    {
                        "node_id": node.id,
                        "node_type": node.type,
                        "node_title": node.title or node.type,
                        "field": "deck",
                        "value": loc,
                        "reason": f"Position {loc} has no catalog labware; the task has no physical source/destination to simulate.",
                    }
                )
    return result
