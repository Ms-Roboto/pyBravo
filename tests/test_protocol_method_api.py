"""Public method discovery and reviewed-adaptation release behavior."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from pybravo.bravo import Bravo
from pybravo.web import server
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols.store import ProtocolStore


@pytest.fixture
def method_api_environment(tmp_path, monkeypatch):
    bravo = Bravo(mode="simulation")
    monkeypatch.setattr(api, "_store", ProtocolStore(tmp_path / "protocols"))
    monkeypatch.setattr(api, "_simulations", {})
    monkeypatch.setattr(server, "_bravo", bravo)
    yield bravo
    bravo.disconnect()


@pytest.mark.asyncio
async def test_method_registry_and_lookup_are_read_only(method_api_environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        registry = await client.get("/api/protocols/methods")
        assert registry.status_code == 200, registry.text
        assert registry.json()["standard"] == "pybravo.bravo-method-registry"
        assert registry.json()["digest"]
        recipes = await client.get("/api/protocols/recipes")
        assert recipes.status_code == 200, recipes.text
        assert recipes.json()["standard"] == "pybravo.protocol-recipes"
        assert all(row["status"] == "synthetic_pattern" for row in recipes.json()["recipes"])

        lookup = await client.post("/api/protocols/methods/lookup", json={
            "operation": "transfer", "tip_id": "unknown", "tipbox_id": "unknown",
            "source_labware_id": "unknown", "destination_labware_id": "unknown",
            "volume_ul": 5,
        })
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()["issues"]
        assert lookup.json()["candidates"] == []
        assert registry.json()["digest"] == (await client.get("/api/protocols/methods")).json()["digest"]
        incomplete = await client.post("/api/protocols/methods", json={
            "expected_registry_digest": registry.json()["digest"],
            "method": {
                "method_id": "reviewed-incomplete", "version": "1.0.0", "status": "reviewed",
                "title": "Incomplete local method", "applicability": {},
            },
        })
        assert incomplete.status_code == 422, incomplete.text
        assert incomplete.json()["detail"]["missing_fields"]


@pytest.mark.asyncio
async def test_scientist_reviewed_simulated_is_distinct_from_qualification(method_api_environment):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        created = (await client.post("/api/protocols/from-text", json={
            "name": "Adaptation review", "text": "Wait 1 second.",
        })).json()
        paragraph = created["source"]["paragraphs"][0]["id"]
        edited = await client.patch(f"/api/protocols/{created['id']}", json={
            "revision": created["revision"], "plan": {
                "name": "Adaptation review", "steps": [{
                    "id": "wait", "kind": "wait", "duration_s": 1,
                    "source_paragraph_ids": [paragraph],
                    "source_values": [{"field": "duration_s", "value": 1, "unit": "s", "paragraph_id": paragraph}],
                }],
            },
        })
        assert edited.status_code == 200, edited.text
        identity = created["id"]
        validated = await client.post(f"/api/protocols/{identity}/validate")
        assert validated.status_code == 200 and validated.json()["validation"]["valid"]
        started = await client.post(f"/api/protocols/{identity}/simulate")
        assert started.status_code == 200, started.text
        await asyncio.wait_for(api._simulations[identity], timeout=10)
        current = (await client.get(f"/api/protocols/{identity}")).json()
        assert current["simulation"]["status"] == "passed"

        response = await client.post(f"/api/protocols/{identity}/approve", json={
            "revision": current["revision"], "scientist": "Example reviewer",
            "qualification": "scientist_reviewed_simulated", "reviewed": True,
            "deck_confirmed": True, "notes": "Reviewed the adaptation and strict simulation.",
        })
        assert response.status_code == 200, response.text
        assert response.json()["approval"]["qualification"] == "scientist_reviewed_simulated"


@pytest.mark.asyncio
async def test_method_difference_requires_explicit_acceptance(method_api_environment, monkeypatch):
    record = api._store.create_session({"name": "Review", "paragraphs": []}, plan={"name": "Review"})
    monkeypatch.setattr(api, "_require_passed", lambda current, context: ("passed", {"graph": {"nodes": [], "edges": []}}))
    monkeypatch.setattr(api, "_validate", lambda current, context: {
        "valid": True, "issues": [{"code": "method_mismatch", "severity": "warning", "message": "Different reagent family"}],
    })
    request = api.ApprovalRequest(
        scientist="Example reviewer", qualification="scientist_reviewed_simulated",
        reviewed=True, deck_confirmed=True, notes="Reviewed the reagent-family difference.",
        revision=record["revision"],
    )
    with pytest.raises(HTTPException) as exc:
        await api.approve(record["id"], request)
    assert exc.value.status_code == 422
    approved = await api.approve(record["id"], request.model_copy(update={"method_differences_accepted": True}))
    assert approved["approval"]["method_differences_accepted"] is True


@pytest.mark.asyncio
async def test_distribute_paired_fallback_requires_acceptance_and_rationale(method_api_environment, monkeypatch):
    record = api._store.create_session({"name": "Review", "paragraphs": []}, plan={"name": "Review"})
    monkeypatch.setattr(api, "_require_passed", lambda current, context: ("passed", {"graph": {"nodes": [], "edges": []}}))
    monkeypatch.setattr(api, "_validate", lambda current, context: {
        "valid": True, "issues": [{"code": "distribute_calibration_fallback", "severity": "warning",
                                   "message": "Separate aspiration-dispense pairs are required."}],
    })
    request = api.ApprovalRequest(
        scientist="Example reviewer", qualification="qualification_run",
        reviewed=True, deck_confirmed=True, revision=record["revision"],
    )
    with pytest.raises(HTTPException, match="changed aspiration sequence") as exc:
        await api.approve(record["id"], request)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException, match="paired aspiration-dispense fallback") as exc:
        await api.approve(record["id"], request.model_copy(update={"method_differences_accepted": True}))
    assert exc.value.status_code == 422
    approved = await api.approve(record["id"], request.model_copy(update={
        "method_differences_accepted": True,
        "notes": "The destination wells are empty and each paired stroke stays within the ST10 calibration.",
    }))
    assert approved["approval"]["method_differences_accepted"] is True
