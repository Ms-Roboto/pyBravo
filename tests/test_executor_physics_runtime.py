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


async def test_snapshot_pose_seeds_collision_scene_and_atomic_initial_frame(monkeypatch):
    probe = _Probe()
    scene_initial_positions = []

    def scene_factory(robot):
        scene_initial_positions.append(robot.get_all_positions())
        return _Scene(robot, probe)

    bravo, executor, events = _executor(monkeypatch, probe, scene_factory=scene_factory)
    snapshot = {"X": 123.0, "Y": 24.0, "Z": 0.0, "W": 0.0, "G": 0.0, "Zg": -20.0}
    executor._runtime_state = {"positions": snapshot}
    try:
        assert bravo.get_position(Axis.Zg) == 0.0
        await executor.execute()
        assert events[-1]["type"] == "workflow:complete"
        assert len(scene_initial_positions) == 1
        assert scene_initial_positions[0] == snapshot
        initial = events[0]
        assert initial["type"] == "workflow:start"
        assert initial["positions"] == snapshot
        assert initial["runtime_state"]["tips_on_head"] is False
        assert events[1] == {"type": "workflow:positions", "positions": snapshot}
    finally:
        bravo.disconnect()


def _tip_exchange_graph():
    tasks = [
        ("flow/Start", {}),
        ("tips/TipsOn", {"location": 1, "head_mode": {"subset_type": "column", "subset_config": "back_left", "column_count": 1}}),
        ("tips/TipsOff", {"location": 4}),
        ("tips/TipsOn", {"location": 1, "head_mode": {"subset_type": "column", "subset_config": "back_left", "column_count": 1}}),
        ("tips/TipsOff", {"location": 4}),
        ("flow/End", {}),
    ]
    return {
        "nodes": [
            {"id": index, "type": kind, "properties": props, "outputs": [{"links": [index]}]}
            for index, (kind, props) in enumerate(tasks, start=1)
        ],
        "links": [[index, index, 0, index + 1, 0, -1] for index in range(1, len(tasks))],
    }


async def test_native_tip_exchange_frames_attach_at_press_and_return_before_retract(monkeypatch):
    bravo, _ = _make_workflow_executor_bravo()
    deck = {
        "1": [{"labware_id": "tipbox-384", "tipbox_fill_state": "full"}],
        "4": [{"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}],
    }
    _, executor, events = _executor(monkeypatch, _Probe(), bravo=bravo, graph=_tip_exchange_graph(), deck=deck)
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:complete", events[-1]
        initial = events[0]["runtime_state"]
        assert initial["tipbox_removed_cells"]["1"] == []
        assert len(initial["tipbox_removed_cells"]["4"]) == 384
        exchanges = [(index, event) for index, event in enumerate(events) if event["type"] == "workflow:tips_change"]
        assert len(exchanges) == 4
        for operation, (index, event) in enumerate(exchanges):
            picked = operation % 2 == 0
            assert event["tips_on"] is picked
            assert event["tips_on_head"] is picked
            assert event["step_name"] == ("tip_press_dwell" if picked else "eject_tips")
            assert event["node_id"] == operation + 2
            assert len(event["tipbox_removed_cells"]["1"]) == 16 * (operation // 2 + 1)
            assert len(event["tipbox_removed_cells"]["4"]) == 384 - 16 * ((operation + 1) // 2)
            if picked:
                assert event["tips_on_head_mode"]["column_count"] == 1
                assert event["tips_on_head_selection"]["row_count"] == 16
                assert event["attached_tip_length_mm"] > 0
                assert event["tip_definition_id"] == "st_10ul"
            else:
                assert event["tips_on_head_mode"] is None
                assert event["tips_on_head_selection"] is None
                assert event["attached_tip_length_mm"] is None
            # Exchange is exactly at the successful native contact/ejection
            # frame, before the next native retract moves the head away.
            assert events[index - 1]["type"] == "workflow:positions"
            assert events[index - 1]["positions"]["Z"] > 0
            next_position = next(e for e in events[index + 1:] if e["type"] == "workflow:positions")
            assert next_position["positions"]["Z"] == bravo.profile.safety.z_safe_position
        assert len(bravo._occupied_tip_wells(1)) == 384 - 32
        assert len(bravo._occupied_tip_wells(4)) == 32
        assert not bravo._tips_on_head
    finally:
        bravo.disconnect()


async def test_failed_native_tip_press_never_emits_attachment(monkeypatch):
    from pybravo.state_machine.tasks import TipsOnTask

    bravo, _ = _make_workflow_executor_bravo()
    _, executor, events = _executor(
        monkeypatch, _Probe(), bravo=bravo, graph=_tip_exchange_graph(),
        deck={"1": [{"labware_id": "tipbox-384", "tipbox_fill_state": "full"}]},
    )
    monkeypatch.setattr(TipsOnTask, "_lower_z_to_tips", AsyncMock(side_effect=RuntimeError("Press failed")))
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert "Press failed" in events[-1]["error"]
        assert not any(event["type"] == "workflow:tips_change" for event in events)
        assert len(bravo._occupied_tip_wells(1)) == 384
        assert not bravo._tips_on_head
    finally:
        bravo.disconnect()


async def test_initially_mounted_snapshot_keeps_source_holes_after_return_and_next_pickup(monkeypatch):
    bravo, _ = _make_workflow_executor_bravo()
    bravo.controller.set_move_timing_enabled(False)
    bravo.set_labware(1, "tipbox-384")
    bravo.set_head_mode("column", "back_left", column_count=1)
    await bravo.tips_on(1)
    snapshot = bravo.get_state()
    graph = _tip_exchange_graph()
    # Already carrying tips at entry: start with the first return.
    graph["links"][0][3] = 3
    _, executor, events = _executor(
        monkeypatch, _Probe(), bravo=bravo, graph=graph,
        deck={
            "1": [{"labware_id": "tipbox-384", "tipbox_fill_state": "full"}],
            "4": [{"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}],
        },
    )
    executor._runtime_state = snapshot
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:complete", events[-1]
        initial = events[0]["runtime_state"]
        assert initial["tips_on_head"] is True
        assert initial["tips_on_head_selection"]["col"] == 23
        assert set(initial["tipbox_removed_cells"]["1"]) == {f"{row}:23" for row in range(16)}
        exchanges = [event for event in events if event["type"] == "workflow:tips_change"]
        assert [event["tips_on_head"] for event in exchanges] == [False, True, False]
        assert [len(event["tipbox_removed_cells"]["1"]) for event in exchanges] == [16, 32, 32]
        assert exchanges[1]["tips_on_head_selection"]["col"] == 22
        assert len(bravo._occupied_tip_wells(1)) == 352
        assert len(bravo._spent_tip_wells(4)) == 32
    finally:
        bravo.disconnect()


def test_tip_display_inventory_honors_interleaved_rack_selection():
    from pybravo.head_mode import TipSelection

    selection = TipSelection(location=1, row=1, col=0, row_count=8, column_count=12, row_stride=2, col_stride=2)
    assert WorkflowExecutor._selection_keys(selection) == {
        f"{row}:{col}" for row in range(1, 16, 2) for col in range(0, 24, 2)
    }


@pytest.mark.parametrize("positions", [{"X": 10, "Zg": float("nan")}, {"X": 10, "Zg": -100}])
async def test_invalid_initial_pose_fails_before_moving_the_simulator(monkeypatch, positions):
    probe = _Probe()
    bravo, executor, events = _executor(monkeypatch, probe)
    executor._runtime_state = {"positions": positions}
    before = bravo.get_all_positions()
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert not any(event["type"] == "workflow:start" for event in events)
        assert bravo.get_all_positions() == before
        assert probe.calls == []
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
