"""Container identity never substitutes for the independently selected tip."""
import asyncio
from copy import deepcopy
from dataclasses import fields

import httpx
import pytest

from pybravo import liquid_classes
from pybravo.bravo import Bravo
from pybravo.deck.labware import InMemoryLabwareCatalog, LabwareDefinition
from pybravo.head_mode import head_geometry_for_type
from pybravo.types import HeadType
from pybravo.web import server
from pybravo.workflow.executor import WorkflowExecutor
from pybravo.workflow.protocols import api, llm
from pybravo.workflow.protocols.compiler import ProtocolCompilationError, compile_plan
from pybravo.workflow.protocols.ingest import ingest_text
from pybravo.workflow.protocols.models import ProtocolPlan
from pybravo.workflow.protocols.store import ProtocolStore
from pybravo.workflow.protocols.tipbox_choices import compatible_tipbox_choices
from pybravo.workflow.protocols.validation import validate_plan
from tests.test_protocol_compiler import protocol_fixture

HEADS = ["HT_16_D_ST", "HT_96_D_70", "HT_96_D_70_S2", "HT_384_D_70", "HT_384_D_70_S2",
         "HT_8_D_LT", "HT_96_D_200", "HT_96_D_200_S2"]


def fixture(head="HT_384_D_70", *, interleaved=False):
    plan, setup, context, sources = protocol_fixture()
    geometry = head_geometry_for_type(HeadType[head])
    rows, cols = (16, 24) if geometry.pitch_x_mm == 4.5 else (8, 12)
    for definition in context["labware"]:
        definition.update(rows=rows, cols=cols, wells=rows * cols,
                          spacing_x_mm=geometry.pitch_x_mm, spacing_y_mm=geometry.pitch_y_mm)
    rack = context["labware"][1]
    if interleaved:
        rack.update(rows=16, cols=24, wells=384, spacing_x_mm=4.5, spacing_y_mm=4.5)
    plan["materials"][2]["available_tips"] = "full"
    # Explicitly synthetic dimensions, not inferred measurements for real tips.
    rack.update(tip_definition_id="test-small", supported_tip_ids=["test-small", "test-large"])
    context.update(head_type=head, tip_definitions=[
        {"id": "test-small", "capacity_ul": 10.0, "length_mm": 20.0, "compatible_heads": [head]},
        {"id": "test-large", "capacity_ul": 70.0, "length_mm": 30.0, "compatible_heads": [head]},
    ])
    context["liquid_classes"][0]["tip_id"] = "test-large"
    plan["materials"][2]["tip_definition_id"] = "test-large"
    return plan, setup, context, sources


@pytest.mark.parametrize("head", HEADS)
def test_all_st_lt_heads_keep_multiple_box_options_and_explicit_tip(head):
    plan, setup, context, sources = fixture(head)
    choices = compatible_tipbox_choices(head, context["labware"], context["tip_definitions"])
    assert [choice["tip_definition_id"] for choice in choices] == ["test-small", "test-large"]
    workflow = compile_plan(plan, setup, context, sources=sources)
    assert workflow["deck"]["3"][0]["tip_definition_id"] == "test-large"
    assert workflow["deck"]["4"][0]["tip_definition_id"] == "test-large", "Empty return rack must hold the actual selected tip"
    plan["materials"][2]["tip_definition_id"] = None
    report = validate_plan(plan, setup, context, sources=sources)
    assert "tip_definition" in {issue["code"] for issue in report["issues"]}
    assert any(question["path"] == "/materials/2/tip_definition_id" for question in report["questions"])


@pytest.mark.parametrize("length", [None, 0, -1, float("nan"), float("inf")])
def test_compatible_unknown_length_is_selectable_for_planning_but_not_compilable(length):
    plan, setup, context, sources = fixture()
    context["tip_definitions"][1]["length_mm"] = length
    selected = compatible_tipbox_choices(context["head_type"], context["labware"], context["tip_definitions"])[1]
    assert selected["tip_definition_id"] == "test-large"
    assert selected["execution_ready"] is False and selected["missing_metadata"] == ["tip_length"]
    assert selected["tip_length_mm"] is None
    with pytest.raises(ProtocolCompilationError) as error:
        compile_plan(plan, setup, context, sources=sources)
    assert "tip_length" in {issue["code"] for issue in error.value.report["issues"]}


def test_capacity_and_liquid_class_follow_selected_tip_not_box_default():
    plan, setup, context, sources = fixture()
    assert validate_plan(plan, setup, context, sources=sources)["valid"]
    plan["materials"][2]["tip_definition_id"] = "test-small"
    report = validate_plan(plan, setup, context, sources=sources)
    assert {"tip_volume_exceeded", "liquid_class_tip"} <= {issue["code"] for issue in report["issues"]}
    plan["materials"][2]["tip_definition_id"] = "test-large"
    context["labware"][1]["supported_tip_ids"] = ["test-small"]
    assert "tip_rack_compatibility" in {issue["code"] for issue in validate_plan(plan, setup, context, sources=sources)["issues"]}


def test_different_selected_tips_cannot_mix_in_one_tracked_return_rack():
    plan, setup, context, sources = fixture("HT_16_D_ST")
    plan["materials"][2]["available_tips"] = [f"{row}1" for row in "ABCDEFGHIJKLMNOP"]
    second = {**deepcopy(plan["materials"][2]), "id": "second", "deck_slot": 5, "tip_definition_id": "test-small"}
    plan["materials"].append(second)
    setup["tip_rack_ids"].append("second")
    context["liquid_classes"][0].pop("tip_id")
    plan["steps"][0].update(volume_ul=5.0, repeat=2)
    plan["steps"][0]["source_values"] = [
        {"field": "volume_ul", "value": 5.0, "unit": "uL", "paragraph_id": "p1"},
        {"field": "repeat", "value": 2, "unit": "count", "paragraph_id": "p1"},
    ]
    report = validate_plan(plan, setup, context, sources=sources)
    assert "disposal_tip_type" in {issue["code"] for issue in report["issues"]}


def test_return_tip_binding_follows_material_when_empty_rack_moves():
    plan, setup, context, sources = fixture()
    plan["steps"].insert(0, {"id": "move-waste", "kind": "move_plate", "material": "waste",
                            "destination_slot": 6, "source_paragraph_ids": ["p1"]})
    workflow = compile_plan(plan, setup, context, sources=sources)
    assert workflow["deck"]["4"][0]["tip_definition_id"] == "test-large"
    assert next(node for node in workflow["graph"]["nodes"] if node["type"] == "tips/TipsOff")["properties"]["location"] == 6


@pytest.mark.parametrize("head", ["HT_96_D_70", "HT_96_D_70_S2"])
def test_96st_uses_four_disjoint_interleaved_384_quadrants(head):
    plan, setup, context, sources = fixture(head, interleaved=True)
    plan["steps"][0].update(repeat=4)
    plan["steps"][0]["source_values"].append({"field": "repeat", "value": 4, "unit": "count", "paragraph_id": "p1"})
    sources[0]["text"] += " Repeat 4 times."
    workflow = compile_plan(plan, setup, context, sources=sources)
    for node_type in ("tips/TipsOn", "tips/TipsOff"):
        props = [node["properties"] for node in workflow["graph"]["nodes"] if node["type"] == node_type]
        assert [(p["tip_anchor_row"], p["tip_anchor_col"]) for p in props] == [(0, 0), (0, 1), (1, 0), (1, 1)]
        assert all(p["row_stride"] == p["col_stride"] == 2 for p in props)
    assert workflow["protocol"]["run_sheet"]["tips_required"] == 384
    plan["materials"][2]["available_tips"] = [f"{row}{col}" for row in "ABCDEFGHIJKLMNOP" for col in range(1, 25) if (row, col) != ("A", 1)]
    assert "tip_inventory_exhausted" in {issue["code"] for issue in validate_plan(plan, setup, context, sources=sources)["issues"]}
    setup["head_mode"] = {"subset_type": "single_barrel", "subset_config": "back_left"}
    assert "interleaved_head_mode" in {issue["code"] for issue in validate_plan(plan, setup, context, sources=sources)["issues"]}


@pytest.mark.parametrize("changed", ["tip", "box"])
def test_llm_accepts_one_explicit_component_change_and_keeps_readiness(changed):
    old = {"id": "tips", "name": "Tips", "role": "tips", "labware_id": "rack", "tip_definition_id": "tip10"}
    new = {**old, ("tip_definition_id" if changed == "tip" else "labware_id"): ("tip70" if changed == "tip" else "rack2")}
    choice = {"labware_id": new["labware_id"], "tip_definition_id": new["tip_definition_id"],
              "execution_ready": False, "missing_metadata": ["tip_length"], "tip_length_mm": None}
    source = ingest_text("Use tip70 instead." if changed == "tip" else "Move the tips to rack2.")
    context = {"current_plan": {"materials": [old]}, "tipbox_choices": [choice]}
    compact = llm._tipbox_context(context, source)
    assert compact["tipbox_choices"] == [choice]
    plan = ProtocolPlan(name="Independent tips", materials=[new])
    issues, recommendations = llm._check_tipbox_guidance(plan, source, compact)
    assert issues == [] and recommendations == []
    issues, _ = llm._check_tipbox_guidance(plan, ingest_text("Keep everything unchanged."), compact)
    assert issues


@pytest.mark.asyncio
async def test_save_reload_and_reusable_setup_keep_selected_tip(tmp_path, monkeypatch):
    plan, setup, _, _ = fixture()
    monkeypatch.setattr(api, "_store", ProtocolStore(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = (await client.post("/api/protocols/from-text", json={"text": "Transfer 20 uL."})).json()
        response = await client.patch(f"/api/protocols/{session['id']}", json={"revision": session["revision"], "plan": plan, "setup": setup})
        assert response.status_code == 200, response.text
        loaded = (await client.get(f"/api/protocols/{session['id']}")).json()
        assert loaded["plan"]["materials"][2]["tip_definition_id"] == "test-large"
        response = await client.post("/api/protocols/setups", json={"name": "Explicit tips", "setup": setup, "materials": plan["materials"]})
        assert response.status_code == 200, response.text
        assert (await client.get("/api/protocols/setups")).json()["items"][0]["materials"][2]["tip_definition_id"] == "test-large"


@pytest.mark.asyncio
@pytest.mark.parametrize("mock_tasks", [True, False])
async def test_executor_forwards_compiled_interleaved_selection(monkeypatch, mock_tasks):
    plan, setup, context, sources = fixture("HT_96_D_70", interleaved=True)
    # Use a real catalog tip in the simulation facade; test fixture measurements
    # for invented IDs above are never promoted to the runtime catalog.
    plan["materials"][2]["tip_definition_id"] = "st_30ul"
    context["tip_definitions"] = [{"id": "st_30ul", "capacity_ul": 30.0, "length_mm": 26.1, "compatible_heads": ["HT_96_D_70"]}]
    context["labware"][1].update(tip_definition_id="st_30ul", supported_tip_ids=["st_30ul"], height_mm=50.0)
    context["liquid_classes"][0]["tip_id"] = "st_30ul"
    context["labware"][0]["height_mm"] = 14.4
    for row in context["labware"]:
        row.update(length_mm=127.76, width_mm=85.48, offset_x_mm=2.25, offset_y_mm=2.25)
    workflow = compile_plan(plan, setup, context, sources=sources)
    bravo = Bravo(mode="simulation")
    bravo.profile.head.head_type = HeadType.HT_96_D_70
    bravo.profile.resolve_tips_for_head()
    allowed = {field.name for field in fields(LabwareDefinition)}
    bravo._labware_catalog = InMemoryLabwareCatalog([LabwareDefinition(**{k: v for k, v in row.items() if k in allowed}) for row in context["labware"]])
    selections, events = [], []
    original = bravo.set_tip_selection

    def select(*args, **kwargs):
        result = original(*args, **kwargs)
        selections.append(result)
        return result

    async def event(payload):
        events.append(payload)

    async def complete(task):
        pass  # No motor actions; facade inventory and selection checks still run.

    monkeypatch.setattr(bravo, "set_tip_selection", select)
    if mock_tasks:
        monkeypatch.setattr(bravo._engine, "execute", complete)
    monkeypatch.setattr(liquid_classes, "get_liquid_class", lambda *args, **kwargs: {
        "name": "Water", "tip_id": "st_30ul", "tip_capacity_ul": 30.0,
        "aspirate": {}, "dispense": {}, "equation": {},
    })
    executor = WorkflowExecutor(bravo, workflow["graph"], deck_config=workflow["deck"], preview_animation=False,
                                strict_validation=True, on_event=event)
    try:
        await asyncio.wait_for(executor.execute(), timeout=20)
        assert events[-1]["type"] == "workflow:complete", events
        assert [(s.row_stride, s.col_stride) for s in selections] == [(2, 2), (2, 2)]
        assert len(bravo._occupied_tip_wells(3)) == 288
        assert len(bravo._occupied_tip_wells(4)) == 96
    finally:
        bravo.disconnect()
