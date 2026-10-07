"""Protocol readiness requires a current, completed collision rehearsal."""

from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
from fastapi import HTTPException

from pybravo.physics.contracts import PHYSICAL_SIMULATION_CONTRACT, is_checked_physical_report
from pybravo.web import server
from pybravo.workflow.executor import WorkflowExecutor
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.store import record_digest, workflow_digest
from tests.test_protocol_api import environment as environment
from tests.test_protocol_api import session_with_plan


def checked_report(**changes):
    return {
        "contract_version": PHYSICAL_SIMULATION_CONTRACT,
        "engine": "SuperDex", "engine_version": "1.0.0", "status": "checked",
        "moves_checked": 0, "samples_checked": 0, "contact_queries": 0,
        "qualification_granted": False, "last_error": None,
        **changes,
    }


@pytest.mark.parametrize("report", [
    None,
    {},
    checked_report(contract_version="old"),
    checked_report(engine="animation"),
    checked_report(engine_version=None),
    checked_report(engine_version=123),
    checked_report(engine_version="  "),
    checked_report(status="failed"),
    checked_report(status="initializing"),
    checked_report(last_error={"message": "Collision detected"}),
    checked_report(moves_checked=-1),
    checked_report(moves_checked=True),
    checked_report(samples_checked=float("nan")),
    checked_report(contact_queries=float("inf")),
    checked_report(moves_checked=1, samples_checked=0),
])
def test_unchecked_incomplete_and_malformed_reports_are_not_passes(report):
    assert not is_checked_physical_report(report)


def test_no_motion_and_checked_native_motion_are_both_supported():
    assert is_checked_physical_report(checked_report())
    assert is_checked_physical_report(checked_report(moves_checked=3, samples_checked=250, contact_queries=1200))


@pytest.mark.parametrize("final_report", [
    None,
    checked_report(contract_version="legacy"),
    checked_report(status="failed", last_error={"message": "Body intersection"}),
])
async def test_assistant_cannot_pass_using_an_earlier_checked_report(environment, monkeypatch, final_report):
    async def pretend_complete(self):
        assert self._strict_validation and self._physical_simulation and not self._preview_animation
        await self._emit({"type": "workflow:start", "physical_simulation": checked_report()})
        await self._emit({"type": "workflow:complete", "status": "ok", "physical_simulation": final_report})

    monkeypatch.setattr(WorkflowExecutor, "execute", pretend_complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        identity = session["id"]
        response = await client.post(f"/api/protocols/{identity}/simulate")
        assert response.status_code == 200, response.text
        task = api._simulations.get(identity)
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
        saved = (await client.get(f"/api/protocols/{identity}")).json()
    assert saved["simulation"]["status"] == "failed"
    assert "completed SuperDex collision report" in saved["simulation"]["error"]
    assert saved["simulation"]["physical_simulation"] == final_report
    assert saved["approval"] is None


async def test_missing_sdk_is_recorded_as_failed_collision_rehearsal(environment, monkeypatch):
    from pybravo.physics.runtime import CollisionRehearsal

    async def unavailable(self):
        raise ModuleNotFoundError("SuperDex SDK is not installed")

    monkeypatch.setattr(CollisionRehearsal, "initialize", unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        identity = session["id"]
        response = await client.post(f"/api/protocols/{identity}/simulate")
        assert response.status_code == 200, response.text
        task = api._simulations.get(identity)
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
        saved = (await client.get(f"/api/protocols/{identity}")).json()
    simulation = saved["simulation"]
    assert simulation["status"] == "failed"
    assert "SuperDex SDK is not installed" in simulation["error"]
    assert simulation["physical_simulation"]["status"] == "failed"
    assert simulation["physical_simulation"]["moves_checked"] == 0
    assert saved["approval"] is None


@pytest.mark.parametrize("report", [None, checked_report(contract_version="old"), checked_report(status="failed")])
async def test_old_pass_requires_rerun_but_preserves_scientist_approval_history(environment, report):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
        capabilities = machine_context(environment)
        workflow = api._compile(session, capabilities)
        approval = {"scientist": "Prior reviewer", "record_hash": record_digest(session, capabilities["context_hash"])}
        legacy = api._store.annotate(session["id"], {
            "simulation": {
                "status": "passed", "physical_simulation": report,
                "record_hash": approval["record_hash"], "workflow_hash": workflow_digest(workflow),
            }, "approval": approval,
        }, revision=session["revision"])
        # Release/preview enforcement must work even before a GET upgrades the display.
        with pytest.raises(HTTPException, match="current SuperDex collision checks") as error:
            api._require_passed(legacy, capabilities)
        assert error.value.status_code == 409
        history = copy.deepcopy(legacy["history"])
        displayed = (await client.get(f"/api/protocols/{session['id']}")).json()
        preview = await client.get(f"/api/protocols/{session['id']}/designer-preview")
    assert displayed["simulation"]["status"] == "rerun_required"
    assert "Run strict simulation again" in displayed["simulation"]["error"]
    assert displayed["approval"] == approval
    assert displayed["history"] == history
    assert displayed["revision"] == session["revision"]
    assert preview.status_code == 409


async def test_robot_asset_change_invalidates_saved_physical_simulation(environment, monkeypatch):
    from pybravo.workflow.protocols import context

    monkeypatch.setattr(context, "robot_assets_sha256", lambda: "first-cad-revision")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        session = await session_with_plan(client)
    before = machine_context(environment)
    workflow = api._compile(session, before)
    session = api._store.annotate(session["id"], {"simulation": {
        "status": "passed", "physical_simulation": checked_report(provenance={"robot_assets_sha256": "first-cad-revision"}),
        "record_hash": record_digest(session, before["context_hash"]),
        "workflow_hash": workflow_digest(workflow),
    }}, revision=session["revision"])
    api._require_passed(session, before)
    monkeypatch.setattr(context, "robot_assets_sha256", lambda: "changed-cad-revision")
    after = machine_context(environment)
    assert before["profile_hash"] == after["profile_hash"]
    assert before["context_hash"] != after["context_hash"]
    assert after["physical_simulation_geometry"]["robot_assets_sha256"] == "changed-cad-revision"
    with pytest.raises(HTTPException, match="successful strict simulation") as error:
        api._require_passed(session, after)
    assert error.value.status_code == 409
    monkeypatch.setattr(api, "robot_assets_sha256", lambda: "changed-cad-revision")
    displayed = await api.get_protocol(session["id"])
    assert displayed["simulation"]["status"] == "rerun_required"
    assert "geometry changed" in displayed["simulation"]["error"]


def test_machine_context_still_loads_when_robot_assets_are_missing(environment, monkeypatch):
    from pybravo.workflow.protocols import context

    def missing():
        raise FileNotFoundError("Missing robot.urdf")

    monkeypatch.setattr(context, "robot_assets_sha256", missing)
    capabilities = machine_context(environment)
    assert capabilities["context_hash"]
    assert capabilities["physical_simulation_geometry"]["robot_assets_sha256"] is None
    assert capabilities["physical_simulation_geometry"]["error"] == "Missing robot.urdf"
