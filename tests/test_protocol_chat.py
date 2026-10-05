"""Designer chat proposals persist atomically and never execute robot actions."""

from __future__ import annotations

import copy

import httpx
import pytest

from pybravo.web import server
from pybravo.workflow.protocols import api, llm
from pybravo.workflow.protocols.models import ProtocolPlan
from pybravo.workflow.protocols.store import ProtocolStore


@pytest.fixture
def chat_environment(tmp_path, monkeypatch):
    store = ProtocolStore(tmp_path / "protocols")
    monkeypatch.setattr(api, "_store", store)
    # No hardware object is needed for this endpoint. Context is read-only.
    monkeypatch.setattr(api, "_bravo", lambda: None)
    monkeypatch.setattr(api, "machine_context", lambda bravo: {"context_hash": "synthetic", "labware": []})
    calls = []

    async def extract(source, *, context=None, **kwargs):
        calls.append({"source": source.model_dump(), "context": copy.deepcopy(context)})
        paragraph = source.paragraphs[-1]
        plan = ProtocolPlan.model_validate({
            "name": "Proposed label check", "steps": [{"id": "check", "kind": "manual",
                "message": paragraph.text, "source_paragraph_ids": [paragraph.id]}],
            "questions": [{"id": "confirm-plate", "path": "/steps/0/message", "prompt": "Which plate should be checked?"}],
        })
        return llm.ExtractionResult(plan=plan, metadata={"provider": "local", "model": "qwen"})

    monkeypatch.setattr(llm, "extract_protocol_plan", extract)
    return store, calls


async def test_chat_creates_cited_source_transcript_and_resumable_preview(chat_environment):
    store, calls = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        result = await client.post("/api/protocols/chat", json={"message": "Inspect the plate label."})
        assert result.status_code == 200, result.text
        payload = result.json()
        session = payload["session"]
        assert session["revision"] == 1
        assert session["name"] == session["source"]["name"] == "Proposed label check"
        assert [item["role"] for item in session["chat_messages"]] == ["user", "assistant"]
        paragraph = session["source"]["paragraphs"][0]
        assert paragraph["text"] == "Inspect the plate label."
        assert session["chat_messages"][0]["source_paragraph_id"] == paragraph["id"]
        assert session["plan"]["steps"][0]["source_paragraph_ids"] == [paragraph["id"]]
        assert session["approval"] is None and session["simulation"] is None and session["runs"] == []
        assert payload["reply"] == "Proposed 1 step for review. Which plate should be checked?"
        assert payload["preview"]["protocol_chat_draft"] is True
        assert payload["preview"]["protocol_chat_session_id"] == session["id"]
        assert payload["preview"]["protocol_revision"] == 1
        assert payload["preview"]["questions"]
        assert calls[0]["context"]["current_plan"] is None
        assert len(store.list("sessions")) == 1
        resumed = await client.get(f"/api/protocols/{session['id']}/chat-preview")
        assert resumed.status_code == 200
        assert resumed.json() == payload


async def test_chat_followup_preserves_messages_sources_and_requires_revision(chat_environment):
    store, calls = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        first = (await client.post("/api/protocols/chat", json={"message": "Inspect plate A."})).json()["session"]
        identity = first["id"]
        missing = await client.post("/api/protocols/chat", json={"session_id": identity, "message": "Use plate B."})
        assert missing.status_code == 409
        assert len(calls) == 1
        changed = await client.post("/api/protocols/chat", json={"session_id": identity, "revision": 1, "message": "Use plate B."})
        assert changed.status_code == 200, changed.text
        session = changed.json()["session"]
        assert session["revision"] == 2
        assert session["chat_messages"][:2] == first["chat_messages"]
        assert [p["text"] for p in session["source"]["paragraphs"]] == ["Inspect plate A.", "Use plate B."]
        assert calls[-1]["context"]["current_plan"] == first["plan"]
        assert session["history"][-1]["source"] == first["source"]
        assert session["history"][-1]["chat_messages"] == first["chat_messages"]
        stale = await client.post("/api/protocols/chat", json={"session_id": identity, "revision": 1, "message": "Lost message"})
        assert stale.status_code == 409
        assert len(calls) == 2
        assert store.get("sessions", identity) == session


async def test_chat_preview_shows_current_verified_tipbox_options(chat_environment, monkeypatch):
    _, calls = chat_environment
    choice = {"labware_id": "rack-384", "labware_name": "384 verified rack",
              "tip_definition_id": "tip-st10", "tip_name": "ST10", "rows": 16,
              "cols": 24, "wells": 384, "spacing_x_mm": 4.5,
              "spacing_y_mm": 4.5, "tip_capacity_ul": 10.0,
              "tip_length_mm": 19.9}
    current = {"context_hash": "synthetic", "head_type": "HT_384_D_70",
               "tipbox_choices": [choice], "tipbox_catalog_candidates": [], "tipbox_choices_reason": ""}
    monkeypatch.setattr(api, "machine_context", lambda bravo: current.copy())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        created = (await client.post("/api/protocols/chat", json={"message": "Inspect this rack."})).json()
        assert calls[-1]["context"]["tipbox_choices"] == [choice]
        assert created["preview"]["head_type"] == "HT_384_D_70"
        assert created["preview"]["tipbox_choices"] == [choice]
        current = {"context_hash": "changed", "head_type": "HT_16_D_ST",
                   "tipbox_choices": [], "tipbox_catalog_candidates": [{"labware_id": "unconfigured-rack",
                   "labware_name": "Short-tip candidate", "rows": 0, "cols": 0, "wells": 384,
                   "spacing_x_mm": 4.5, "spacing_y_mm": 4.5, "verified": False,
                   "missing_metadata": ["rows_cols", "tip_link"]}],
                   "tipbox_choices_reason": "No verified rack for this head."}
        resumed = (await client.get(f"/api/protocols/{created['session']['id']}/chat-preview")).json()
        assert resumed["preview"]["head_type"] == "HT_16_D_ST"
        assert resumed["preview"]["tipbox_choices"] == []
        assert resumed["preview"]["tipbox_catalog_candidates"] == current["tipbox_catalog_candidates"]
        assert resumed["preview"]["tipbox_choices_reason"] == "No verified rack for this head."


async def test_chat_title_updates_atomically_without_changing_citation_ids(chat_environment, monkeypatch):
    store, _ = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        first = (await client.post("/api/protocols/chat", json={"message": "Inspect plate A."})).json()["session"]
        original_extract = llm.extract_protocol_plan

        async def rename(*args, **kwargs):
            result = await original_extract(*args, **kwargs)
            result.plan.name = "Updated label inspection"
            return result

        monkeypatch.setattr(llm, "extract_protocol_plan", rename)
        response = await client.post("/api/protocols/chat", json={"session_id": first["id"], "revision": 1,
            "message": "Inspect plate B instead."})
        assert response.status_code == 200
        second = response.json()["session"]
        assert second["name"] == second["source"]["name"] == "Updated label inspection"
        assert second["source"]["source_id"] == first["source"]["source_id"]
        assert second["source"]["paragraphs"][0] == first["source"]["paragraphs"][0]
        assert second["history"][-1]["source"]["name"] == "Proposed label check"
        assert store.get("sessions", first["id"]) == second

        async def blank_name(*args, **kwargs):
            result = await original_extract(*args, **kwargs)
            result.plan.name = "   "
            return result

        monkeypatch.setattr(llm, "extract_protocol_plan", blank_name)
        response = await client.post("/api/protocols/chat", json={"session_id": second["id"], "revision": 2,
            "message": "Check the label carefully."})
        assert response.status_code == 200
        assert response.json()["session"]["name"] == "Updated label inspection"
        assert response.json()["session"]["plan"]["name"] == "Updated label inspection"
        initial_blank = await client.post("/api/protocols/chat", json={"message": "A new unnamed inspection."})
        assert initial_blank.status_code == 200
        assert initial_blank.json()["session"]["name"] == "Designer conversation"


async def test_chat_failed_extraction_commits_no_partial_initial_or_followup_message(chat_environment, monkeypatch):
    store, _ = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        initial = (await client.post("/api/protocols/chat", json={"message": "Inspect the plate."})).json()["session"]

        async def failure(*args, **kwargs):
            raise llm.ProtocolLLMError("Synthetic local model unavailable")

        monkeypatch.setattr(llm, "extract_protocol_plan", failure)
        failed = await client.post("/api/protocols/chat", json={"message": "A failed new request"})
        assert failed.status_code == 422
        assert len(store.list("sessions")) == 1
        failed_followup = await client.post("/api/protocols/chat", json={
            "message": "A failed correction", "session_id": initial["id"], "revision": initial["revision"]})
        assert failed_followup.status_code == 422
        assert store.get("sessions", initial["id"]) == initial


async def test_chat_concurrent_edit_prevents_stale_model_commit(chat_environment, monkeypatch):
    store, _ = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        initial = (await client.post("/api/protocols/chat", json={"message": "Inspect the plate."})).json()["session"]
        original_extract = llm.extract_protocol_plan

        async def race(*args, **kwargs):
            store.update_session(initial["id"], {"setup": {"name": "Concurrent edit"}}, revision=1)
            return await original_extract(*args, **kwargs)

        monkeypatch.setattr(llm, "extract_protocol_plan", race)
        result = await client.post("/api/protocols/chat", json={
            "session_id": initial["id"], "revision": 1, "message": "An outdated correction"})
        assert result.status_code == 409
        saved = store.get("sessions", initial["id"])
        assert saved["revision"] == 2
        assert saved["setup"]["name"] == "Concurrent edit"
        assert saved["source"] == initial["source"]
        assert saved["chat_messages"] == initial["chat_messages"]


async def test_chat_continuing_an_existing_protocol_preserves_original_source_selection(chat_environment):
    _, calls = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        original = (await client.post("/api/protocols/from-text", json={"text": "Inspect A.\nInspect B."})).json()
        selected = [original["source"]["paragraphs"][1]["id"]]
        updated = (await client.patch(f"/api/protocols/{original['id']}", json={"revision": 1,
            "selected_paragraph_ids": selected})).json()
        response = await client.post("/api/protocols/chat", json={"session_id": updated["id"], "revision": updated["revision"],
            "message": "Inspect C instead."})
        assert response.status_code == 200, response.text
        session = response.json()["session"]
        assert len(session["source"]["paragraphs"]) == 3
        assert session["selected_paragraph_ids"][0] == selected[0]
        assert [p["text"] for p in calls[0]["source"]["paragraphs"]] == ["Inspect B.", "Inspect C instead."]


async def test_chat_rejects_blank_unknown_approval_fields_and_invalid_revision(chat_environment):
    store, calls = chat_environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        for payload in ({"message": "  "}, {"message": "Inspect", "revision": 1},
                        {"message": "Inspect", "approval": True}):
            response = await client.post("/api/protocols/chat", json=payload)
            assert response.status_code == 422, response.text
        assert not calls
        assert not store.list("sessions")
