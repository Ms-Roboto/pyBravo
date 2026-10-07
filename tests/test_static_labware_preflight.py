"""Definite rack/plate mistakes are reported before a saved workflow moves."""

import asyncio
import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from pybravo.bravo import Bravo
from pybravo.deck.labware import InMemoryLabwareCatalog, LabwareDefinition
from pybravo.physics.planning import mechanical_issues, static_labware_issues
from pybravo.web import server
from pybravo.workflow.storage import WorkflowStorage

CATALOG = {"labware": [
    {"id": "st10-rack", "name": "384 ST10 rack", "kind": "sbs_plate", "base_class": "tip_box", "well_diameter_mm": 0},
    {"id": "plate", "name": "CellVis plate", "kind": "sbs_plate", "base_class": "microplate", "well_diameter_mm": 3},
]}


def workflow(liquid_location=1):
    nodes = [
        {"id": 1, "type": "flow/Start", "properties": {}},
        {"id": 2, "type": "tips/TipsOn", "properties": {"location": 1}},
        {"id": 3, "type": "liquid/Dispense", "title": "Dispense sample", "properties": {
            "location": liquid_location, "volume": 5, "liquid_class": "Test class"}},
        {"id": 4, "type": "tips/TipsOff", "title": "Return tips", "properties": {"location": 3}},
        {"id": 5, "type": "flow/End", "properties": {}},
    ]
    return {"name": "Static role checks", "deck": {
        "1": [{"labware_id": "st10-rack"}], "2": [{"labware_id": "plate"}], "3": [{"labware_id": "st10-rack"}],
    }, "graph": {"nodes": nodes, "links": [[i, i, 0, i + 1, 0, -1] for i in range(1, 5)]}}


@pytest.mark.parametrize("node_type", ["liquid/Aspirate", "liquid/Dispense", "liquid/Mix"])
def test_known_tip_rack_liquid_target_and_missing_return_geometry_report_together(node_type):
    data = workflow()
    data["graph"]["nodes"][2]["type"] = node_type
    before = copy.deepcopy(data)
    issues = static_labware_issues(data, catalog_context=CATALOG)
    assert len(issues) == 2
    role, geometry = issues
    assert role["node_id"] == 3 and role["field"] == "location" and role["value"] == 1
    assert "Dispense sample (task 3)" in role["reason"]
    assert "deck position 1" in role["reason"] and "384 ST10 rack" in role["reason"]
    assert "plate or reservoir" in role["reason"]
    assert geometry["node_id"] == 4 and geometry["field"] == "well_diameter_mm"
    assert "deck position 3" in geometry["reason"] and "documented geometry or measurement" in geometry["reason"]
    assert data == before


def test_catalog_role_wins_over_stale_saved_deck_label():
    data = workflow()
    data["deck"]["1"][0].update(base_class="microplate", name="Incorrect copied label")
    assert any(issue["field"] == "location" for issue in static_labware_issues(data, catalog_context=CATALOG))


def test_valid_plate_target_and_measured_tip_return_geometry_pass():
    catalog = copy.deepcopy(CATALOG)
    catalog["labware"][0]["well_diameter_mm"] = 3.5
    assert static_labware_issues(workflow(2), catalog_context=catalog) == []


@pytest.mark.parametrize("node_type", ["plate/PickPlace", "plate/Stack", "plate/Destack", "system/Manual", "logic/Script"])
def test_layout_changing_workflow_is_deferred_to_runtime(node_type):
    data = workflow()
    data["graph"]["nodes"].insert(1, {"id": 6, "type": node_type, "properties": {
        "pick_location": 2, "place_location": 1, "source_location": 2, "destination_location": 1, "base_location": 1,
    }})
    assert static_labware_issues(data, catalog_context=CATALOG) == []


@pytest.mark.parametrize("location", ["iter:1,2", "var:destination"])
def test_dynamic_liquid_targets_are_not_compared_to_initial_deck(location):
    data = workflow(location)
    data["graph"]["nodes"][3]["properties"]["location"] = "var:return_rack"
    assert static_labware_issues(data, catalog_context=CATALOG) == []


def test_hardware_role_check_does_not_require_collision_geometry():
    assert static_labware_issues(workflow(2), catalog_context=CATALOG, physical_geometry=False) == []
    issues = static_labware_issues(workflow(), catalog_context=CATALOG, physical_geometry=False)
    assert len(issues) == 1 and issues[0]["field"] == "location"


def test_mechanical_readiness_reuses_role_and_geometry_diagnostics():
    issues = mechanical_issues(workflow(), catalog_context=CATALOG)
    assert any(issue["node_id"] == 3 and issue["field"] == "location" for issue in issues)
    assert any(issue["node_id"] == 4 and issue["field"] == "well_diameter_mm" for issue in issues)


@pytest.mark.parametrize("mode", ["simulate", "execute"])
async def test_saved_handmade_workflow_is_rejected_before_initialization_or_executor(tmp_path, monkeypatch, mode):
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_workflow(workflow())
    live = Bravo(mode="simulation")
    live._labware_catalog = InMemoryLabwareCatalog([LabwareDefinition(**row) for row in CATALOG["labware"]])
    initialize = AsyncMock()
    monkeypatch.setattr(live, "initialize", initialize)
    if mode == "execute":
        # The preflight is before hardware dispatch; keep a software controller
        # while exercising a physical profile's launch route.
        live.connect()
        live.profile.connection.controller_type = "agile"
    monkeypatch.setattr(server, "_bravo", live)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_validate_workflow_liquid_classes", lambda *args: [])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            response = await client.post(f"/api/workflows/{saved['id']}/{mode}")
        assert response.status_code == 400, response.text
        issues = response.json()["detail"]["invalid_nodes"]
        assert len(issues) == (2 if mode == "simulate" else 1)
        assert any(issue["node_id"] == 3 and issue["field"] == "location" for issue in issues)
        initialize.assert_not_awaited()
        assert server._active_workflow_executor is None
        assert storage.get_workflow(saved["id"]) == saved
    finally:
        live.disconnect()
