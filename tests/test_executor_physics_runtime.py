"""Physical rehearsals retain failure reports and execute native task motion."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from pybravo.bravo import Bravo
from pybravo.physics import runtime as collision_runtime
from pybravo.types import Axis
from pybravo.workflow import executor as executor_module
from pybravo.workflow.executor import WorkflowExecutor
from tests.test_bravo_init import _make_workflow_executor_bravo
from tests.test_collision_runtime import _assert_owner_stopped, _Probe, _Scene, _wait_for


def _graph():
    return {
        "nodes": [
            {"id": 1, "type": "flow/Start", "properties": {}, "outputs": [{"links": [1]}]},
            {"id": 2, "type": "system/DockGripper", "properties": {}, "outputs": [{"links": [2]}]},
            {"id": 3, "type": "flow/End", "properties": {}},
        ],
        "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]],
    }


def _executor(monkeypatch, probe, *, scene_factory=None, bravo=None, graph=None, deck=None):
    runtime_class = collision_runtime.CollisionRehearsal

    def create_runtime(bravo):
        return runtime_class(bravo, scene_factory=scene_factory or (lambda robot: _Scene(robot, probe)))

    monkeypatch.setattr(collision_runtime, "CollisionRehearsal", create_runtime)
    # Support either a module-level import or a lazy optional-physics import.
    monkeypatch.setattr(executor_module, "CollisionRehearsal", create_runtime, raising=False)
    if bravo is None:
        bravo = Bravo(mode="simulation")
        bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    events = []
    executor = WorkflowExecutor.for_simulation(bravo, graph or _graph(), deck_config=deck, on_event=events.append)
    return bravo, executor, events


@pytest.mark.parametrize("field", ["strict_validation", "physical_simulation"])
def test_simulation_factory_rejects_disabling_required_checks(field):
    with pytest.raises(ValueError, match=f"cannot disable {field}"):
        WorkflowExecutor.for_simulation(object(), _graph(), **{field: False})


async def test_physical_mode_always_rejects_unknown_tasks_even_without_strict_flag(monkeypatch):
    probe = _Probe()
    graph = _graph()
    graph["nodes"][1].update(type="unsupported/InventedMotion", properties={})
    bravo, _, events = _executor(monkeypatch, probe, graph=graph)
    executor = WorkflowExecutor(bravo, graph, on_event=events.append, physical_simulation=True)
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert events[-1]["physical_simulation"]["status"] == "failed"
        assert not any(event["type"] == "workflow:complete" for event in events)
        assert probe.closed
    finally:
        bravo.disconnect()


async def test_completed_native_tasks_cannot_pass_without_valid_physical_report(monkeypatch):
    class IncompleteScene(_Scene):
        def report(self):
            return {**super().report(), "status": "not_checked"}

    probe = _Probe()
    bravo, executor, events = _executor(monkeypatch, probe, scene_factory=lambda robot: IncompleteScene(robot, probe))
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert "without a completed SuperDex collision report" in events[-1]["error"]
        assert events[-1]["physical_simulation"]["status"] == "failed"
        assert not any(event["type"] == "workflow:complete" for event in events)
        assert bravo.controller._motion_guard is None
        assert probe.closed
    finally:
        bravo.disconnect()


async def test_native_primitive_motion_uses_owner_and_completion_reads_closed_cache(monkeypatch):
    probe = _Probe()
    bravo, executor, events = _executor(monkeypatch, probe)
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:complete"
        assert events[-1]["status"] == "ok"
        assert events[-1]["physical_simulation"]["status"] == "checked"
        assert events[-1]["physical_simulation"]["moves_checked"] > 0
        assert events[-1]["physical_simulation"]["qualification_granted"] is False
        assert any(event["type"] == "workflow:positions" for event in events)
        assert any(event["type"] == "workflow:node_step" for event in events)
        step_reports = [event["physical_simulation"] for event in events if event["type"] == "workflow:node_step"]
        assert any(report["moves_checked"] > 0 for report in step_reports)
        assert probe.closed
        assert {ident for _, ident in probe.calls} == {probe.owner.ident}
        assert probe.calls[-1][0] == "close"
        assert bravo.controller._motion_guard is None
        _assert_owner_stopped(probe)
    finally:
        bravo.disconnect()


async def test_deck_setup_failure_has_failed_physical_report_and_restores_handlers(monkeypatch):
    probe = _Probe()
    bravo, executor, events = _executor(monkeypatch, probe)
    def prior_step(*args):
        pass

    def prior_error(*args):
        pass
    bravo._engine.set_step_handler(prior_step)
    bravo._engine.set_error_handler(prior_error)
    monkeypatch.setattr(executor, "_setup_deck", AsyncMock(side_effect=ValueError("Changed tip inventory")))
    try:
        await executor.execute()
        assert [event["type"] for event in events] == ["workflow:error"]
        report = events[-1]["physical_simulation"]
        assert report["status"] == "failed"
        assert report["qualification_granted"] is False
        assert "Changed tip inventory" in str(report)
        assert bravo._engine._on_step_complete is prior_step
        assert bravo._engine._on_error is prior_error
        assert bravo.controller._motion_guard is None
        assert probe.calls == []
    finally:
        bravo.disconnect()


async def test_missing_native_sdk_is_a_failed_event_with_no_surviving_owner(monkeypatch):
    probe = _Probe()

    def missing_scene(bravo):
        probe.record("construct")
        raise RuntimeError("SuperDex SDK unavailable")

    bravo, executor, events = _executor(monkeypatch, probe, scene_factory=missing_scene)
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert events[-1]["physical_simulation"]["status"] == "failed"
        assert events[-1]["physical_simulation"]["qualification_granted"] is False
        assert "SuperDex SDK unavailable" in events[-1]["error"]
        assert not any(event["type"] == "workflow:complete" for event in events)
        assert bravo.controller._motion_guard is None
        _assert_owner_stopped(probe)
    finally:
        bravo.disconnect()


async def test_native_motion_failure_stops_workflow_before_axis_mutation(monkeypatch):
    probe = _Probe(motion_error=True)
    bravo, executor, events = _executor(monkeypatch, probe)
    original_positions = bravo.get_all_positions()
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert "Gripper intersects source plate" in events[-1]["error"]
        assert events[-1]["physical_simulation"]["status"] == "failed"
        assert bravo.get_all_positions() == original_positions
        assert not any(event["type"] == "workflow:complete" for event in events)
        assert bravo.controller._motion_guard is None
        assert probe.closed
        _assert_owner_stopped(probe)
    finally:
        bravo.disconnect()


async def test_designer_home_axis_names_dispatch_native_guarded_homing(monkeypatch):
    probe = _Probe()
    graph = _graph()
    graph["nodes"][1].update(type="system/Home", properties={"axes": "Zg,G"})
    bravo, executor, events = _executor(monkeypatch, probe, graph=graph)
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:complete"
        assert events[-1]["status"] == "ok"
        report = events[-1]["physical_simulation"]
        assert report["status"] == "checked"
        assert report["moves_checked"] > 0
        assert report["qualification_granted"] is False
        assert bravo.controller.is_axis_homed(Axis.G)
        assert bravo.controller.is_axis_homed(Axis.Zg)
        assert bravo.get_position(Axis.Zg) == -20
        assert any(event["type"] == "workflow:node_step" for event in events)
        assert probe.closed
        assert bravo.controller._motion_guard is None
        _assert_owner_stopped(probe)
    finally:
        bravo.disconnect()


async def test_designer_home_unknown_axis_fails_without_native_motion(monkeypatch):
    probe = _Probe()
    graph = _graph()
    graph["nodes"][1].update(type="system/Home", properties={"axes": "G,nonexistent_axis"})
    bravo, executor, events = _executor(monkeypatch, probe, graph=graph)
    original_positions = bravo.get_all_positions()
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert "Unknown Bravo home axis" in events[-1]["error"]
        report = events[-1]["physical_simulation"]
        assert report["status"] == "failed"
        assert report["moves_checked"] == 0
        assert report["qualification_granted"] is False
        assert not any(operation == "guard" for operation, _ in probe.calls)
        assert bravo.get_all_positions() == original_positions
        assert not any(event["type"] == "workflow:complete" for event in events)
        assert probe.closed
        assert bravo.controller._motion_guard is None
        _assert_owner_stopped(probe)
    finally:
        bravo.disconnect()


async def test_cancel_during_native_guard_drains_method_before_scene_disposal(monkeypatch):
    probe = _Probe(guard_release=threading.Event())
    native_method_finished = threading.Event()

    class _DrainCheckedScene(_Scene):
        def close(self):
            assert native_method_finished.is_set(), "Scene disposed while the native method still owned its worker"
            assert self.probe.active_guards == 0
            super().close()

    bravo, deck = _make_workflow_executor_bravo()
    graph = _graph()
    graph["nodes"][1].update(type="plate/PickPlace", properties={"pick_location": 5, "place_location": 6})
    bravo, executor, events = _executor(
        monkeypatch, probe, scene_factory=lambda robot: _DrainCheckedScene(robot, probe),
        bravo=bravo, graph=graph, deck=deck,
    )
    original_pick_place = bravo.pick_place

    async def pick_place_and_record(**params):
        try:
            return await original_pick_place(**params)
        finally:
            native_method_finished.set()

    monkeypatch.setattr(bravo, "pick_place", pick_place_and_record)

    def prior_step(*args):
        pass

    def prior_error(*args):
        pass

    bravo._engine.set_step_handler(prior_step)
    bravo._engine.set_error_handler(prior_error)
    running = asyncio.create_task(executor.execute())
    try:
        try:
            await _wait_for(probe.guard_started.is_set)
            running.cancel()
            await asyncio.sleep(0.002)
            assert not running.done(), "Executor cancellation abandoned its native worker"
            assert not native_method_finished.is_set()
            assert not probe.closed
            assert probe.active_guards == 1
        finally:
            probe.guard_release.set()

        with pytest.raises(asyncio.CancelledError):
            await running
        assert native_method_finished.is_set()
        assert probe.closed
        assert not bravo._engine.is_busy
        assert bravo._engine._on_step_complete is prior_step
        assert bravo._engine._on_error is prior_error
        assert bravo.controller._motion_guard is None
        assert not any(event["type"] == "workflow:complete" for event in events)
        _assert_owner_stopped(probe)
    finally:
        probe.guard_release.set()
        if not running.done():
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
        bravo.disconnect()


@pytest.mark.parametrize("method_name", ["dock_gripper", "home"])
async def test_dock_and_home_guard_waits_leave_event_loop_responsive(method_name):
    probe = _Probe(guard_release=threading.Event())
    bravo = Bravo(mode="simulation")
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    runtime = collision_runtime.CollisionRehearsal(bravo, scene_factory=lambda robot: _Scene(robot, probe))

    def fail_task(error):
        raise RuntimeError(error.message)

    bravo._engine.set_error_handler(fail_task)
    await runtime.initialize()
    bravo.controller.set_motion_guard(runtime.check_motion)
    method = getattr(bravo, method_name)
    params = {"axes": [Axis.G, Axis.Zg]} if method_name == "home" else {}
    running = asyncio.create_task(method(**params))
    try:
        try:
            await _wait_for(probe.guard_started.is_set)
            # The native checker remains blocked. An event-loop heartbeat can
            # still run because this controller operation belongs to a worker.
            async with asyncio.timeout(0.25):
                await asyncio.sleep(0.002)
            assert not running.done()
            assert probe.active_guards == 1
            assert not probe.closed
        finally:
            probe.guard_release.set()

        await running
        assert runtime.report()["moves_checked"] > 0
        assert not bravo._engine.is_busy
    finally:
        probe.guard_release.set()
        if not running.done():
            try:
                await running
            except RuntimeError:
                pass
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()
    _assert_owner_stopped(probe)
