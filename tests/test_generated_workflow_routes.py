"""Saved model drafts stay editable and unreviewed through Designer CRUD."""

from __future__ import annotations

import httpx
import pytest

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

        for endpoint in ("simulate", "execute"):
            assert (await client.post(f"/api/workflows/{identity}/{endpoint}")).status_code == 409
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
