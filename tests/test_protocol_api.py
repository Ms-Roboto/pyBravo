"""Release lifecycle exercised through HTTP and the real simulation executor."""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from pybravo.bravo import Bravo
from pybravo.web import server
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.store import ProtocolStore, RevisionConflict, workflow_digest
from pybravo.workflow.storage import WorkflowStorage


@pytest.fixture
def environment(tmp_path, monkeypatch):
    bravo = Bravo(mode="simulation")
    monkeypatch.setattr(api, "_store", ProtocolStore(tmp_path / "protocols"))
    monkeypatch.setattr(api, "_simulations", {})
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_workflow_storage", WorkflowStorage(tmp_path / "workflows"))
    yield bravo
    bravo.disconnect()


async def session_with_plan(client):
    response = await client.post("/api/protocols/from-text", json={"text": "Inspect the plate label manually.\nWait 2 seconds.", "name": "Review example"})
    assert response.status_code == 200, response.text
    session = response.json()
    paragraphs = session["source"]["paragraphs"]
    plan = {"name": "Review example", "materials": [], "steps": [
        {"id": "inspect", "kind": "manual", "message": "Inspect the plate label and return the plate to its original position.",
         "source_paragraph_ids": [paragraphs[0]["id"]]},
        {"id": "wait", "kind": "wait", "duration_s": 2.0, "source_paragraph_ids": [paragraphs[1]["id"]],
         "source_values": [{"field": "duration_s", "value": 2.0, "unit": "s", "paragraph_id": paragraphs[1]["id"]}]},
    ]}
    response = await client.patch(f"/api/protocols/{session['id']}", json={"revision": session["revision"], "plan": plan})
    assert response.status_code == 200, response.text
    return response.json()


async def simulate_and_approve(client, session):
    identity = session["id"]
    response = await client.post(f"/api/protocols/{identity}/validate")
    assert response.status_code == 200, response.text
    assert response.json()["validation"]["valid"], response.text
    response = await client.post(f"/api/protocols/{identity}/simulate")
    assert response.status_code == 200, response.text
    await asyncio.wait_for(api._simulations[identity], timeout=10)
    session = (await client.get(f"/api/protocols/{identity}")).json()
    assert session["simulation"]["status"] == "passed", session["simulation"]
    response = await client.post(f"/api/protocols/{identity}/approve", json={
        "scientist": "Test reviewer", "reviewed": True, "deck_confirmed": True,
        "qualification": "qualification_run", "revision": session["revision"], "notes": "Synthetic test only"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_full_review_release_library_and_setup_lifecycle(environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.get("/protocol-assistant")).status_code == 200
        context = (await client.get("/api/protocols/context")).json()
        assert context["labware"] and context["tip_definitions"] and context["context_hash"]
        session = await session_with_plan(client)
        denied = await client.post(f"/api/protocols/{session['id']}/export-workflow")
        assert denied.status_code == 409
        approved = await simulate_and_approve(client, session)
        identity = approved["id"]
        exported = (await client.post(f"/api/protocols/{identity}/export-workflow")).json()
        workflow = server._get_workflow_storage().get_workflow(exported["workflow_id"])
        assert workflow["protocol_session_id"] == identity
        assert not workflow.get("library")
        assert all(n["type"] != "logic/Script" for n in workflow["graph"]["nodes"])
        release = api.check_execution_release(workflow["id"], workflow, environment)
        assert release["session_id"] == identity
        published = await client.post(f"/api/protocols/{identity}/publish", json={"name": "Inspected procedure"})
        assert published.status_code == 200, published.text
        listing = (await client.get("/api/protocols/library")).json()["items"]
        assert listing[0]["source_revision"] == approved["revision"]
        reused = (await client.post(f"/api/protocols/library/{listing[0]['id']}/reuse")).json()
        assert reused["plan"] == approved["plan"]
        assert reused["approval"] is None and reused["simulation"] is None
        assert reused["id"] != identity
        saved = await client.post("/api/protocols/setups", json={"name": "Manual setup", "setup": {}, "materials": []})
        assert saved.status_code == 200
        assert (await client.get("/api/protocols/setups")).json()["items"][0]["name"] == "Manual setup"


@pytest.mark.asyncio
async def test_edits_invalidate_approval_and_stripped_markers_do_not_bypass_release(environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await simulate_and_approve(client, await session_with_plan(client))
        exported = (await client.post(f"/api/protocols/{session['id']}/export-workflow")).json()
        workflow = server._get_workflow_storage().get_workflow(exported["workflow_id"])
        changed = copy.deepcopy(workflow)
        changed.pop("protocol_session_id")
        changed["graph"]["nodes"][1]["properties"]["message"] = "A different experiment"
        with pytest.raises(HTTPException) as exc:
            api.check_execution_release(changed["id"], changed, environment)
        assert exc.value.status_code == 409
        session["plan"]["name"] = "Edited"
        changed_session = (await client.patch(f"/api/protocols/{session['id']}", json={
            "revision": session["revision"], "plan": session["plan"]})).json()
        assert changed_session["approval"] is None and changed_session["simulation"] is None
        assert changed_session["history"][-1]["approval"]["scientist"] == "Test reviewer"
        with pytest.raises(HTTPException) as exc:
            api.check_execution_release(workflow["id"], workflow, environment)
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_profile_drift_invalidates_simulation_and_gate_precedes_initialization(environment, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await simulate_and_approve(client, await session_with_plan(client))
        exported = (await client.post(f"/api/protocols/{session['id']}/export-workflow")).json()
        # Configuration says real machine, but the underlying controller stays a
        # simulator. The release guard must reject it before initialize is called.
        environment.connect()
        environment.profile.connection.controller_type = "agile"
        initialized = AsyncMock()
        monkeypatch.setattr(environment, "initialize", initialized)
        result = await client.post(f"/api/workflows/{exported['workflow_id']}/execute")
        assert result.status_code == 409, result.text
        initialized.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_selection_revision_and_unknown_fields_are_rejected(environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        path = f"/api/protocols/{session['id']}"
        result = await client.patch(path, json={"revision": 1, "setup": {"name": "stale"}})
        assert result.status_code == 409
        result = await client.patch(path, json={"revision": session["revision"], "selected_paragraph_ids": ["invented"]})
        assert result.status_code == 422
        result = await client.post(path + "/extract", json={
            "revision": session["revision"], "selected_paragraph_ids": []})
        assert result.status_code == 422
        result = await client.patch(path, json={"revision": session["revision"], "approval": {"approved": True}})
        assert result.status_code == 422
        result = await client.patch(path, json={"revision": session["revision"], "plan": {"name": "Unsafe", "library": "import os"}})
        assert result.status_code == 422


@pytest.mark.asyncio
async def test_simulator_construction_failure_is_recorded(environment, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        identity = session["id"]

        def failed_simulator(*args, **kwargs):
            raise RuntimeError("Synthetic simulator setup failure")

        monkeypatch.setattr(api, "Bravo", failed_simulator)
        result = await client.post(f"/api/protocols/{identity}/simulate")
        assert result.status_code == 200, result.text
        task = api._simulations.get(identity)
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
        updated = (await client.get(f"/api/protocols/{identity}")).json()
        assert updated["simulation"]["status"] == "failed"
        assert "Synthetic simulator setup failure" in updated["simulation"]["error"]
        assert updated["approval"] is None


def test_atomic_store_rejects_stale_results_and_path_traversal(tmp_path):
    store = ProtocolStore(tmp_path)
    record = store.create_session({"name": "x", "paragraphs": [{"id": "p", "text": "x"}]})
    changed = store.update_session(record["id"], {"setup": {"name": "new"}}, revision=1)
    assert changed["revision"] == 2
    with pytest.raises(RevisionConflict):
        store.annotate(record["id"], {"approval": {"forged": True}}, revision=1)
    with pytest.raises(ValueError):
        store.get("sessions", "../../secret")
    assert not list(tmp_path.rglob("*.tmp"))


def test_release_hash_ignores_canvas_but_preserves_executable_properties():
    wf = {"graph": {"nodes": [{"id": 1, "type": "system/Wait", "properties": {"duration_s": 2}, "pos": [0, 0]}], "links": []}, "deck": {}}
    moved = copy.deepcopy(wf)
    moved["graph"]["nodes"][0]["pos"] = [100, 200]
    assert workflow_digest(moved) == workflow_digest(wf)
    moved["graph"]["nodes"][0]["properties"]["duration_s"] = 3
    assert workflow_digest(moved) != workflow_digest(wf)
    rewired = copy.deepcopy(wf)
    rewired["graph"]["nodes"][0]["outputs"] = [{"links": [9]}]
    assert workflow_digest(rewired) != workflow_digest(wf)


def test_tip_offset_calibration_changes_context_fingerprint(environment, monkeypatch):
    from pybravo.tip_offsets import TipOffsetEntry, TipOffsetTable
    from pybravo.workflow.protocols import context

    before = machine_context(environment)
    monkeypatch.setattr(context, "get_tip_offset_table", lambda: TipOffsetTable([
        TipOffsetEntry(head_type=before["head_type"], tipbox_id="rack", tips_on_z_offset=2.0)
    ]))
    after = machine_context(environment)
    assert before["context_hash"] != after["context_hash"]
    assert after["tip_offsets"][0]["tips_on_z_offset"] == 2.0


@pytest.mark.asyncio
async def test_original_pdf_is_retained_and_downloadable(environment, monkeypatch):
    from tests.test_protocol_ingest import _pdf

    monkeypatch.delenv("PYBRAVO_DOCLING_URL", raising=False)
    original = _pdf()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        result = await client.post("/api/protocols/ingest", files={"file": ("source.pdf", original, "application/pdf")})
        assert result.status_code == 200, result.text
        session = result.json()
        assert session["source"]["metadata"]["original_pdf"]
        downloaded = await client.get(f"/api/protocols/{session['id']}/source-pdf")
        assert downloaded.status_code == 200
        assert downloaded.content == original
        assert downloaded.headers["content-type"] == "application/pdf"
        assert downloaded.headers["content-disposition"].startswith("inline")
