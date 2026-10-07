"""Synthetic catalog-grounding checks for newly drafted Designer graphs."""

from __future__ import annotations

from pybravo.workflow.drafter import llm
from pybravo.workflow.drafter.prompt import build_system_prompt
from pybravo.workflow.drafter.schema import DraftedWorkflow
from pybravo.workflow.drafter.validator import (
    quarantine_unverified_liquid_classes,
    validate_drafted_workflow,
)


def _context(*, tip_id: str = "tip-200", tip_capacity: int = 200) -> dict:
    return {
        "machine_id": "machine-test",
        "head_type": "HT_96_D_200",
        "liquid_classes": [
            {
                "machine_id": "machine-test", "head_type": "HT_96_D_200",
                "name": "Stored Method A", "liquid_class_id": "liq-a",
                "tip_id": "", "tip_capacity_ul": 200,
            },
            {
                "machine_id": "other-machine", "head_type": "HT_96_D_200",
                "name": "Other Machine Method", "liquid_class_id": "liq-other",
                "tip_id": "", "tip_capacity_ul": 200,
            },
        ],
        "tip_definitions": [{"tip_id": tip_id, "capacity_ul": tip_capacity}],
        "tipbox_choices": [{
            "labware_id": "rack-1", "tip_definition_id": tip_id,
            "tip_capacity_ul": tip_capacity, "execution_ready": True,
        }],
        "labware": [
            {"id": "rack-1", "name": "Test Rack", "kind": "sbs_plate",
             "base_class": "tip_box", "wells": 96},
            {"id": "plate-1", "name": "Test Plate", "kind": "sbs_plate",
             "base_class": "microplate", "wells": 96},
        ],
    }


def _workflow(*, liquid_class: str = "Stored Method A",
              liquid_class_id: str = "liq-a", tip_id: str = "tip-200") -> DraftedWorkflow:
    types = ("flow/Start", "tips/TipsOn", "liquid/Aspirate",
             "tips/TipsOff", "flow/End")
    nodes = []
    for index, node_type in enumerate(types, 1):
        properties = {}
        if node_type in ("tips/TipsOn", "tips/TipsOff"):
            properties = {"location": 1}
        elif node_type == "liquid/Aspirate":
            properties = {
                "location": 2, "volume": 10, "liquid_class": liquid_class,
                "liquid_class_id": liquid_class_id,
                "reagent_text": "operator-described test liquid",
            }
        nodes.append({"id": index, "type": node_type, "properties": properties})
    return DraftedWorkflow.model_validate({
        "name": "Synthetic catalog test",
        "deck": {
            "1": [{"labware_id": "rack-1", "tip_definition_id": tip_id}],
            "2": [{"labware_id": "plate-1"}],
        },
        "graph": {
            "nodes": nodes,
            "links": [
                {"id": index, "origin_id": index, "origin_slot": 0,
                 "target_id": index + 1, "target_slot": 0, "link_type": -1}
                for index in range(1, len(nodes))
            ],
        },
    })


def _codes(workflow: DraftedWorkflow, context: dict | None) -> set[str]:
    return {issue.code for issue in validate_drafted_workflow(
        workflow, catalog_context=context, require_catalog=True,
    )}


def test_exact_selected_head_identity_and_tip_pair_are_valid():
    assert _codes(_workflow(), _context()) == set()


def test_unmatched_reference_is_never_left_executable_in_new_draft():
    workflow = _workflow(liquid_class="Descriptive Liquid", liquid_class_id="invented")
    original_text = workflow.graph.nodes[2].properties["reagent_text"]

    quarantine_unverified_liquid_classes(workflow, context=_context())

    properties = workflow.graph.nodes[2].properties
    assert properties["liquid_class"] == ""
    assert "liquid_class_id" not in properties
    assert properties["liquid_class_unresolved"]["requested_reference"] == "Descriptive Liquid"
    assert properties["reagent_text"] == original_text
    assert _codes(workflow, _context()) == {"UNRESOLVED_LIQUID_CLASS"}

    mismatched_id = _workflow(liquid_class_id="wrong-id")
    quarantine_unverified_liquid_classes(mismatched_id, context=_context())
    assert mismatched_id.graph.nodes[2].properties["liquid_class"] == ""
    assert "UNRESOLVED_LIQUID_CLASS" in _codes(mismatched_id, _context())

    no_catalog = _workflow()
    quarantine_unverified_liquid_classes(no_catalog, context=None)
    assert no_catalog.graph.nodes[2].properties["liquid_class"] == ""
    assert "UNRESOLVED_LIQUID_CLASS" in _codes(no_catalog, None)


def test_unresolved_marker_must_be_structured_and_keep_reagent_text():
    workflow = _workflow()
    properties = workflow.graph.nodes[2].properties
    properties["liquid_class"] = ""
    properties.pop("liquid_class_id")
    properties["liquid_class_unresolved"] = {
        "requested_reference": "test liquid", "reason": "No selected method",
    }
    assert _codes(workflow, _context()) == {"UNRESOLVED_LIQUID_CLASS"}

    properties["liquid_class_unresolved"]["reason"] = ""
    assert "INVALID_UNRESOLVED_LIQUID_CLASS" in _codes(workflow, _context())
    properties["liquid_class_unresolved"]["reason"] = "No selected method"
    properties.pop("reagent_text")
    assert "MISSING_REAGENT_TEXT" in _codes(workflow, _context())


def test_head_scoping_and_tip_capacity_mismatch_are_reported():
    wrong_head = _workflow(liquid_class="Other Machine Method", liquid_class_id="liq-other")
    assert "UNKNOWN_LIQUID_CLASS" in _codes(wrong_head, _context())

    wrong_tip = _workflow(tip_id="tip-250")
    assert "LIQUID_CLASS_TIP_CAPACITY_MISMATCH" in _codes(
        wrong_tip, _context(tip_id="tip-250", tip_capacity=250),
    )

    wrong_pair = _workflow(tip_id="unknown-tip")
    assert "INCOMPATIBLE_TIPBOX_TIP_PAIR" in _codes(wrong_pair, _context())

    unready = _context()
    unready["tipbox_choices"][0]["execution_ready"] = False
    assert "TIPBOX_NOT_EXECUTION_READY" in _codes(_workflow(), unready)


def test_prompt_lists_only_selected_catalog_identities_and_tip_pairs():
    prompt = build_system_prompt(catalog_context=_context(), include_exemplars=False)
    assert "'Stored Method A' | id=liq-a" in prompt
    assert "Other Machine Method" not in prompt
    assert "labware_id=rack-1 | tip_definition_id=tip-200" in prompt
    assert "liquid_class_unresolved" in prompt


async def test_draft_entrypoint_quarantines_alias_without_live_model(monkeypatch):
    async def fake_model(*args, **kwargs):
        return _workflow(liquid_class="Descriptive Liquid", liquid_class_id="invented")

    monkeypatch.setattr(llm, "_build_client", lambda provider: None)
    monkeypatch.setattr(llm, "_llm_messages", fake_model)
    result = await llm.draft_workflow(
        "Synthetic test request", catalog_context=_context(),
        include_exemplars=False,
        config=llm.DrafterConfig(provider="local", model="qwen", max_repair_attempts=0),
    )
    assert result.workflow.graph.nodes[2].properties["liquid_class"] == ""
    assert {issue.code for issue in result.issues} == {"UNRESOLVED_LIQUID_CLASS"}
    assert result.designer_payload()["errors"] == []
    assert "UNRESOLVED_LIQUID_CLASS" in result.designer_payload()["warnings"][0]
