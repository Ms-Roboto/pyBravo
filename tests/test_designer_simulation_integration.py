"""Real Designer simulation route dispatches native tasks through SuperDex.

The private ST10 rack below has synthetic hole dimensions solely for collision
regression. No deployed catalog, workflow store, profile, or hardware is edited.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.controllers.simulation import SimulationController
from pybravo.physics.contracts import is_checked_physical_report
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis
from pybravo.web import server
from pybravo.workflow.storage import WorkflowStorage
from tests.test_bravo_init import _catalog_from_definitions

PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"
RACK_ID = "synthetic-route-st10-rack"


def _workflow(*, dock_first: bool):
    # Eight earlier P-row pickups are absent. With A1 active, the next legal
    # fresh tip is P16; its native pickup path intersects an undocked finger.
    fresh = [f"{chr(65 + row)}{col + 1}" for row in range(16) for col in range(24)
             if not (row == 15 and col >= 16)]
    tasks = [
        (3, "tips/TipsOn", {"location": 2, "head_mode": {"subset_type": "single_barrel", "subset_config": "back_left"},
                             "tip_anchor_row": 15, "tip_anchor_col": 15}),
        (4, "tips/TipsOff", {"location": 4}),
    ]
    if dock_first:
        tasks.insert(0, (2, "system/DockGripper", {}))
    tasks = [(1, "flow/Start", {}), *tasks, (5, "flow/End", {})]
    nodes = []
    links = []
    for index, (identity, kind, properties) in enumerate(tasks):
        node = {"id": identity, "type": kind, "title": kind.rsplit("/", 1)[-1], "properties": properties}
        if index + 1 < len(tasks):
            link_id = index + 1
            node["outputs"] = [{"links": [link_id]}]
            links.append([link_id, identity, 0, tasks[index + 1][0], 0, -1])
        nodes.append(node)
    return {
        "name": "Hand-built native tip cycle",
        "deck": {
            "2": [{"labware_id": RACK_ID, "tip_definition_id": "st_10ul", "tipbox_fill_state": "full",
                   "available_tips": fresh}],
            "4": [{"labware_id": RACK_ID, "tip_definition_id": "st_10ul", "tipbox_fill_state": "empty"}],
        },
        "graph": {"nodes": nodes, "links": links},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("dock_first", [True, False], ids=["safe-native-cycle", "undocked-finger-collision"])
async def test_designer_simulate_route_runs_superdex_and_stops_before_collision(
    tmp_path, monkeypatch, dock_first,
):
    storage = WorkflowStorage(tmp_path / "workflows")
    saved = storage.create_workflow(_workflow(dock_first=dock_first))
    before = copy.deepcopy(storage.get_workflow(saved["id"]))
    profile = BravoProfile.load(PROFILE_PATH)
    profile.connection.controller_type = "agile"
    original_profile = copy.deepcopy(profile._to_dict())
    live_controller = Mock(name="physical_controller_never_used")
    live = SimpleNamespace(
        profile=profile, _profile=profile, _controller=live_controller,
        machine_id=profile.connection.machine_id,
        active_tip_id=lambda: "st_10ul", active_tip_capacity_ul=lambda: 10.0,
        get_state=Mock(return_value={}), initialize=AsyncMock(),
    )
    simulated = []
    events = []
    initial_fresh_counts = []
    finished = asyncio.Event()

    def make_simulator(*args, **kwargs):
        assert kwargs["mode"] == "simulation"
        robot = Bravo(*args, **kwargs)
        original = robot._labware_catalog.get_definition("lw-4914769d0af7")
        assert original is not None
        fixture = replace(original, id=RACK_ID, name="Synthetic ST10 collision test fixture",
                          well_diameter_mm=3.5, well_depth_mm=50.0)
        robot._labware_catalog = _catalog_from_definitions(fixture)
        robot.connect()
        robot.controller.set_move_timing_enabled(False)
        simulated.append(robot)
        return robot

    async def broadcast(event):
        events.append(copy.deepcopy(event))
        if event["type"] == "workflow:start":
            initial_fresh_counts.append(len(simulated[0]._fresh_tip_wells(2)))
        if event["type"] in {"workflow:complete", "workflow:error"}:
            finished.set()

    monkeypatch.setattr(server, "_bravo", live)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "Bravo", make_simulator)
    monkeypatch.setattr(server.ws_manager, "broadcast", broadcast)
    transport = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/api/workflows/{saved['id']}/simulate")
            assert response.status_code == 200, response.text
            assert response.json()["physical_engine"] == "SuperDex"
            await asyncio.wait_for(finished.wait(), timeout=120)
            await asyncio.sleep(0)  # Allow the route's ownership cleanup to finish.

        assert len(simulated) == 1
        assert initial_fresh_counts == [376]
        robot = simulated[0]
        assert isinstance(robot.controller, SimulationController)
        assert robot.profile is not profile
        assert profile._to_dict() == original_profile
        assert live_controller.mock_calls == []
        live.initialize.assert_not_awaited()
        assert storage.get_workflow(saved["id"]) == before
        assert server._active_workflow_executor is None
        assert robot.controller._motion_guard is None
        assert any(event["type"] == "workflow:node_step" for event in events)
        report = events[-1]["physical_simulation"]
        assert report["engine"] == "SuperDex"
        assert report["samples_checked"] > 0
        assert report["qualification_granted"] is False
        if dock_first:
            assert events[-1]["type"] == "workflow:complete"
            assert events[-1]["status"] == "ok"
            assert is_checked_physical_report(report)
            assert len(robot._fresh_tip_wells(2)) == 375
            assert len(robot._spent_tip_wells(4)) == 1
            assert robot._fresh_tip_wells(4) == set()
            assert any(event.get("step_name") == "lower_z_to_tips" for event in events)
            assert any(event.get("step_name") == "eject_tips" for event in events)
        else:
            assert events[-1]["type"] == "workflow:error"
            assert report["status"] == "failed"
            assert report["last_error"]["node_id"] == 3
            assert report["last_error"]["node_type"] == "tips/TipsOn"
            assert "Physical collision" in report["last_error"]["message"]
            assert any("finger" in body for body in report["last_error"]["bodies"])
            assert report["last_error"]["pose"]
            assert report["contact_queries"] > 0
            assert robot.get_position(Axis.Z) == 0  # Rejected descent is atomic.
            assert not any(event.get("step_name") == "lower_z_to_tips" for event in events)
            assert len(robot._fresh_tip_wells(2)) == 376
            assert robot._spent_tip_wells(4) == set()
            assert not robot._tips_on_head
            assert not any(event["type"] == "workflow:complete" for event in events)
            assert not any(event["type"] == "workflow:node_start" and event["node_id"] in {4, 5} for event in events)
    finally:
        executor = server._active_workflow_executor
        if executor is not None:
            executor.abort()
            await asyncio.wait_for(finished.wait(), timeout=30)
            await asyncio.sleep(0)
        for robot in simulated:
            robot.disconnect()
