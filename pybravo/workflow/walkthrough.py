"""Nonexecuting visual walkthrough of a saved, model-generated Designer graph.

This is deliberately separate from ``WorkflowExecutor``.  It never creates a
Bravo, calls a controller/task, resolves a liquid class, handles a tip, or
changes deck contents.  Its position events show only retracted head hovers
over taught deck locations; they are not a motion or collision simulation.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from pybravo.profile.profile import BravoProfile
from pybravo.types import AXIS_RANGES, Axis
from pybravo.workflow.storage import assert_safe_generated_draft

_LOCATION_PROPERTIES: dict[str, tuple[str, ...]] = {
    "liquid/Aspirate": ("location",),
    "liquid/Dispense": ("location",),
    "liquid/Mix": ("location",),
    "tips/TipsOn": ("location",),
    "tips/TipsOff": ("location",),
    "plate/PickPlace": ("pick_location", "place_location"),
    "plate/Stack": ("source_location", "base_location"),
    "plate/Destack": ("source_location", "destination_location"),
    "plate/Mount": ("source_location", "destination_location"),
    "plate/Unmount": ("source_location", "destination_location"),
    "plate/Delid": ("location",),
    "plate/Relid": ("location",),
}
_SAFE_POSE = {"Z": 0.0, "Zg": -20.0, "G": 0.0, "W": 0.0}
_MAX_LOOP_COUNT = 1000


def _node_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Designer node and link IDs must be positive integers.")
    return value


def _slot(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Designer flow slots must be nonnegative integers.")
    return value


def _link_fields(raw: Any) -> tuple[int, int, int, int, int, int]:
    if isinstance(raw, (list, tuple)) and len(raw) == 6:
        values = raw
    elif isinstance(raw, dict):
        values = tuple(raw.get(key) for key in (
            "id", "origin_id", "origin_slot", "target_id", "target_slot", "link_type",
        ))
    else:
        raise ValueError("A Designer link must be a six-element tuple or link object.")
    identity, origin, origin_slot, target, target_slot, link_type = values
    if isinstance(link_type, bool) or link_type != -1:
        raise ValueError("Visual walkthrough accepts flow links only.")
    return (_node_id(identity), _node_id(origin), _slot(origin_slot),
            _node_id(target), _slot(target_slot), -1)


class WorkflowWalkthrough:
    """Emit a labeled Designer event stream without performing any operation.

    ``workflow`` is a saved generated-draft record.  Both its graph/deck and
    the profile are deep copied.  A malformed or ambiguous graph raises before
    any event is emitted.  ``abort()`` matches the synchronous Designer stop
    route; ``await stop()`` is also available to direct callers.
    """

    def __init__(
        self,
        workflow: dict[str, Any],
        profile: BravoProfile,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        *,
        step_delay_s: float = 0.04,
        max_visits: int = 5000,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> None:
        if not isinstance(workflow, dict) or workflow.get("protocol_generated_draft") is not True:
            raise ValueError("Visual walkthrough requires a saved generated draft.")
        assert_safe_generated_draft(workflow)
        if not isinstance(profile, BravoProfile):
            raise TypeError("Visual walkthrough requires a BravoProfile snapshot.")
        if not isinstance(step_delay_s, (int, float)) or isinstance(step_delay_s, bool) or (
            not math.isfinite(step_delay_s) or not 0 <= step_delay_s <= 1
        ):
            raise ValueError("Walkthrough step delay must be between 0 and 1 second.")
        if isinstance(max_visits, bool) or not isinstance(max_visits, int) or not 1 <= max_visits <= 5000:
            raise ValueError("Walkthrough visit cap must be an integer from 1 to 5000.")
        self._workflow = copy.deepcopy(workflow)
        self._graph = self._workflow["graph"]
        self._deck = copy.deepcopy(self._workflow.get("deck") or {})
        self._profile = copy.deepcopy(profile)
        self._on_event = on_event
        self._delay = float(step_delay_s)
        self._max_visits = max_visits
        self._diagnostics = copy.deepcopy(diagnostics or [])
        self._aborted = False
        self._running = False
        self._route = self._compile_route()

    def abort(self) -> None:
        """Request that event playback stop at the next async boundary."""
        self._aborted = True

    async def stop(self) -> None:
        self.abort()

    async def _emit(self, event: dict[str, Any]) -> None:
        payload = {
            "simulation_kind": "visual_walkthrough",
            "qualification_granted": False,
            "validation_passed": False,
            "time": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        if self._on_event is not None:
            result = self._on_event(payload)
            if inspect.isawaitable(result):
                await result

    def _compile_route(self) -> list[tuple[str, Any]]:
        nodes_raw = self._graph["nodes"]
        nodes: dict[int, dict[str, Any]] = {}
        for raw in nodes_raw:
            identity = _node_id(raw.get("id"))
            if identity in nodes:
                raise ValueError("Designer graph repeats a node ID.")
            nodes[identity] = raw
        starts = [node for node in nodes.values() if node["type"] == "flow/Start"]
        ends = [node for node in nodes.values() if node["type"] == "flow/End"]
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError("Walkthrough needs exactly one Start and one End.")
        if any(node["type"] == "flow/IfElse" for node in nodes.values()):
            raise ValueError("Conditional branches need evaluated runtime data; visual walkthrough cannot choose a branch.")

        edges: dict[tuple[int, int], int] = {}
        incoming: dict[int, int] = defaultdict(int)
        link_ids: set[int] = set()
        for raw in self._graph["links"]:
            identity, origin, origin_slot, target, target_slot, _ = _link_fields(raw)
            if identity in link_ids:
                raise ValueError("Designer graph repeats a link ID.")
            link_ids.add(identity)
            if origin not in nodes or target not in nodes:
                raise ValueError("Designer graph has a dangling flow link.")
            origin_type = nodes[origin]["type"]
            target_type = nodes[target]["type"]
            allowed_slots = {0, 1} if origin_type == "flow/Loop" else (
                set() if origin_type in {"flow/End", "flow/Frame"} else {0}
            )
            if (origin_slot not in allowed_slots or target_slot != 0
                    or target_type in {"flow/Start", "flow/Frame"}):
                raise ValueError("Designer graph connects an unsupported flow slot.")
            key = (origin, origin_slot)
            if key in edges:
                raise ValueError("Ambiguous graph: more than one flow link leaves an output slot.")
            edges[key] = target
            incoming[target] += 1
            if incoming[target] > 1:
                raise ValueError("Ambiguous graph: flow paths merge at a node.")
        start_id = starts[0]["id"]
        if incoming[start_id]:
            raise ValueError("The Start node cannot have an incoming flow link.")

        # Check the *saved* graph, not just the route that a zero-count loop
        # happens to visit.  A hidden cycle or disconnected task is never
        # silently skipped.  Native loop repetition is represented by a
        # count and separate body/done outputs, not a graph back-edge.
        colors: dict[int, int] = {}
        reachable: set[int] = set()
        stack: list[tuple[int, bool]] = [(start_id, False)]
        while stack:
            identity, exiting = stack.pop()
            if exiting:
                colors[identity] = 2
                continue
            if colors.get(identity) == 1:
                raise ValueError("Designer flow graph contains a cycle.")
            if colors.get(identity) == 2:
                continue
            colors[identity] = 1
            reachable.add(identity)
            stack.append((identity, True))
            for slot in (1, 0):
                next_id = edges.get((identity, slot))
                if next_id is not None:
                    stack.append((next_id, False))
        if ends[0]["id"] not in reachable:
            raise ValueError("The End node is not reachable from Start.")
        if any(identity not in reachable for identity, node in nodes.items()
               if node["type"] != "flow/Frame"):
            raise ValueError("Designer graph contains a disconnected task.")

        for node in nodes.values():
            if node["type"] != "flow/Loop":
                continue
            count = (node.get("properties") or {}).get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _MAX_LOOP_COUNT:
                raise ValueError("Loop repetition must be a bounded static integer.")
            if (node["id"], 1) not in edges or (count > 0 and (node["id"], 0) not in edges):
                raise ValueError("Loop needs a done path and a body when repetitions are positive.")

        route: list[tuple[str, Any]] = []
        pending: list[tuple[str, Any, Any, Any]] = [("edge", start_id, 0, False)]
        visits = 0
        while pending:
            kind, identity, value, in_body = pending.pop()
            if kind == "loop_step":
                route.append((kind, (identity, value)))
                continue
            if kind == "loop_complete":
                route.append((kind, identity))
                continue
            target_id = edges.get((identity, value))
            if target_id is None:
                if in_body:
                    continue
                raise ValueError("A main flow path ends without reaching End.")
            node = nodes[target_id]
            node_type = node["type"]
            if node_type == "flow/End" and in_body:
                raise ValueError("A loop body cannot consume the workflow End node.")
            visits += 1
            if visits > self._max_visits:
                raise ValueError("Visual walkthrough exceeds its finite visit cap.")
            route.append(("node", node))
            if node_type == "flow/End":
                continue
            if node_type == "flow/Loop":
                count = (node.get("properties") or {}).get("count", 1)
                if len(pending) + 2 * count + 2 > self._max_visits * 3:
                    raise ValueError("Visual walkthrough exceeds its finite visit cap while expanding loops.")
                pending.append(("edge", target_id, 1, in_body))
                pending.append(("loop_complete", target_id, None, in_body))
                for index in reversed(range(count)):
                    pending.append(("edge", target_id, 0, True))
                    pending.append(("loop_step", target_id, (index + 1, count), in_body))
            else:
                pending.append(("edge", target_id, 0, in_body))
        return route

    @staticmethod
    def _deck_location(raw: Any) -> int | None:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw if 1 <= raw <= 9 else None
        if isinstance(raw, str) and raw.isascii() and raw.isdecimal():
            number = int(raw)
            return number if 1 <= number <= 9 else None
        return None

    def _hover(self, location: int) -> tuple[dict[str, float] | None, str | None]:
        teachpoints = self._profile.teachpoints
        if teachpoints is None:
            return None, "Profile has no taught deck positions; head remains stationary."
        try:
            x = float(teachpoints.get_teachpoint(location, Axis.X))
            y = float(teachpoints.get_teachpoint(location, Axis.Y))
        except (KeyError, TypeError, ValueError):
            return None, f"Deck position {location} has no usable X/Y teachpoint; head remains stationary."
        position = {"X": x, "Y": y, **_SAFE_POSE}
        for axis_name, value in position.items():
            axis = Axis[axis_name]
            machine_range = AXIS_RANGES[axis]
            configured = self._profile.axes.get(axis_name)
            profile_range = configured.range if configured is not None else machine_range
            if (not math.isfinite(value)
                    or not machine_range.min_pos <= value <= machine_range.max_pos
                    or not profile_range.min_pos <= value <= profile_range.max_pos):
                return None, (f"Deck position {location} has {axis_name} outside the "
                              "profile/machine range; head remains stationary.")
        return position, None

    async def execute(self) -> dict[str, Any]:
        if self._running:
            raise RuntimeError("Visual walkthrough is already running.")
        self._running = True
        visited = 0
        try:
            await self._emit({"type": "workflow:start", "status": "visual_walkthrough",
                              "warning": "Visual order and deck hover only; no task was executed."})
            for diagnostic in self._diagnostics:
                if isinstance(diagnostic, dict):
                    await self._emit({
                        "type": "workflow:task_warning",
                        "node_id": diagnostic.get("node_id"),
                        "code": str(diagnostic.get("code") or "draft_issue"),
                        "warning": str(diagnostic.get("message") or diagnostic.get("warning")
                                       or diagnostic.get("reason")
                                       or "Saved draft requires review."),
                    })
            for kind, item in self._route:
                if self._aborted:
                    break
                if kind == "loop_step":
                    node_id, (index, count) = item
                    await self._emit({"type": "workflow:node_step", "node_id": node_id,
                                      "step_index": index - 1,
                                      "step_name": f"visual iteration {index}/{count}; no tasks executed"})
                    continue
                if kind == "loop_complete":
                    await self._emit({"type": "workflow:node_complete", "node_id": item,
                                      "status": "visualized", "execution_status": "not_executed"})
                    continue
                node = item
                node_id, node_type = node["id"], node["type"]
                visited += 1
                await self._emit({"type": "workflow:node_start", "node_id": node_id,
                                  "task_name": node.get("title") or node_type,
                                  "execution_status": "not_executed"})
                if node_type == "flow/Loop":
                    continue
                properties = node.get("properties") or {}
                if node_type in _LOCATION_PROPERTIES:
                    for field in _LOCATION_PROPERTIES[node_type]:
                        location = self._deck_location(properties.get(field))
                        if location is None:
                            await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                              "code": "unresolved_location",
                                              "warning": f"{field} is missing or invalid; head remains stationary."})
                            continue
                        position, warning = self._hover(location)
                        if warning:
                            await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                              "code": "unavailable_safe_hover", "warning": warning})
                            continue
                        await self._emit({"type": "workflow:node_step", "node_id": node_id,
                                          "step_name": f"hover above deck position {location}; no contact"})
                        await self._emit({"type": "workflow:positions", "node_id": node_id,
                                          "positions": position, "deck_location": location,
                                          "pose": "retracted_hover"})
                if node_type.startswith("liquid/"):
                    if not str(properties.get("liquid_class") or "").strip():
                        await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                          "code": "liquid_method_missing",
                                          "warning": "No liquid class is selected; no liquid movement was simulated."})
                    else:
                        await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                          "code": "liquid_method_unverified",
                                          "warning": "Selected liquid class was not validated by this visual walkthrough; no liquid movement was simulated."})
                elif node_type.startswith("tips/"):
                    await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                      "code": "tips_not_executed",
                                      "warning": "Tip pickup/ejection and freshness were not simulated."})
                elif node_type.startswith("plate/"):
                    await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                      "code": "plate_not_moved",
                                      "warning": "Plate handling was not executed; deck contents were not changed."})
                elif node_type == "system/Manual":
                    await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                      "code": "manual_not_confirmed",
                                      "warning": "Manual checkpoint was not performed or confirmed."})
                elif node_type not in {"flow/End", "flow/Frame"}:
                    await self._emit({"type": "workflow:task_warning", "node_id": node_id,
                                      "code": "task_not_executed",
                                      "warning": "This task was shown in order but not executed."})
                await self._emit({"type": "workflow:node_complete", "node_id": node_id,
                                  "status": "visualized", "execution_status": "not_executed"})
                await asyncio.sleep(self._delay)
            status = "aborted" if self._aborted else "visualized"
            await self._emit({"type": "workflow:complete", "status": status,
                              "visited_nodes": visited,
                              "warning": "No task was executed; strict simulation remains required."})
            return {"status": status, "visited_nodes": visited,
                    "simulation_kind": "visual_walkthrough",
                    "qualification_granted": False, "validation_passed": False}
        finally:
            self._running = False
