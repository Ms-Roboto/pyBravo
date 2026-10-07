"""A walkthrough cannot turn unresolved generated tasks into a validated run."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pybravo.profile.profile import BravoProfile
from pybravo.web import server
from pybravo.workflow.storage import WorkflowStorage


def _draft():
    return {"name": "Unresolved catalog draft", "deck": {}, "graph": {
        "nodes": [
            {"id": 1, "type": "flow/Start", "properties": {}},
            {"id": 2, "type": "liquid/Aspirate", "title": "Unresolved liquid", "properties": {
                "location": 1, "volume": 5, "liquid_class": "reagent-name-is-not-a-class",
            }},
            {"id": 3, "type": "flow/End", "properties": {}},
        ], "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]],
    }}


def test_unresolved_marker_never_falls_through_to_default_class(monkeypatch):
    graph = _draft()["graph"]
    graph["nodes"][1]["properties"].update(
        liquid_class="", liquid_class_unresolved={
            "requested_reference": "reagent-name-is-not-a-class", "reason": "No catalog record",
        },
    )
    monkeypatch.setattr(server, "_active_liquid_context", lambda robot: {"machine_id": "hardware", "head_type": "head"})
    monkeypatch.setattr(server.liquid_classes_store, "list_liquid_classes", lambda **kwargs: [{"name": "Water"}])
    errors = server._validate_workflow_liquid_classes(graph, object())
    assert len(errors) == 1
    assert errors[0]["node_id"] == 2
    assert errors[0]["value"] == "reagent-name-is-not-a-class"


def test_generated_blank_class_cannot_inherit_an_implicit_default():
    graph = _draft()["graph"]
    graph["nodes"][1]["properties"]["liquid_class"] = ""
    errors = server._missing_generated_liquid_methods(graph)
    assert len(errors) == 1
    assert errors[0]["node_id"] == 2
    assert errors[0]["value"] == "unresolved"


@pytest.mark.asyncio
async def test_strict_launch_rejects_blank_class_before_creating_executor(tmp_path, monkeypatch):
    from pybravo.workflow import executor

    storage = WorkflowStorage(tmp_path)
    draft = _draft()
    draft["graph"]["nodes"][1]["properties"]["liquid_class"] = ""
    saved = storage.create_generated_draft(draft, provenance={"model": "qwen"})
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_bravo", None)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_validate_workflow_liquid_classes", lambda graph, bravo: [])
    constructor = Mock()
    monkeypatch.setattr(executor, "WorkflowExecutor", constructor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post(f"/api/workflows/{saved['id']}/simulate")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["invalid_nodes"][0]["value"] == "unresolved"
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_walkthrough_preserves_unknown_classes_without_calling_hardware(tmp_path, monkeypatch):
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(_draft(), provenance={"model": "qwen"})
    before = copy.deepcopy(saved)
    profile = BravoProfile.default()
    profile.connection.controller_type = "agile"
    controller = Mock()
    live = SimpleNamespace(profile=profile, _controller=controller, initialize=AsyncMock())
    profile_before = profile._to_dict()
    unknown = [{"node_id": 2, "field": "liquid_class", "value": "reagent-name-is-not-a-class",
                "reason": "No matching hardware liquid class"}]
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_bravo", live)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_validate_workflow_liquid_classes", lambda graph, bravo: unknown)
    constructor = Mock(side_effect=AssertionError("Walkthrough must not construct Bravo"))
    monkeypatch.setattr(server, "Bravo", constructor)
    events, tasks = [], []
    monkeypatch.setattr(server.ws_manager, "broadcast", AsyncMock(side_effect=events.append))

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(server.asyncio, "ensure_future", schedule)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        result = await client.post(f"/api/workflows/{saved['id']}/walkthrough")
        assert result.status_code == 200, result.text
        assert result.json()["simulation_kind"] == "visual_walkthrough"
        assert result.json()["validation_passed"] is False
        assert result.json()["qualification_granted"] is False
        assert result.json()["diagnostics"] == unknown
        assert (await client.post(f"/api/workflows/{saved['id']}/execute")).status_code == 409
        await asyncio.gather(*tasks)
    assert storage.get_workflow(saved["id"]) == before
    assert profile._to_dict() == profile_before
    constructor.assert_not_called()
    live.initialize.assert_not_called()
    assert not controller.mock_calls
    complete = next(event for event in events if event["type"] == "workflow:complete")
    assert complete["validation_passed"] is False
    assert complete["qualification_granted"] is False
    assert server._active_workflow_executor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["script", "library", "approval", "ordinary"])
async def test_walkthrough_rejects_non_draft_or_executable_imports(tmp_path, monkeypatch, unsafe):
    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(_draft(), provenance={"model": "qwen"})
    corrupt = copy.deepcopy(saved)
    if unsafe == "script":
        corrupt["graph"]["nodes"][1]["type"] = "flow/Script"
    elif unsafe == "library":
        corrupt["library"] = "raise RuntimeError('must not run')"
    elif unsafe == "approval":
        corrupt["approval"] = {"scientist": "untrusted"}
    else:
        corrupt["protocol_generated_draft"] = False
    storage.update_workflow(saved["id"], corrupt)
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_bravo", None)
    monkeypatch.setattr(server, "_active_workflow_executor", None)
    monkeypatch.setattr(server, "_workflow_start_lock", asyncio.Lock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post(f"/api/workflows/{saved['id']}/walkthrough")
    assert response.status_code == 409
    assert server._active_workflow_executor is None
