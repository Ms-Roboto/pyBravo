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
from pybravo.types import Axis, HeadType
from pybravo.web import server
from pybravo.workflow.executor import WorkflowExecutor
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
        loaded["protocol_simulation_target"] = _virtual_target()
        updated = await client.put(f"/api/workflows/{identity}", json=loaded)
        assert updated.status_code == 200, updated.text
        assert updated.json()["protocol_generated_draft"] is True
        assert updated.json()["protocol_draft_status"] == "unreviewed"
        assert updated.json()["protocol_simulation_target"] == _virtual_target()
        copy = {**updated.json(), "name": "Saved copy"}
        copy.pop("id")
        copied = await client.post("/api/workflows", json=copy)
        assert copied.status_code == 200, copied.text
        assert copied.json()["protocol_generated_root_id"] == identity
        assert copied.json()["protocol_generated_provenance"] == provenance
        assert copied.json()["protocol_generated_draft"] is True
        assert copied.json()["protocol_simulation_target"] == _virtual_target()


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
async def test_generated_native_clearance_tasks_can_be_saved_without_hardware_release(tmp_path, monkeypatch):
    storage = WorkflowStorage(tmp_path)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_bravo", None)
    workflow = _native_workflow()
    workflow['graph']['nodes'][1].update(type='system/DockGripper', properties={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.post('/api/protocols/generated-drafts', json={
            'workflow': workflow, 'provenance': {'source_kind': 'synthetic_mechanical_test',
                                               'source_id': 'clearance-fixture', 'model': 'test-fixture'},
        })
        assert response.status_code == 200, response.text
        identity = response.json()['workflow_id']
        saved = (await client.get(f'/api/workflows/{identity}')).json()
        assert saved['graph']['nodes'][1]['type'] == 'system/DockGripper'
        saved['graph']['nodes'][1].update(type='system/Home', properties={'axes': 'Y'})
        updated = await client.put(f'/api/workflows/{identity}', json=saved)
        assert updated.status_code == 200, updated.text
        assert updated.json()['graph']['nodes'][1]['type'] == 'system/Home'
        assert (await client.post(f'/api/workflows/{identity}/execute')).status_code == 409


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


def _virtual_target():
    return {
        "machine_id": "04-91-62-CF-7B-B0", "head_type": "HT_96_D_200", "tip_definition_id": "lt_250ul",
    }


def _active_384_profile():
    profile = BravoProfile.default()
    profile.connection.machine_id = "04-91-62-CF-7B-B0"
    profile.connection.controller_type = "agile"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_id = profile.head.default_tip_id = "st_30ul"
    profile.head.teach_tip_capacity = profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.teachpoints.set_default_teachpoints(profile.head.head_type)
    return profile


@pytest.mark.asyncio
@pytest.mark.parametrize("class_name", ["96 disposable tip 5 - 200ul Water", "384 disposable tip 0.5 - 10ul"])
async def test_virtual_target_uses_its_catalog_and_leaves_installed_384_head_unchanged(
    tmp_path, monkeypatch, class_name,
):
    workflow = _native_workflow()
    workflow["protocol_simulation_target"] = _virtual_target()
    # The successful branch tests isolated virtual-head setup with Initialize.
    # A liquid task without source/rack/volume/catalog ID is now correctly
    # rejected by mechanical preflight even if its name matches this head.
    if class_name.startswith("384"):
        workflow["graph"]["nodes"][1].update(type="liquid/Aspirate")
        workflow["graph"]["nodes"][1]["properties"]["liquid_class"] = class_name
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(workflow, provenance={"model": "qwen"})
    before = storage.get_workflow(saved["id"])
    live = Bravo(profile=_active_384_profile())
    original_profile = live.profile._to_dict()
    live_controller = Mock(name="active_physical_controller")
    live._controller = live_controller
    monkeypatch.setattr(live, "initialize", AsyncMock())
    inherited = {
        "head_mode": {"subset_type": "all_barrels", "row_count": 16, "column_count": 24},
        "tip_selection": {"location": 2, "row": 0, "col": 0},
        "plate_selection": {"1": {"row": 0, "col": 0}},
        "tips_on_head": True, "tip_definition_id": "st_30ul", "attached_tip_length_mm": 26.1,
    }
    monkeypatch.setattr(live, "get_state", Mock(return_value=inherited))
    monkeypatch.setattr(server, "_bravo", live)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    executors, tasks = [], []

    class FakeExecutor(WorkflowExecutor):
        def __init__(self, bravo, graph, **kwargs):
            self.bravo = bravo
            self.kwargs = kwargs
            executors.append(self)

        async def execute(self):
            pass

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr("pybravo.workflow.executor.WorkflowExecutor", FakeExecutor)
    monkeypatch.setattr(server.asyncio, "ensure_future", schedule)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/workflows/{saved['id']}/simulate")
        if class_name.startswith("384"):
            assert response.status_code == 400, response.text
            assert "HT_96_D_200" in response.json()["detail"]["invalid_nodes"][0]["reason"]
            assert executors == []
        else:
            assert response.status_code == 200, response.text
            assert response.json()["simulation_target"] == {
                **_virtual_target(), "tip_capacity_ul": 250.0, "tip_length_mm": 55.2,
            }
            await asyncio.gather(*tasks)
            virtual = executors[0].bravo
            assert virtual.profile.head.head_type is HeadType.HT_96_D_200
            assert virtual.profile.connection.controller_type == "simulation"
            assert virtual.active_tip_id() == "lt_250ul"
            assert virtual.active_tip_capacity_ul() == 250.0
            assert virtual.profile.head.teach_tip_length_mm == 55.2
            assert executors[0].kwargs["runtime_state"] == {}
            assert executors[0].kwargs["strict_validation"] is True
            for location in live.teachpoints.locations:
                old_plane = live.teachpoints.get_teachpoint(location, Axis.Z) + 26.1
                new_plane = virtual.teachpoints.get_teachpoint(location, Axis.Z) + 55.2
                assert new_plane == pytest.approx(old_plane)
            # A head-compatible class still cannot be used with another tip's
            # calibration merely because its name passes preflight.
            with pytest.raises(RuntimeError, match="Unknown liquid class"):
                virtual._resolve_liquid_class(class_name)
        assert (await client.post(f"/api/workflows/{saved['id']}/execute")).status_code == 409
    assert live.profile._to_dict() == original_profile
    assert live_controller.mock_calls == []
    live.initialize.assert_not_awaited()
    assert storage.get_workflow(saved["id"]) == before
    assert server._active_workflow_executor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["wrong_machine", "unknown_head", "wrong_tip", "unknown_tip", "missing_length", "extra_field", "missing_field", "no_profile"])
async def test_virtual_target_rejects_incomplete_or_incompatible_catalog_choices(
    tmp_path, monkeypatch, variant,
):
    target = _virtual_target()
    if variant == "wrong_machine":
        target["machine_id"] = "SIM_OPPORTUNITY"
    elif variant == "unknown_head":
        target["head_type"] = "NOT_A_HEAD"
    elif variant == "wrong_tip":
        target["tip_definition_id"] = "st_10ul"
    elif variant == "unknown_tip":
        target["tip_definition_id"] = "unlisted_tip"
    elif variant == "missing_length":
        target["tip_definition_id"] = "lt_200ul"
    elif variant == "extra_field":
        target["tip_capacity_ul"] = 1000
    elif variant == "missing_field":
        target.pop("machine_id")
    workflow = _native_workflow()
    workflow["protocol_simulation_target"] = target
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(workflow, provenance={"model": "qwen"})
    live = SimpleNamespace(profile=_active_384_profile(), get_state=Mock(return_value={}))
    monkeypatch.setattr(server, "_bravo", None if variant == "no_profile" else live)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    constructor = Mock()
    monkeypatch.setattr(server, "Bravo", constructor)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/workflows/{saved['id']}/simulate")
        assert response.status_code == 409, response.text
    constructor.assert_not_called()
    assert server._active_workflow_executor is None


@pytest.mark.asyncio
async def test_proposed_virtual_target_cannot_be_laundered_into_an_ordinary_workflow(tmp_path, monkeypatch):
    workflow = _native_workflow()
    workflow["protocol_simulation_target"] = _virtual_target()
    workflow["graph"]["nodes"][1]["properties"].clear()
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_workflow(workflow)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    constructor = Mock()
    monkeypatch.setattr(server, "Bravo", constructor)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for mode in ("simulate", "execute"):
            response = await client.post(f"/api/workflows/{saved['id']}/{mode}")
            assert response.status_code == 409
            assert "only for an unreviewed native generated draft" in response.json()["detail"]
    constructor.assert_not_called()
