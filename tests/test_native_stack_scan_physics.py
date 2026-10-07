"""Virtual stack counts retain an actual guarded native scan descent."""

from pathlib import Path

import pytest

from pybravo.bravo import Bravo
from pybravo.profile.profile import BravoProfile
from pybravo.state_machine.tasks import ScanStackHeightTask
from pybravo.types import Axis
from tests.test_physical_simulation import _pp_plate

PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"


def _bravo(count):
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    for index in range(count):
        plate = _pp_plate(f"stack-{index}")
        plate.metadata["test_identity"] = index
        bravo.deck.add(5, plate)
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)

    def raise_error(error):
        raise error.original_exception

    bravo._engine.set_error_handler(raise_error)
    return bravo


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 4])
async def test_native_stack_scan_sweeps_with_superdex_and_preserves_modeled_plates(count):
    pytest.importorskip("superdex.physics")
    from pybravo.physics.runtime import CollisionRehearsal

    bravo = _bravo(count)
    original = tuple(bravo.deck.get_stack(5).items)
    runtime = CollisionRehearsal(bravo)
    sweep = []
    try:
        await runtime.initialize()

        def guard(start, end):
            step = bravo._engine.current_task._current_step_name
            if step == "scan_with_plate_sensor":
                sweep.append((dict(start), dict(end)))
            runtime.check_motion(start, end)

        bravo.controller.set_motion_guard(guard)
        await runtime.set_context("scan", "sensor/ScanStackHeight", {"location": 5})
        result = await bravo.scan_stack_height(location=5)

        assert len(sweep) == 1
        before, after = sweep[0]
        assert after[Axis.Zg] - before[Axis.Zg] == pytest.approx(bravo.profile.safety.approach_height)
        assert result["trigger_zg_mm"] == after[Axis.Zg]
        assert result["inferred_count"] == count
        assert result["measured_height_mm"] == pytest.approx(bravo.deck.get_stack(5).get_total_height())
        assert result["scan_mode"] == "virtual_deck_geometry"
        assert result["sensor_response_modeled"] is False
        assert result["force_behavior_modeled"] is False
        current = bravo.deck.get_stack(5).items
        assert all(before is after for before, after in zip(original, current))
        assert [plate.metadata["test_identity"] for plate in current] == list(range(count))

        # A following native task forces the same scene to refresh all modeled
        # identities. A virtual count must not replace them with template clones.
        await runtime.set_context("dock", "system/DockGripper", {})
        await bravo.dock_gripper()
        report = runtime.report()
        assert report["status"] == "checked"
        assert report["moves_checked"] >= 10
        assert report["samples_checked"] > report["moves_checked"]
        assert report["qualification_granted"] is False
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


@pytest.mark.asyncio
async def test_scan_descent_guard_failure_keeps_position_and_result_pending():
    bravo = _bravo(4)
    task = ScanStackHeightTask(
        bravo.controller, bravo._teachpoints, bravo.profile, bravo.deck,
        location=5, template_labware=bravo.deck.get_stack(5).top,
    )
    try:
        await task._move_to_safe_start()
        await task._move_xy_to_scan()
        await task._move_to_scan_start()
        starting = bravo.get_all_positions()

        def reject_sweep(start, end):
            assert end[Axis.Zg] > start[Axis.Zg]
            raise RuntimeError("Modeled scan descent collides with an obstacle")

        bravo.controller.set_motion_guard(reject_sweep)
        with pytest.raises(RuntimeError, match="Modeled scan descent collides"):
            await task._scan_with_plate_sensor()
        assert bravo.get_all_positions() == starting
        assert task.result_payload()["status"] == "pending"
    finally:
        bravo.controller.set_motion_guard(None)
        bravo.disconnect()


@pytest.mark.asyncio
async def test_unreachable_virtual_stack_trigger_fails_before_scan_approach():
    bravo = _bravo(20)
    task = ScanStackHeightTask(
        bravo.controller, bravo._teachpoints, bravo.profile, bravo.deck,
        location=5, template_labware=bravo.deck.get_stack(5).top,
    )
    starting = bravo.get_all_positions()
    try:
        with pytest.raises(RuntimeError, match="trigger plane with approach clearance"):
            await task._move_to_scan_start()
        assert bravo.get_all_positions() == starting
        assert task.result_payload()["status"] == "pending"
    finally:
        bravo.disconnect()
