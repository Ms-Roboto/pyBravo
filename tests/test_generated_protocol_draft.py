"""Generated protocol diagrams remain editable but cannot become releases by saving."""

from __future__ import annotations

import copy

import httpx
import pytest

from pybravo.web import server
from pybravo.workflow.protocols import api
from pybravo.workflow.storage import WorkflowStorage


def _draft_workflow() -> dict:
    nodes = [
        {"id": 1, "type": "flow/Start", "title": "Start"},
        {"id": 2, "type": "liquid/Aspirate", "title": "Aspirate source", "properties": {
            "location": 1, "volume": 10, "liquid_class": "unqualified-example", "anchor": "A1",
        }},
        {"id": 3, "type": "liquid/Dispense", "title": "Dispense destination", "properties": {
            "location": 2, "volume": 10, "liquid_class": "unqualified-example", "anchor": "A1",
        }},
        {"id": 4, "type": "system/Manual", "title": "External module handoff", "properties": {
            "message": "Move the plate to the external module and record its result.",
        }},
        {"id": 5, "type": "flow/End", "title": "End"},
    ]
    links = [
        {"id": index, "origin_id": index, "origin_slot": 0, "target_id": index + 1,
         "target_slot": 0, "link_type": -1}
        for index in range(1, 5)
    ]
    return {"name": "Model-generated review", "description": "Unreviewed local-model diagram",
            "deck": {"1": [{"labware_id": "source-plate", "name": "Proposed source",
                             "tip_definition_id": "st_10ul"}], "2": []},
            "graph": {"nodes": nodes, "links": links}}


def _request(workflow: dict | None = None) -> dict:
    return {"workflow": workflow or _draft_workflow(), "provenance": {
        "source_kind": "text2wetlab", "source_id": "example-task", "model": "local-qwen",
        "source_sha256": "a" * 64,
        "source_url": "https://example.org/pinned-task",
        "source_paper_sha256": "b" * 64,
        "model_url": "http://sparky.local:8000/v1",
        "dataset_revision": "pinned-revision",
    }}


@pytest.fixture
def generated_context(monkeypatch):
    monkeypatch.setattr(api, "_bravo", lambda: None)
    monkeypatch.setattr(api, "machine_context", lambda bravo: {
        "labware": [{"id": "source-plate"}],
    })


@pytest.mark.asyncio
async def test_generated_draft_is_saved_as_loadable_unreviewed_designer_graph(tmp_path, monkeypatch, generated_context):
    storage = WorkflowStorage(tmp_path / "workflows")
    monkeypatch.setattr(server, "_workflow_storage", storage)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        request = _request()
        request["issues"] = [{"severity": "warning", "code": "METHOD_UNKNOWN",
                              "message": "Liquid method must be selected after review.", "path": "/graph/nodes/1"}]
        response = await client.post("/api/protocols/generated-drafts", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "unreviewed"
        assert result["url"] == f"/designer?workflow={result['workflow_id']}"
        saved = (await client.get(f"/api/workflows/{result['workflow_id']}")).json()
    assert saved["protocol_generated_draft"] is True
    assert saved["protocol_draft_status"] == "unreviewed"
    assert saved["protocol_generated_root_id"] == saved["id"]
    assert saved["protocol_generated_provenance"]["model"] == "local-qwen"
    assert saved["protocol_generated_provenance"]["source_paper_sha256"] == "b" * 64
    assert any(item.get("origin") == "submitted_with_draft" and item["code"] == "METHOD_UNKNOWN"
               for item in saved["protocol_draft_issues"])
    assert saved["deck"]["1"][0]["tip_definition_id"] == "st_10ul"
    assert saved["library"] == ""
    nodes = saved["graph"]["nodes"]
    assert [node["type"] for node in nodes] == [
        "flow/Start", "liquid/Aspirate", "liquid/Dispense", "system/Manual", "flow/End",
    ]
    assert nodes[1]["inputs"][0]["link"] == 1
    assert nodes[1]["outputs"][0]["links"] == [2]
    assert nodes[1]["properties"]["_protocol_step_id"] == "unreviewed-node-2"
    assert saved["graph"]["links"][0] == [1, 1, 0, 2, 0, -1]
    assert storage.list_workflows()[0]["id"] == saved["id"]
    assert storage.list_workflows()[0]["protocol_generated_draft"] is True


@pytest.mark.asyncio
async def test_generated_draft_rejects_executable_content_and_broken_topology(tmp_path, monkeypatch, generated_context):
    storage = WorkflowStorage(tmp_path / "workflows")
    monkeypatch.setattr(server, "_workflow_storage", storage)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        script = _draft_workflow()
        script["graph"]["nodes"][3]["type"] = "logic/Script"
        assert (await client.post("/api/protocols/generated-drafts", json=_request(script))).status_code == 422

        library = _draft_workflow()
        library["library"] = "import os"
        assert (await client.post("/api/protocols/generated-drafts", json=_request(library))).status_code == 422

        disconnected = _draft_workflow()
        disconnected["graph"]["links"].pop()
        assert (await client.post("/api/protocols/generated-drafts", json=_request(disconnected))).status_code == 422

        release = _draft_workflow()
        release["protocol_session_id"] = "pretend-approved"
        assert (await client.post("/api/protocols/generated-drafts", json=_request(release))).status_code == 422

        unknown_labware = _draft_workflow()
        unknown_labware["deck"]["1"][0]["labware_id"] = "UNRESOLVED"
        response = await client.post("/api/protocols/generated-drafts", json=_request(unknown_labware))
        assert response.status_code == 422
        assert "unknown labware_id" in response.json()["detail"]
    assert storage.list_workflows() == []


def test_generated_draft_edit_and_copy_preserve_immutable_provenance(tmp_path):
    from pybravo.workflow.protocols.api import _prepare_generated_draft

    storage = WorkflowStorage(tmp_path / "workflows")
    graph, issues = _prepare_generated_draft(_draft_workflow(), labware_ids={"source-plate"})
    source = storage.create_generated_draft(graph, provenance={
        "source_kind": "text2wetlab", "source_id": "example-task", "model": "local-qwen",
    }, issues=issues)

    edit = copy.deepcopy(source)
    edit["name"] = "Reviewed on canvas but still unapproved"
    updated = storage.update_generated_draft(source["id"], edit)
    assert updated["name"] == edit["name"]
    assert updated["protocol_draft_status"] == "unreviewed"
    assert updated["protocol_draft_validation_stale"] is True

    stripped = copy.deepcopy(updated)
    stripped.pop("protocol_generated_draft")
    with pytest.raises(ValueError, match="status cannot be removed"):
        storage.update_generated_draft(source["id"], stripped)
    changed_source = copy.deepcopy(updated)
    changed_source["protocol_generated_provenance"]["model"] = "different-model"
    with pytest.raises(ValueError, match="provenance cannot be changed"):
        storage.update_generated_draft(source["id"], changed_source)

    copied = copy.deepcopy(updated)
    copied.pop("id")
    copied["name"] = "Editable copy"
    duplicate = storage.create_generated_copy(copied)
    assert duplicate["id"] != source["id"]
    assert duplicate["protocol_generated_root_id"] == source["id"]
    assert duplicate["protocol_generated_provenance"] == source["protocol_generated_provenance"]
    assert duplicate["protocol_generated_draft"] is True
    assert duplicate["protocol_draft_status"] == "unreviewed"
    assert updated["protocol_draft_issues"] == source["protocol_draft_issues"]
    assert duplicate["protocol_draft_issues"] == source["protocol_draft_issues"]
    assert storage.get_workflow(source["id"])["name"] == edit["name"]


def test_import_keeps_proposed_simulator_target_without_granting_approval():
    workflow = _draft_workflow()
    target = {"machine_id": "hardware-machine", "head_type": "HT_96_D_200", "tip_definition_id": "lt_250ul"}
    workflow["protocol_simulation_target"] = target
    normalized, _ = api._prepare_generated_draft(workflow, labware_ids={"source-plate"})
    assert normalized["protocol_simulation_target"] == target
    assert "approval" not in normalized
