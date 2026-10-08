"""Deterministic mechanical diagnostics for saved native drafts, without motion."""

from __future__ import annotations

import math

from pydantic import ValidationError

from pybravo.workflow.drafter.schema import DraftedWorkflow
from pybravo.workflow.drafter.validator import validate_drafted_workflow

from .well_geometry import missing_liquid_well_geometry


def static_labware_issues(workflow, *, catalog_context, physical_geometry=True):
    """Reject definite labware mistakes on an unchanged initial deck.

    Plate handling, scripts/libraries, and manual handoffs can alter occupancy.
    Their layouts and variable/iteration targets remain the native runtime's
    responsibility; this check must not label a later plate using an old rack.
    """
    nodes = workflow.get("graph", {}).get("nodes", [])
    if workflow.get("library") or any(
        node.get("type", "").startswith("plate/")
        or node.get("type") in {"logic/Script", "system/Manual"}
        for node in nodes
    ):
        return []
    catalog = {str(row["id"]): row for row in catalog_context.get("labware", [])
               if isinstance(row, dict) and row.get("id")}
    result = []
    for node in nodes:
        node_type = node.get("type")
        liquid = node_type in {"liquid/Aspirate", "liquid/Dispense", "liquid/Mix"}
        if not liquid and not (physical_geometry and node_type == "tips/TipsOff"):
            continue
        location = (node.get("properties") or {}).get("location")
        if isinstance(location, str) and location.isdecimal():
            location = int(location)
        if type(location) is not int or not 1 <= location <= 9:
            continue  # Dynamic targets must be checked against the runtime deck.
        stack = workflow.get("deck", {}).get(str(location), [])
        if isinstance(stack, dict):
            stack = [stack]
        if not stack or not isinstance(stack[-1], dict):
            continue
        definition = catalog.get(str(stack[-1].get("labware_id", "")))
        if definition is None:
            continue  # Other catalog checks report unresolved identities.
        roles = {str(definition.get(key) or "").lower() for key in ("base_class", "kind")}
        tip_box = "tip_box" in roles
        tip_trash = bool(roles & {"tip_trash", "tip_trash_bin"})
        title = node.get("title") or node_type.split("/")[-1]
        name = definition.get("name") or definition["id"]
        issue = {"node_id": node.get("id"), "node_type": node_type, "node_title": title}
        if liquid and (tip_box or tip_trash):
            result.append({**issue, "field": "location", "value": location,
                "reason": f"{title} (task {node.get('id')}) targets deck position {location}, "
                f"'{name}', which is a {'tip rack' if tip_box else 'tip waste receptacle'}. "
                "Choose the intended plate or reservoir for this liquid task."})
        elif liquid and physical_geometry:
            missing = missing_liquid_well_geometry(definition)
            if missing:
                result.append({**issue, "field": "well_geometry", "value": missing,
                    "reason": f"{title} (task {node.get('id')}) targets deck position {location}, "
                    f"'{name}', whose catalog lacks recorded {', '.join(missing)}. "
                    "Complete its well geometry in Labware Editor before physical simulation; "
                    "an unknown well depth cannot be treated as the plate rim."})
        if physical_geometry and node_type == "tips/TipsOff" and tip_box:
            diameter = definition.get("well_diameter_mm")
            try:
                known = not isinstance(diameter, bool) and math.isfinite(float(diameter)) and float(diameter) > 0
            except (TypeError, ValueError):
                known = False
            if not known:
                result.append({**issue, "field": "well_diameter_mm", "value": "missing_positive_rack_hole_diameter",
                    "reason": f"{title} (task {node.get('id')}) returns tips to deck position {location}, "
                    f"'{name}', whose catalog has no positive tip-hole diameter (well_diameter_mm). "
                    "Record the rack-hole diameter from documented geometry or measurement in Labware Editor before physical simulation."})
    return result


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
    result.extend(static_labware_issues(workflow, catalog_context=catalog_context))
    return result
