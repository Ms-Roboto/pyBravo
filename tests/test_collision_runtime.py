"""Native collision resources have one owner even when task guards use threads."""

from __future__ import annotations

import asyncio
import copy
import sys
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from pybravo.physics.runtime import CollisionRehearsal


@dataclass
class _Probe:
    calls: list[tuple[str, int]] = field(default_factory=list)
    owner: threading.Thread | None = None
    constructor_started: threading.Event = field(default_factory=threading.Event)
    constructor_release: threading.Event | None = None
    constructor_delay: float = 0.0
    close_delay: float = 0.0
    close_release: threading.Event | None = None
    constructor_finished_at: float = 0.0
    close_started_at: float = 0.0
    close_finished_at: float = 0.0
    active_guards: int = 0
    maximum_active_guards: int = 0
    guard_started: threading.Event = field(default_factory=threading.Event)
    guard_release: threading.Event | None = None
    closed: bool = False
    context_error: bool = False
    motion_error: bool = False
    report_error: bool = False
    close_error: bool = False

    def record(self, operation):
        if self.owner is None:
            self.owner = threading.current_thread()
        self.calls.append((operation, threading.get_ident()))


class _SceneError(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        self.details = {"body": "robot/gripper", "message": message}


class _Scene:
    def __init__(self, bravo, probe):
        self.probe = probe
        self.context = {}
        self.moves_checked = 0
        probe.record("construct")
        probe.constructor_started.set()
        if probe.constructor_release is not None:
            assert probe.constructor_release.wait(2), "Test did not release collision construction"
        time.sleep(probe.constructor_delay)
        probe.constructor_finished_at = time.monotonic()

    def set_context(self, node_id, node_type, properties):
        self.probe.record("context")
        self.context = {"node_id": node_id, "node_type": node_type, "properties": copy.deepcopy(properties)}
        if self.probe.context_error:
            raise _SceneError("Unmodeled lid geometry")

    def check_motion(self, start, end):
        self.probe.record("guard")
        self.probe.active_guards += 1
        self.probe.maximum_active_guards = max(self.probe.maximum_active_guards, self.probe.active_guards)
        self.probe.guard_started.set()
        try:
            if self.probe.guard_release is not None:
                assert self.probe.guard_release.wait(2), "Test did not release the native motion guard"
            time.sleep(0.015)
            if self.probe.motion_error:
                raise _SceneError("Gripper intersects source plate")
            self.moves_checked += 1
        finally:
            self.probe.active_guards -= 1

    def report(self):
        self.probe.record("report")
        assert not self.probe.closed, "Report accessed an already destroyed native scene"
        if self.probe.report_error:
            raise _SceneError("Scene report unavailable")
        # Deliberately do not turn caught errors into a failure here: unexpected
        # scene errors must also fail the runtime's report, not look successful.
        return {
            "engine": "SuperDex",
            "status": "checked",
            "moves_checked": self.moves_checked,
            "qualification_granted": False,
            "last_error": None,
            "context": copy.deepcopy(self.context),
        }

    def close(self):
        self.probe.record("close")
        assert not self.probe.closed, "Native scene was closed twice"
        self.probe.close_started_at = time.monotonic()
        if self.probe.close_release is not None:
            assert self.probe.close_release.wait(2), "Test did not release collision disposal"
        time.sleep(self.probe.close_delay)
        self.probe.closed = True
        self.probe.close_finished_at = time.monotonic()
        if self.probe.close_error:
            raise _SceneError("Native disposal failed")


def _runtime(probe):
    return CollisionRehearsal(SimpleNamespace(), scene_factory=lambda bravo: _Scene(bravo, probe))


async def _wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def _assert_owner_stopped(probe):
    assert probe.owner is not None
    assert not probe.owner.is_alive(), "Collision owner thread survived cleanup"


async def test_scene_lifecycle_and_parallel_motion_guards_have_one_serial_owner():
    probe = _Probe()
    runtime = _runtime(probe)
    event_loop_thread = threading.get_ident()
    try:
        await runtime.initialize()
        await runtime.set_context("pick-1", "plate/PickPlace", {"from_location": 1, "to_location": 3})
        await asyncio.gather(*(asyncio.to_thread(runtime.check_motion, {"Zg": 0}, {"Zg": i}) for i in range(4)))
        assert runtime.report()["moves_checked"] == 4
        assert probe.maximum_active_guards == 1
    finally:
        await runtime.close()

    assert {ident for _, ident in probe.calls} == {probe.owner.ident}
    assert probe.owner.ident != event_loop_thread
    assert [operation for operation, _ in probe.calls].count("close") == 1
    assert probe.calls[-1][0] == "close"
    _assert_owner_stopped(probe)


async def test_slow_scene_construction_keeps_the_event_loop_responsive():
    probe = _Probe(constructor_delay=0.08)
    runtime = _runtime(probe)
    ticks = []
    initialize = asyncio.create_task(runtime.initialize())
    try:
        for _ in range(20):
            await asyncio.sleep(0.002)
            ticks.append(time.monotonic())
        await initialize
        assert any(tick < probe.constructor_finished_at for tick in ticks)
    finally:
        await runtime.close()
    _assert_owner_stopped(probe)


async def test_report_is_detached_and_remains_available_after_idempotent_close():
    probe = _Probe()
    runtime = _runtime(probe)
    await runtime.initialize()
    await runtime.set_context("asp-1", "liquid/Aspirate", {"well": "A1"})
    first = runtime.report()
    first["context"]["properties"]["well"] = "H12"
    first["status"] = "invented"
    assert runtime.report()["context"]["properties"]["well"] == "A1"
    assert runtime.report()["status"] == "checked"

    await runtime.close()
    calls_after_close = list(probe.calls)
    await runtime.close()
    assert runtime.report()["context"]["node_id"] == "asp-1"
    assert probe.calls == calls_after_close
    _assert_owner_stopped(probe)


async def test_slow_native_close_keeps_the_event_loop_responsive():
    probe = _Probe(close_delay=0.08)
    runtime = _runtime(probe)
    await runtime.initialize()
    closing = asyncio.create_task(runtime.close())
    await _wait_for(lambda: probe.close_started_at > 0)
    tick = time.monotonic()
    await asyncio.sleep(0.002)
    assert not probe.closed
    await closing
    assert tick < probe.close_finished_at
    _assert_owner_stopped(probe)


@pytest.mark.parametrize("failure", ["context", "motion"])
async def test_scene_errors_fail_cached_report_and_release_resources(failure):
    probe = _Probe(context_error=failure == "context", motion_error=failure == "motion")
    runtime = _runtime(probe)
    await runtime.initialize()
    try:
        with pytest.raises(_SceneError):
            if failure == "context":
                await runtime.set_context("lid-1", "plate/Delid", {})
            else:
                await runtime.set_context("pick-1", "plate/PickPlace", {})
                await asyncio.to_thread(runtime.check_motion, {"Zg": 0}, {"Zg": 5})
        report = runtime.report()
        assert report["status"] == "failed"
        assert report["qualification_granted"] is False
        assert "robot/gripper" in str(report["last_error"])
    finally:
        await runtime.close()
    assert runtime.report()["status"] == "failed"
    _assert_owner_stopped(probe)


async def test_initialization_failure_is_reported_and_stops_the_owner():
    probe = _Probe()

    def unavailable_scene(bravo):
        probe.record("construct")
        raise RuntimeError("Missing collision SDK")

    runtime = CollisionRehearsal(SimpleNamespace(), scene_factory=unavailable_scene)
    with pytest.raises(RuntimeError, match="Missing collision SDK"):
        await runtime.initialize()
    assert runtime.report()["status"] == "failed"
    assert runtime.report()["qualification_granted"] is False
    assert "Missing collision SDK" in str(runtime.report())
    _assert_owner_stopped(probe)
    await runtime.close()


async def test_cancelled_initialization_waits_for_native_scene_cleanup():
    probe = _Probe(constructor_release=threading.Event())
    runtime = _runtime(probe)
    initialize = asyncio.create_task(runtime.initialize())
    try:
        await _wait_for(probe.constructor_started.is_set)
        initialize.cancel()
        await asyncio.sleep(0.002)
        assert not initialize.done(), "Canceled initialization abandoned its still-running constructor"
    finally:
        probe.constructor_release.set()

    with pytest.raises(asyncio.CancelledError):
        await initialize
    assert probe.closed
    assert runtime.report()["qualification_granted"] is False
    _assert_owner_stopped(probe)
    await runtime.close()


async def test_cancelled_close_waits_for_native_disposal_and_joins_worker():
    probe = _Probe(close_release=threading.Event())
    runtime = _runtime(probe)
    await runtime.initialize()
    closing = asyncio.create_task(runtime.close())
    try:
        await _wait_for(lambda: probe.close_started_at > 0)
        closing.cancel()
        await asyncio.sleep(0.002)
        assert not closing.done(), "Canceled close abandoned native disposal"
    finally:
        probe.close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert probe.closed
    _assert_owner_stopped(probe)
    await runtime.close()


async def test_close_failure_is_cached_and_worker_still_joins():
    probe = _Probe(close_error=True)
    runtime = _runtime(probe)
    await runtime.initialize()
    with pytest.raises(_SceneError, match="Native disposal failed"):
        await runtime.close()
    report = runtime.report()
    assert report["status"] == "failed"
    assert report["qualification_granted"] is False
    assert "Native disposal failed" in str(report["last_error"])
    _assert_owner_stopped(probe)
    await runtime.close()


async def test_initial_report_failure_disposes_the_successfully_constructed_scene():
    probe = _Probe(report_error=True)
    runtime = _runtime(probe)
    with pytest.raises(_SceneError, match="Scene report unavailable"):
        await runtime.initialize()
    assert probe.closed
    assert runtime.report()["status"] == "failed"
    assert "Scene report unavailable" in str(runtime.report()["last_error"])
    assert probe.calls[-1][0] == "close"
    _assert_owner_stopped(probe)


async def test_default_scene_factory_import_failure_is_reported(monkeypatch):
    # Exercise the lazy default factory rather than replacing it with a fake;
    # startup must fail visibly when optional scene dependencies cannot load.
    preparation_threads = []
    monkeypatch.setitem(sys.modules, "pybravo.physics.superdex_backend", SimpleNamespace(
        ensure_physics_context=lambda: preparation_threads.append(threading.get_ident()),
    ))
    monkeypatch.setitem(sys.modules, "pybravo.physics.scene", None)
    runtime = CollisionRehearsal(SimpleNamespace())
    with pytest.raises(ModuleNotFoundError, match="pybravo.physics.scene"):
        await runtime.initialize()
    assert runtime.report()["status"] == "failed"
    assert runtime.report()["qualification_granted"] is False
    assert "pybravo.physics.scene" in str(runtime.report()["last_error"])
    assert preparation_threads == [threading.get_ident()]
    await runtime.close()


async def test_missing_sdk_preparation_module_fails_before_owner_starts(monkeypatch):
    monkeypatch.setitem(sys.modules, "pybravo.physics.superdex_backend", None)
    runtime = CollisionRehearsal(SimpleNamespace())
    with pytest.raises(ModuleNotFoundError, match="pybravo.physics.superdex_backend"):
        await runtime.initialize()
    assert runtime.report()["status"] == "failed"
    assert runtime.report()["qualification_granted"] is False
    assert "pybravo.physics.superdex_backend" in str(runtime.report()["last_error"])
    await runtime.close()
