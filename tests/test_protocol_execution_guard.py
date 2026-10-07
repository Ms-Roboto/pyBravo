"""Reviewed workflow launch owns the instrument until completion, including abort."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from pybravo.bravo import Bravo
from pybravo.web import server
from pybravo.workflow.executor import WorkflowExecutor
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols.ingest import ingest_text
from pybravo.workflow.protocols.store import ProtocolStore


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [True, False])
@pytest.mark.parametrize("mode", ["simulate", "execute"])
async def test_malformed_generated_draft_cannot_start_even_if_marker_is_removed(monkeypatch, marker, mode):
    node = {"id": 2, "type": "liquid/Aspirate", "properties": {"_protocol_step_id": "unreviewed-node-2"}}
    workflow = {"graph": {"nodes": [node]}, "deck": {}, "protocol_generated_draft": marker}
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: SimpleNamespace(get_workflow=lambda identity: workflow))
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    with pytest.raises(HTTPException) as error:
        await server._run_designer_workflow("generated-draft", mode=mode)
    assert error.value.status_code == 409
    assert ("unreviewed draft" if mode == "execute" else "Generated draft needs a Designer graph") in str(error.value.detail)


@pytest.mark.asyncio
async def test_startup_running_and_aborting_all_exclude_second_workflow(monkeypatch):
    entered_initialize, finish_initialize = asyncio.Event(), asyncio.Event()
    entered_execute, finish_execute = asyncio.Event(), asyncio.Event()
    runs = []
    executors = []

    async def initialize():
        entered_initialize.set()
        await finish_initialize.wait()

    bravo = SimpleNamespace(is_connected=True, _initialized=False,
                            _profile=SimpleNamespace(connection=SimpleNamespace(controller_type="agile")),
                            initialize=AsyncMock(side_effect=initialize))
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    storage = SimpleNamespace(get_workflow=lambda identity: {"graph": {"nodes": []}, "deck": {}})
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_validate_workflow_liquid_classes", lambda graph, robot: [])
    monkeypatch.setattr(api, "check_execution_release", lambda *args: None)

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            self.aborted = False
            executors.append(self)

        async def execute(self):
            entered_execute.set()
            await finish_execute.wait()

        def abort(self):
            self.aborted = True

    def schedule(coro):
        task = asyncio.create_task(coro)
        runs.append(task)
        return task

    monkeypatch.setattr("pybravo.workflow.executor.WorkflowExecutor", FakeExecutor)
    monkeypatch.setattr(server.asyncio, "ensure_future", schedule)
    startup = asyncio.create_task(server.execute_designer_workflow("first"))
    try:
        await asyncio.wait_for(entered_initialize.wait(), 1)
        with pytest.raises(HTTPException) as error:
            await server.execute_designer_workflow("during-initialize")
        assert error.value.status_code == 409
        assert bravo.initialize.await_count == 1
        assert executors == []
        finish_initialize.set()
        assert (await startup)["status"] == "started"
        await asyncio.wait_for(entered_execute.wait(), 1)
        with pytest.raises(HTTPException):
            await server.execute_designer_workflow("during-run")
        assert (await server.stop_designer_workflow())["status"] == "stopping"
        assert executors[0].aborted
        assert server._active_workflow_executor is executors[0]
        with pytest.raises(HTTPException):
            await server.execute_designer_workflow("during-abort")
        assert bravo.initialize.await_count == 1
        finish_execute.set()
        await asyncio.gather(*runs)
        assert server._active_workflow_executor is None
    finally:
        finish_initialize.set()
        finish_execute.set()
        await startup
        await asyncio.gather(*runs)


@pytest.mark.asyncio
async def test_completion_does_not_clear_another_owners_executor(monkeypatch):
    entered, finished = asyncio.Event(), asyncio.Event()
    scheduled = []
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_bravo", None)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: SimpleNamespace(
        get_workflow=lambda identity: {"graph": {"nodes": []}, "deck": {}}))

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self):
            entered.set()
            await finished.wait()

    def schedule(coro):
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    monkeypatch.setattr("pybravo.workflow.executor.WorkflowExecutor", FakeExecutor)
    monkeypatch.setattr(server.asyncio, "ensure_future", schedule)
    await server.simulate_designer_workflow("first")
    await entered.wait()
    other_owner = object()
    server._active_workflow_executor = other_owner
    finished.set()
    await asyncio.gather(*scheduled)
    assert server._active_workflow_executor is other_owner


@pytest.mark.asyncio
async def test_setup_failure_is_recorded_and_restores_previous_engine_handlers(tmp_path, monkeypatch):
    bravo = Bravo(mode="simulation")
    bravo.connect()
    store = ProtocolStore(tmp_path)
    monkeypatch.setattr(api, "_store", store)
    session = store.create_session(ingest_text("Inspect the plate.").model_dump())
    run = {"session_id": session["id"], "revision": 1, "workflow_id": "test-release", "events": []}
    def prior_step(*args):
        pass

    def prior_error(*args):
        pass
    bravo._engine.set_step_handler(prior_step)
    bravo._engine.set_error_handler(prior_error)
    events = []

    async def event(payload):
        events.append(payload)
        api.record_execution(run, payload)

    executor = WorkflowExecutor(bravo, {"nodes": [{"id": 1, "type": "flow/Start", "outputs": []}]},
                                strict_validation=True, on_event=event)
    monkeypatch.setattr(executor, "_setup_deck", AsyncMock(side_effect=ValueError("Tip inventory no longer matches")))
    try:
        await executor.execute()
        assert [event["type"] for event in events] == ["workflow:error"]
        saved = store.get("sessions", session["id"])
        assert saved["runs"][0]["result"]["type"] == "workflow:error"
        assert "Tip inventory" in saved["runs"][0]["result"]["error"]
        assert bravo._engine._on_step_complete is prior_step
        assert bravo._engine._on_error is prior_error
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['approval', 'instrument'])
async def test_initialization_change_is_rechecked_before_scheduling(monkeypatch, change):
    from unittest.mock import Mock

    changed = False
    checks = []

    async def initialize():
        nonlocal changed
        changed = True
        if change == 'instrument':
            server._bravo = object()

    bravo = SimpleNamespace(is_connected=True, _initialized=False,
        _profile=SimpleNamespace(connection=SimpleNamespace(controller_type='agile')),
        initialize=AsyncMock(side_effect=initialize))
    monkeypatch.setattr(server, '_bravo', bravo)
    monkeypatch.setattr(server, '_active_workflow_executor', None)
    monkeypatch.setattr(server, '_workflow_start_lock', asyncio.Lock())
    monkeypatch.setattr(server, '_get_workflow_storage', lambda: SimpleNamespace(
        get_workflow=lambda identity: {'graph': {'nodes': []}, 'deck': {}}))
    monkeypatch.setattr(server, '_validate_workflow_liquid_classes', lambda graph, robot: [])

    def check_release(*args):
        checks.append(args)
        if changed:
            raise HTTPException(409, 'Approval or configuration changed during initialization')
        return {'session_id': 'reviewed', 'events': []}

    monkeypatch.setattr(api, 'check_execution_release', check_release)
    executor_constructor = Mock()
    monkeypatch.setattr('pybravo.workflow.executor.WorkflowExecutor', executor_constructor)
    with pytest.raises(HTTPException) as error:
        await server.execute_designer_workflow('reviewed')
    assert error.value.status_code == 409
    assert ('active Bravo changed' in error.value.detail if change == 'instrument'
            else 'Approval or configuration changed' in error.value.detail)
    assert bravo.initialize.await_count == 1
    assert len(checks) == (1 if change == 'instrument' else 2)
    executor_constructor.assert_not_called()
    assert server._active_workflow_executor is None
    assert not server._workflow_start_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["simulate", "execute"])
@pytest.mark.parametrize("variant", ["marked", "node_only", "marker_only"])
async def test_chat_draft_graph_cannot_start_even_with_marker_removed(monkeypatch, mode, variant):
    bravo = SimpleNamespace(
        is_connected=True, _initialized=False,
        _profile=SimpleNamespace(connection=SimpleNamespace(controller_type="agile")),
        initialize=AsyncMock(),
    )
    nodes = [] if variant == "marker_only" else [{"id": 1, "type": "review/ProtocolStep"}]
    workflow = {"graph": {"nodes": nodes}, "deck": {}}
    if variant != "node_only":
        workflow["protocol_chat_draft"] = True
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: SimpleNamespace(
        get_workflow=lambda identity: workflow))
    with pytest.raises(HTTPException) as error:
        await server._run_designer_workflow("chat-draft", mode=mode)
    assert error.value.status_code == 409
    assert "chat graph is a draft" in error.value.detail
    bravo.initialize.assert_not_awaited()
    assert server._active_workflow_executor is None
