"""Lower a validated plan to an allowlisted, linear designer workflow.

The LLM never supplies executable properties, graph nodes or Python. This
compiler creates every node and edge, and refuses any unresolved check.
"""
from __future__ import annotations

from typing import Any

from .models import ProtocolPlan, ProtocolSetup
from .validation import _sources, prepare_protocol

ALLOWED_NODE_TYPES = frozenset({"flow/Start", "flow/End", "tips/TipsOn", "tips/TipsOff",
    "liquid/Aspirate", "liquid/Dispense", "liquid/Mix", "plate/PickPlace", "plate/Stack",
    "plate/Destack", "system/Wait", "system/Manual"})


class ProtocolCompilationError(ValueError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__("Protocol has unresolved validation issues; review the validation report before compilation.")


def compile_plan(plan: ProtocolPlan | dict, setup: ProtocolSetup | dict, context: dict, *, sources: Any = None) -> dict:
    prepared = prepare_protocol(plan, setup, context, sources=sources)
    if not prepared.report["ok"]:
        raise ProtocolCompilationError(prepared.report)
    assert prepared.plan is not None
    paragraphs = _sources(sources)
    specs = [{"type": "flow/Start", "properties": {}}] + prepared.operations + [{"type": "flow/End", "properties": {}}]
    nodes, links = [], []
    for index, spec in enumerate(specs):
        node_id = index + 1
        node_type = spec["type"]
        if node_type not in ALLOWED_NODE_TYPES:
            raise ValueError(f"Compiler rejected unsupported operation {node_type!r}")
        properties = dict(spec["properties"])
        source_ids = spec.get("source_paragraph_ids", [])
        if source_ids:
            first = paragraphs[source_ids[0]]
            properties["_source_citation"] = {"paragraph_id": source_ids[0], "excerpt": str(first.get("text", ""))[:500],
                "page": first.get("page") or first.get("page_no"), "paragraph_ids": source_ids}
        if spec.get("step_id"):
            properties["_protocol_step_id"] = spec["step_id"]
            properties["_protocol_path"] = spec["path"]
        nodes.append({"id": node_id, "type": node_type, "title": spec.get("description") or node_type.split("/")[-1],
            "pos": [index * 260.0, 100.0], "size": [230, 130], "order": index, "mode": 0, "properties": properties,
            "inputs": [] if index == 0 else [{"name": "flow", "type": -1, "link": index}],
            "outputs": [] if index == len(specs) - 1 else [{"name": "flow", "type": -1, "links": [node_id]}]})
        if index:
            links.append([index, index, 0, node_id, 0, -1])
    return {"id": "", "name": prepared.plan.name, "description": prepared.plan.description,
        "deck": prepared.deck, "graph": {"last_node_id": len(nodes), "last_link_id": len(links), "nodes": nodes,
        "links": links, "groups": [], "config": {}, "extra": {}, "version": 0.4},
        "protocol": {"schema_version": prepared.plan.schema_version, "run_sheet": prepared.report["summary"]}}
