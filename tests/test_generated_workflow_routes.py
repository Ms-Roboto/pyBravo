"""Saved model drafts are editable, rehearse in software, and cannot run hardware."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pybravo.bravo import Bravo
from pybravo.controllers.simulation import SimulationController
from pybravo.profile.profile import BravoProfile
from pybravo.web import server
from pybravo.workflow.storage import WorkflowStorage


@pytest.mark.asyncio
async def test_generated_draft_load_save_copy_and_run_guards(tmp_path, monkeypatch):
    storage = WorkflowStorage(tmp_path)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_bravo", None)
    workflow = {
        "name": "Local model protocol",
        "description": "Unreviewed external stage",
        "deck": {},
        "graph": {
            "nodes": [
                {"id": 1, "type": "flow/Start", "properties": {}},
                {"id": 2, "type": "system/Manual", "properties": {"message": "Perform the cited external stage."}},
                {"id": 3, "type": "flow/End", "properties": {}},
            ],
            "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]],
        },
    }
    provenance = {"source_kind": "text2wetlab_pinned_task", "source_id": "test-task", "model": "qwen"}
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/protocols/generated-drafts", json={"workflow": workflow, "provenance": provenance})
        assert created.status_code == 200, created.text
        identity = created.json()["workflow_id"]
        loaded = (await client.get(f"/api/workflows/{identity}")).json()
        assert loaded["protocol_generated_draft"] is True
        assert loaded["protocol_draft_status"] == "unreviewed"
        assert loaded["protocol_generated_provenance"] == provenance
        assert loaded["graph"]["nodes"][1]["properties"]["_protocol_step_id"] == "unreviewed-node-2"
        assert any(item["id"] == identity and item["protocol_generated_draft"]
                   for item in (await client.get("/api/workflows")).json()["workflows"])

        assert (await client.post(f"/api/workflows/{identity}/execute")).status_code == 409
        stripped = {**loaded, "protocol_generated_draft": False}
        assert (await client.put(f"/api/workflows/{identity}", json=stripped)).status_code == 409
        stripped.pop("protocol_generated_draft")
        assert (await client.post("/api/workflows", json=stripped)).status_code == 409

        loaded["name"] = "Edited model protocol"
        updated = await client.put(f"/api/workflows/{identity}", json=loaded)
        assert updated.status_code == 200, updated.text
        assert updated.json()["protocol_generated_draft"] is True
        assert updated.json()["protocol_draft_status"] == "unreviewed"
        copy = {**updated.json(), "name": "Saved copy"}
        copy.pop("id")
        copied = await client.post("/api/workflows", json=copy)
        assert copied.status_code == 200, copied.text
        assert copied.json()["protocol_generated_root_id"] == identity
        assert copied.json()["protocol_generated_provenance"] == provenance
        assert copied.json()["protocol_generated_draft"] is True


def _native_workflow():
    return {
        "name": "Native draft rehearsal",
        "deck": {},
        "graph": {
            "nodes": [
                {"id": 1, "type": "flow/Start", "properties": {},
                 "outputs": [{"links": [1]}]},
                {"id": 2, "type": "system/Initialize",
                 "properties": {"_protocol_step_id": "unreviewed-node-2"},
                 "outputs": [{"links": [2]}]},
                {"id": 3, "type": "flow/End", "properties": {}},
            ],
            "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [True, False])
@pytest.mark.parametrize("task_fails", [True, False])
async def test_native_generated_rehearsal_is_isolated_unqualified_and_stops_on_error(
    tmp_path, monkeypatch, marker, task_fails,
):
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(
        _native_workflow(), provenance={"model": "qwen", "source_id": "native-test"},
    )
    identity = saved["id"]
    if not marker:
        storage.update_workflow(identity, {"protocol_generated_draft": False})
    before = storage.get_workflow(identity)

    profile = BravoProfile.default()
    profile.connection.controller_type = "agile"
    live_controller = Mock(name="live_hardware_controller")
    live = SimpleNamespace(
        profile=profile, _profile=profile, _controller=live_controller,
        get_state=Mock(return_value={}), initialize=AsyncMock(),
    )
    monkeypatch.setattr(server, "_bravo", live)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_validate_workflow_liquid_classes", lambda graph, robot: [])
    simulated, tasks, events = [], [], []

    def simulation_bravo(*args, **kwargs):
        assert kwargs["mode"] == "simulation"
        result = Bravo(*args, **kwargs)
        simulated.append(result)
        if task_fails:
            monkeypatch.setattr(result, "initialize", AsyncMock(side_effect=RuntimeError("Rehearsal task failed")))
        return result

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(server, "Bravo", simulation_bravo)
    monkeypatch.setattr(server.asyncio, "ensure_future", schedule)
    monkeypatch.setattr(server.ws_manager, "broadcast", AsyncMock(side_effect=events.append))
    transport = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.post(f"/api/workflows/{identity}/execute")
            assert denied.status_code == 409
            assert simulated == []
            started = await client.post(f"/api/workflows/{identity}/simulate")
            assert started.status_code == 200, started.text
            assert started.json()["simulation_kind"] == "draft_rehearsal"
            assert started.json()["qualification_granted"] is False
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
            assert (await client.post(f"/api/workflows/{identity}/execute")).status_code == 409

        assert len(simulated) == 1
        assert isinstance(simulated[0].controller, SimulationController)
        assert simulated[0].profile is not profile
        assert profile.connection.controller_type == "agile"
        assert live_controller.mock_calls == []
        live.initialize.assert_not_awaited()
        assert storage.get_workflow(identity) == before
        assert before["protocol_draft_status"] == "unreviewed"
        assert server._active_workflow_executor is None
        if task_fails:
            assert any(event["type"] == "workflow:task_aborted" for event in events)
            assert events[-1]["type"] == "workflow:error"
            assert "Rehearsal task failed" in events[-1]["error"]
            assert not any(event["type"] == "workflow:complete" for event in events)
        else:
            assert simulated[0]._initialized
            assert events[-1]["type"] == "workflow:complete"
            assert events[-1]["status"] == "ok"
    finally:
        await asyncio.gather(*tasks)
        for bravo in simulated:
            bravo.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [True, False])
@pytest.mark.parametrize("unsafe", ["script", "library", "release", "review_node", "compiled_preview"])
async def test_generated_rehearsal_rechecks_saved_content_before_any_simulator(
    tmp_path, monkeypatch, marker, unsafe,
):
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(_native_workflow(), provenance={"model": "qwen"})
    corrupted = copy.deepcopy(saved)
    corrupted["protocol_generated_draft"] = marker
    if unsafe == "script":
        corrupted["graph"]["nodes"][1].update(type="logic/Script")
        corrupted["graph"]["nodes"][1]["properties"]["script"] = "import pybravo"
    elif unsafe == "library":
        corrupted["library"] = "import pybravo"
    elif unsafe == "release":
        corrupted["approval"] = {"scientist": "Untrusted imported approval"}
    elif unsafe == "review_node":
        corrupted["graph"]["nodes"][1]["type"] = "review/ProtocolStep"
    else:
        corrupted["protocol_compiled_preview"] = True
    # Simulate stale/external file edits that bypass the normal CRUD import gate.
    storage.update_workflow(saved["id"], corrupted)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    constructor = Mock()
    monkeypatch.setattr(server, "Bravo", constructor)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for endpoint in ("simulate", "execute"):
            response = await client.post(f"/api/workflows/{saved['id']}/{endpoint}")
            assert response.status_code == 409, response.text
    constructor.assert_not_called()
    assert server._active_workflow_executor is None
