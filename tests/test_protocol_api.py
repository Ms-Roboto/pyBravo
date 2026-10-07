"""Release lifecycle exercised through HTTP and the real simulation executor."""

from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from pybravo.bravo import Bravo
from pybravo.controllers.simulation import SimulationController
from pybravo.web import server
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols import setup_recommendations as setup_recommendations_module
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
async def test_strict_protocol_simulation_disables_travel_delay_but_completes_tasks(environment, monkeypatch):
    calls = []
    original = SimulationController.set_move_timing_enabled

    def record_timing_choice(self, enabled):
        calls.append(enabled)
        original(self, enabled)

    monkeypatch.setattr(SimulationController, "set_move_timing_enabled", record_timing_choice)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        identity = session["id"]
        response = await client.post(f"/api/protocols/{identity}/simulate")
        assert response.status_code == 200, response.text
        await asyncio.wait_for(api._simulations[identity], timeout=10)
        saved = (await client.get(f"/api/protocols/{identity}")).json()
    assert calls == [False]
    assert saved["simulation"]["status"] == "passed"
    assert saved["simulation"]["events"][-1]["type"] == "workflow:complete"


@pytest.mark.asyncio
async def test_full_review_release_library_and_setup_lifecycle(environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.get("/protocol-assistant")).status_code == 200
        context = (await client.get("/api/protocols/context")).json()
        assert context["labware"] and context["tip_definitions"] and context["context_hash"]
        manifest_response = await client.get("/api/protocols/capabilities")
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        assert manifest["schema_version"] == "0.3.0"
        assert manifest["context_hash"] == context["context_hash"]
        assert "profile" not in manifest
        assert next(row for row in manifest["assistant_operations"] if row["id"] == "transfer")["lowers_to"] == [
            "liquid/Aspirate", "liquid/Dispense",
        ]
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
async def test_simulation_only_liquid_assumption_cannot_be_approved_or_released(environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        assumption = {
            "liquid_class_id": "existing-class",
            "distance_from_bottom_mm": 1.0,
            "basis": "unqualified_geometric_placeholder",
        }
        response = await client.patch(f"/api/protocols/{session['id']}", json={
            "revision": session["revision"],
            "setup": {"simulation_only_liquid_assumption": assumption},
        })
        assert response.status_code == 200, response.text
        session = response.json()
        identity = session["id"]
        approval = await client.post(f"/api/protocols/{identity}/approve", json={
            "scientist": "Test reviewer", "reviewed": True, "deck_confirmed": True,
            "qualification": "qualification_run", "revision": session["revision"],
        })
        assert approval.status_code == 409
        assert "simulation-only liquid assumption" in approval.json()["detail"]
        exported = await client.post(f"/api/protocols/{identity}/export-workflow")
        assert exported.status_code == 409
        assert "simulation-only liquid assumption" in exported.json()["detail"]
        published = await client.post(f"/api/protocols/{identity}/publish", json={"name": "Unsafe release"})
        assert published.status_code == 409
        assert "simulation-only liquid assumption" in published.json()["detail"]


@pytest.mark.asyncio
async def test_strictly_simulated_protocol_can_be_previewed_without_approval_or_storage(environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        identity = session["id"]
        no_simulation = await client.get(f"/api/protocols/{identity}/designer-preview")
        assert no_simulation.status_code == 409

        response = await client.post(f"/api/protocols/{identity}/simulate")
        assert response.status_code == 200, response.text
        await asyncio.wait_for(api._simulations[identity], timeout=10)
        before = (await client.get(f"/api/protocols/{identity}")).json()
        assert before["simulation"]["status"] == "passed"
        assert before["approval"] is None

        preview_response = await client.get(f"/api/protocols/{identity}/designer-preview")
        assert preview_response.status_code == 200, preview_response.text
        payload = preview_response.json()
        preview = payload["preview"]
        workflow = payload["workflow"]
        assert preview["session_id"] == identity
        assert preview["revision"] == before["revision"]
        assert preview["read_only"] is True
        assert preview["executable"] is False
        assert preview["approved"] is False
        assert preview["status"] == "unapproved"
        assert workflow["protocol_compiled_preview"] is True
        assert workflow["protocol_session_id"] == identity
        assert workflow["protocol_revision"] == before["revision"]
        assert [node["type"] for node in workflow["graph"]["nodes"]] == [
            "flow/Start", "system/Manual", "system/Wait", "flow/End",
        ]
        assert (await client.get(f"/api/protocols/{identity}")).json() == before
        assert server._get_workflow_storage().list_workflows() == []

        save = await client.post("/api/workflows", json=workflow)
        assert save.status_code == 409
        imported = await client.post("/api/workflows/import-json", files={
            "file": ("compiled-preview.json", json.dumps(workflow), "application/json"),
        })
        assert imported.status_code == 409
        assert server._get_workflow_storage().list_workflows() == []
        ordinary = await client.post("/api/workflows", json={"name": "Unrelated workflow", "graph": {"nodes": []}})
        assert ordinary.status_code == 200
        replace = await client.put(f"/api/workflows/{ordinary.json()['id']}", json=workflow)
        assert replace.status_code == 409
        assert (await client.get(f"/api/workflows/{ordinary.json()['id']}")).json()["name"] == "Unrelated workflow"
        with pytest.raises(HTTPException) as denied:
            api.check_execution_release("copied-preview", workflow, environment)
        assert denied.value.status_code == 409
        forced = server._get_workflow_storage().create_workflow({**copy.deepcopy(workflow), "id": "forced-preview"})
        cannot_run = await client.post(f"/api/workflows/{forced['id']}/simulate")
        assert cannot_run.status_code == 409
        cannot_remove_marker = await client.put(f"/api/workflows/{forced['id']}", json={"protocol_compiled_preview": False})
        assert cannot_remove_marker.status_code == 409

        changed = copy.deepcopy(before["plan"])
        changed["name"] = "Changed after simulation"
        edited = await client.patch(f"/api/protocols/{identity}", json={
            "revision": before["revision"], "plan": changed,
        })
        assert edited.status_code == 200
        stale = await client.get(f"/api/protocols/{identity}/designer-preview")
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_setup_recommendations_use_active_context_without_saving_draft(environment, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        unsaved_plan = copy.deepcopy(session["plan"])
        unsaved_plan["name"] = "Unsaved setup review"
        selected_id = session["source"]["paragraphs"][1]["id"]
        observed_source_ids = []
        original_recommend = setup_recommendations_module.recommend_setup

        def capture_selected_source(plan, setup, manifest, *, source=None, runtime_snapshot=None):
            observed_source_ids.append([paragraph["id"] for paragraph in source["paragraphs"]])
            return original_recommend(plan, setup, manifest, source=source,
                                      runtime_snapshot=runtime_snapshot)

        monkeypatch.setattr(setup_recommendations_module, "recommend_setup", capture_selected_source)
        response = await client.post("/api/protocols/setup-recommendations", json={
            "session_id": session["id"], "plan": unsaved_plan, "setup": {},
            "selected_paragraph_ids": [selected_id],
        })
        assert response.status_code == 200, response.text
        assert observed_source_ids == [[selected_id]]
        report = response.json()
        assert report["context_hash"] == (await client.get("/api/protocols/capabilities")).json()["context_hash"]
        assert all(key in report for key in ("recommendations", "unresolved", "blocked"))
        saved = (await client.get(f"/api/protocols/{session['id']}")).json()
        assert saved["revision"] == session["revision"]
        assert saved["plan"]["name"] == session["plan"]["name"]
        assert saved["selected_paragraph_ids"] == session["selected_paragraph_ids"]
        for selected in ([], ["not-a-paragraph"]):
            invalid = await client.post("/api/protocols/setup-recommendations", json={
                "session_id": session["id"], "plan": unsaved_plan,
                "selected_paragraph_ids": selected,
            })
            assert invalid.status_code == 422
        missing = await client.post("/api/protocols/setup-recommendations", json={
            "session_id": "missing-session", "plan": unsaved_plan,
        })
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_setup_recommendations_expose_read_only_unverified_runtime_evidence(environment, monkeypatch):
    snapshot = {
        "deck": {"4": ["Configured plate"]},
        "tipbox_inventory": {"2": {"labware_name": "Configured rack", "tip_id": "st_10ul",
                                   "rows": 16, "cols": 24, "occupied": ["0:0"]}},
        "positions": {"X": 123},
    }
    monkeypatch.setattr(environment, "get_state", lambda: snapshot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/protocols/setup-recommendations", json={
            "plan": {"name": "Inspect", "materials": [], "steps": [
                {"id": "inspect", "kind": "manual", "message": "Inspect the deck."}]},
        })
        assert response.status_code == 200, response.text
        advisory = response.json()["runtime_snapshot"]
        assert advisory["status"] == "software_known_unverified"
        assert advisory["physically_verified"] is False
        assert advisory["occupied_slots"] == [
            {"slot": 2, "labware_names": ["Configured rack"], "source": "software_tipbox_inventory"},
            {"slot": 4, "labware_names": ["Configured plate"], "source": "software_deck_state"},
        ]
        assert advisory["tipbox_inventory"][0]["software_occupied_count"] == 1
        assert "positions" not in advisory
        manifest = (await client.get("/api/protocols/capabilities")).json()
        assert "runtime_snapshot" not in manifest

        def unavailable_state():
            raise RuntimeError("software snapshot unavailable")

        monkeypatch.setattr(environment, "get_state", unavailable_state)
        response = await client.post("/api/protocols/setup-recommendations", json={
            "plan": {"name": "Inspect", "materials": [], "steps": [
                {"id": "inspect", "kind": "manual", "message": "Inspect the deck."}]},
        })
        assert response.status_code == 200, response.text
        assert response.json()["runtime_snapshot"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_reviewed_catalog_dead_volume_action_is_revision_checked_and_audited(environment, monkeypatch):
    context = {"context_hash": "test-catalog-revision", "labware": [
        {"id": "reviewed-plate", "dead_volume_ul": 6.5, "dead_volume_status": "reviewed"},
        {"id": "placeholder-plate", "dead_volume_ul": 4.0, "dead_volume_status": "placeholder"},
        {"id": "destination-plate", "dead_volume_ul": 1.0, "dead_volume_status": "reviewed"},
    ]}
    monkeypatch.setattr(api, "machine_context", lambda bravo: context)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        created = (await client.post("/api/protocols/from-text", json={
            "text": "Transfer 5 uL from each source to the destination.", "name": "Catalog defaults",
        })).json()
        plan = {"name": "Catalog defaults", "materials": [
            {"id": "source-a", "name": "Reviewed source", "labware_id": "reviewed-plate",
             "initial_volume_ul": None, "dead_volume_ul": None},
            {"id": "source-b", "name": "Placeholder source", "labware_id": "placeholder-plate",
             "initial_volume_ul": None, "dead_volume_ul": None},
            {"id": "source-c", "name": "Measured source", "labware_id": "reviewed-plate",
             "initial_volume_ul": 20, "dead_volume_ul": 9},
            {"id": "destination", "name": "Receive-only", "labware_id": "destination-plate",
             "initial_volume_ul": None, "dead_volume_ul": None},
        ], "steps": [
            {"id": f"move-{source_id}", "kind": "transfer", "source": source_id,
             "destination": "destination", "volume_ul": 5}
            for source_id in ("source-a", "source-b", "source-c")
        ], "decisions": [{"path": "/setup/head_mode", "value": {"subset_type": "all_barrels"},
                          "reason": "Scientist selected full head", "actor": "scientist"}]}
        saved_response = await client.patch(f"/api/protocols/{created['id']}", json={
            "revision": created["revision"], "plan": plan})
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()
        endpoint = f"/api/protocols/{created['id']}/apply-reviewed-dead-volumes"
        stale = await client.post(endpoint, json={"revision": created["revision"]})
        assert stale.status_code == 409
        response = await client.post(endpoint, json={"revision": saved["revision"]})
        assert response.status_code == 200, response.text
        updated = response.json()
        expected_plan = copy.deepcopy(saved["plan"])
        expected_plan["materials"][0]["dead_volume_ul"] = 6.5
        assert updated["plan"] == expected_plan
        assert updated["setup"] == saved["setup"]
        assert updated["revision"] == saved["revision"] + 1
        assert updated["history"][-1]["event"] == "reviewed_catalog_dead_volume_defaults"
        assert updated["model"]["catalog_defaults"] == [{
            "path": "/materials/0/dead_volume_ul", "value": 6.5,
            "labware_id": "reviewed-plate", "source": "reviewed_labware_catalog",
            "catalog_context_hash": "test-catalog-revision",
        }]
        repeated = await client.post(endpoint, json={"revision": updated["revision"]})
        assert repeated.status_code == 200
        assert repeated.json()["revision"] == updated["revision"]
        assert repeated.json()["history"] == updated["history"]


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
