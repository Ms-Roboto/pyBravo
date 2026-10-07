"""One owner thread for a collision scene and its native physics context.

Bravo primitive tasks call controllers from worker threads. The collision SDK
is thread-affine, so construction, checks, reporting, and disposal must instead
stay on one serialized worker. Only cached report snapshots leave that worker.
"""
from __future__ import annotations

import asyncio
import copy
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .contracts import PHYSICAL_SIMULATION_CONTRACT


def _create_scene(bravo: Any):
    # Import optional native dependencies on the owner, inside the failure
    # boundary. A missing SDK is a failed rehearsal, never an unchecked pass.
    from .scene import BravoCollisionScene

    return BravoCollisionScene(bravo)


async def _await_drained(future: Future | asyncio.Future):
    """Do not abandon an owned native operation when its waiter is canceled."""
    wrapped = asyncio.wrap_future(future) if isinstance(future, Future) else future
    try:
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        while not wrapped.done():
            try:
                await asyncio.shield(wrapped)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Retrieve a concurrent failure so cancellation does not leave an
        # unobserved exception. Ownership cleanup is the caller's responsibility.
        if wrapped.done() and not wrapped.cancelled():
            wrapped.exception()
        raise


class CollisionRehearsal:
    """Serialize a scene on a dedicated worker without blocking the event loop.

    ``check_motion`` is deliberately synchronous for SimulationController's
    atomic pre-move guard. Initialization, task context, and cleanup are awaited
    by the workflow. Cancellation waits for the owned operation and disposal;
    callers cannot leave a constructor or native scene running in the background.
    """

    def __init__(self, bravo: Any, *, scene_factory: Callable | None = None):
        self._bravo = bravo
        self._factory = scene_factory or _create_scene
        self._uses_default_factory = scene_factory is None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bravo-collision")
        self._lock = threading.RLock()
        self._owner_ident: int | None = None
        self._scene = None
        self._state = "new"
        self._initialize_future: Future | None = None
        self._close_future: Future | None = None
        self._shutdown_future: asyncio.Future | None = None
        self._failure: dict[str, Any] | None = None
        self._context: dict[str, Any] = {}
        self._report: dict[str, Any] = {
            "contract_version": PHYSICAL_SIMULATION_CONTRACT,
            "engine": "SuperDex",
            "engine_version": "1.0.0",
            "scope": "tool_meshes_and_catalog_deck",
            "status": "not_started",
            "moves_checked": 0,
            "samples_checked": 0,
            "contact_queries": 0,
            "qualification_granted": False,
            "last_error": None,
        }

    def report(self) -> dict[str, Any]:
        """Return an isolated cached snapshot, including after scene disposal."""
        with self._lock:
            return copy.deepcopy(self._report)

    def record_failure(self, error: BaseException | str, *, stage: str = "workflow") -> None:
        """Record orchestration failures even when scene construction never ran."""
        details = getattr(error, "details", None)
        with self._lock:
            # Keep the first fault and its collision evidence if cleanup also
            # fails. Later errors must not turn the rehearsal into a pass.
            if self._failure is None:
                self._failure = {
                    **copy.deepcopy(self._context),
                    **(copy.deepcopy(details) if isinstance(details, dict) else {}),
                    "message": str(error) or type(error).__name__,
                    "stage": stage,
                }
            self._report.update({
                "status": "failed",
                "qualification_granted": False,
                "last_error": copy.deepcopy(self._failure),
            })

    def _refresh_report_owned(self) -> None:
        if self._scene is None:
            return
        snapshot = copy.deepcopy(self._scene.report())
        with self._lock:
            snapshot["contract_version"] = PHYSICAL_SIMULATION_CONTRACT
            snapshot["qualification_granted"] = False
            if self._failure is not None:
                snapshot.update(status="failed", last_error=copy.deepcopy(self._failure))
            self._report = snapshot

    def _initialize_owned(self) -> None:
        self._owner_ident = threading.get_ident()
        try:
            self._scene = self._factory(self._bravo)
            self._refresh_report_owned()
        except BaseException as exc:
            self.record_failure(exc, stage="initialization")
            raise

    async def initialize(self) -> None:
        try:
            with self._lock:
                if self._state in {"closing", "closed"}:
                    raise RuntimeError("Collision rehearsal is closed")
                if self._state == "ready":
                    return
                if self._initialize_future is None:
                    if self._uses_default_factory:
                        # The SDK's process context must initialize on main,
                        # matching its atexit shutdown thread. Scene creation,
                        # CAD loading, contact checks and disposal stay owned.
                        from .superdex_backend import ensure_physics_context

                        ensure_physics_context()
                    self._state = "initializing"
                    self._report["status"] = "initializing"
                    self._initialize_future = self._pool.submit(self._initialize_owned)
                future = self._initialize_future
            await _await_drained(future)
        except BaseException as exc:
            self.record_failure(exc, stage="initialization")
            # A canceled constructor is allowed to finish, then its scene is
            # closed on the same thread before cancellation reaches the caller.
            try:
                await self.close()
            except BaseException:
                pass  # Preserve the original initialization fault/cancellation.
            raise
        with self._lock:
            if self._state not in {"initializing", "ready"}:
                raise RuntimeError("Collision rehearsal was closed during initialization")
            self._state = "ready"

    def _call_owned(self, name: str, *args) -> Any:
        try:
            result = getattr(self._scene, name)(*args)
            self._refresh_report_owned()
            return result
        except BaseException as exc:
            self.record_failure(exc, stage=name)
            try:
                self._refresh_report_owned()
            except BaseException:
                pass  # Preserve the original context/motion failure.
            raise

    def _submit_ready(self, name: str, *args) -> Future:
        with self._lock:
            if self._state != "ready":
                raise RuntimeError("Collision rehearsal is not ready or is closed")
            return self._pool.submit(self._call_owned, name, *args)

    async def set_context(self, node_id, node_type, properties) -> None:
        # Properties are a snapshot; Bravo itself remains a live read-only view
        # while the controller waits synchronously for each guard's answer.
        props = copy.deepcopy(properties)
        with self._lock:
            self._context = {"node_id": node_id, "node_type": node_type, "properties": props}
        await _await_drained(self._submit_ready("set_context", node_id, node_type, props))

    def check_motion(self, start, end) -> None:
        start, end = dict(start), dict(end)
        if threading.get_ident() == self._owner_ident:
            # Avoid deadlock should an SDK-backed operation make a nested guard.
            with self._lock:
                if self._state != "ready":
                    raise RuntimeError("Collision rehearsal is not ready or is closed")
            self._call_owned("check_motion", start, end)
            return
        self._submit_ready("check_motion", start, end).result()

    def _close_owned(self) -> None:
        if self._scene is None:
            return
        scene = self._scene
        try:
            # SDK queries must precede disposal. The final event reads only
            # this cache, even if scene.report would be invalid after close.
            self._refresh_report_owned()
        except BaseException as exc:
            self.record_failure(exc, stage="report")
        try:
            scene.close()
        except BaseException as exc:
            self.record_failure(exc, stage="close")
            raise
        finally:
            self._scene = None

    async def close(self) -> None:
        cancellation = None
        error = None
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closing"
            if self._close_future is None:
                # No native object exists and no worker has started if the
                # workflow failed before initialization.
                if self._initialize_future is None:
                    self._close_future = Future()
                    self._close_future.set_result(None)
                else:
                    self._close_future = self._pool.submit(self._close_owned)
            close_future = self._close_future
        try:
            await _await_drained(close_future)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except BaseException as exc:
            error = exc
        finally:
            with self._lock:
                if self._shutdown_future is None:
                    # Joining native ownership off the loop also keeps slow SDK
                    # disposal and thread exit from freezing WebSocket events.
                    self._shutdown_future = asyncio.get_running_loop().run_in_executor(
                        None, lambda: self._pool.shutdown(wait=True, cancel_futures=False),
                    )
                shutdown_future = self._shutdown_future
            try:
                await _await_drained(shutdown_future)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            finally:
                with self._lock:
                    self._state = "closed"
        if cancellation is not None:
            raise cancellation
        if error is not None:
            raise error
