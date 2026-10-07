"""Resolve a liquid node's design-time mapping context without executing tasks.

This is deliberately a bounded flow/deck analysis, not a workflow validator or
simulator. In particular, it does not certify tip inventory or collision safety.
The caller must apply catalog geometry and native reachability checks afterwards.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

_LIQUID = {"liquid/Aspirate", "liquid/Dispense", "liquid/Mix"}
_PASSIVE = _LIQUID | {
    "flow/Start", "system/Wait", "system/Initialize", "system/Home",
    "system/DockGripper", "sensor/ReadBarcode",
}
_MOVES = {
    "plate/PickPlace": ("pick_location", "place_location"),
    "plate/Stack": ("source_location", "base_location"),
    "plate/Destack": ("source_location", "destination_location"),
}
_EMPTY_CONTEXT = {
    "deck": {}, "location": None, "head_mode": None,
    "tip_node_id": None, "tip_location": None,
    "tip_definition_id": None, "tip_labware": None,
}


class _Unresolved(ValueError):
    pass


def _location(value: Any) -> int:
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if type(value) is not int or not 1 <= value <= 9:
        raise _Unresolved("Mapping needs a fixed deck position from 1 to 9; iteration or variable locations are unresolved.")
    return value


def _mode(value: Any) -> dict | None:
    # Match the executor: only a nonempty dictionary applies an override.
    if not isinstance(value, dict) or not value:
        return None
    return deepcopy(value)


@dataclass
class _State:
    deck: dict[str, list[dict]]
    configured_mode: dict | None = None
    mounted: dict | None = None


class _Resolver:
    def __init__(self, workflow: dict, node_id: Any):
        if workflow.get("library"):
            raise _Unresolved("Workflow library code can change runtime context; mapping cannot be determined without running it.")
        graph = workflow.get("graph") or {}
        raw_nodes = graph.get("nodes") or []
        self.nodes = {str(node["id"]): node for node in raw_nodes}
        if len(self.nodes) != len(raw_nodes):
            raise _Unresolved("Duplicate task IDs make the flow context ambiguous.")
        self.target = str(node_id)
        target = self.nodes.get(self.target)
        if not target or target.get("type") not in _LIQUID:
            raise _Unresolved("Select an Aspirate, Dispense, or Mix task to choose its plate wells.")
        self.location = _location((target.get("properties") or {}).get("location"))
        starts = [key for key, node in self.nodes.items() if node.get("type") == "flow/Start"]
        if len(starts) != 1:
            raise _Unresolved("Mapping needs one connected Start task.")
        self.start = starts[0]
        self.outputs: dict[tuple[str, int], list[str]] = {}
        for link in graph.get("links") or []:
            if isinstance(link, (list, tuple)):
                if len(link) < 5:
                    raise _Unresolved("A malformed flow link prevents mapping this task.")
                origin, slot, target = link[1:4]
                link_type = link[5] if len(link) > 5 else -1
            else:
                origin, slot, target = link["origin_id"], link["origin_slot"], link["target_id"]
                link_type = link.get("link_type", link.get("type", -1))
            if link_type != -1:
                continue
            if str(origin) not in self.nodes or str(target) not in self.nodes:
                raise _Unresolved("A flow link refers to a missing task.")
            self.outputs.setdefault((str(origin), int(slot)), []).append(str(target))
        deck = {}
        for slot, stack in (workflow.get("deck") or {}).items():
            key = str(_location(slot))
            if isinstance(stack, dict):
                stack = [stack]
            if not isinstance(stack, list) or any(not isinstance(item, dict) for item in stack):
                raise _Unresolved(f"Deck position {key} does not contain a defined stack.")
            if stack:
                deck[key] = deepcopy(stack)
        self.initial = _State(deck)
        self.contexts: list[dict] = []
        self.steps = 0

    def follow(self, node_id: str, slot: int, state: _State, active: frozenset, loop_depth: int) -> list[_State]:
        targets = self.outputs.get((node_id, slot), [])
        if len(targets) > 1:
            raise _Unresolved("A flow output has multiple connections; choose an unambiguous task sequence.")
        if not targets:
            return [state]
        return self.walk(targets[0], state, active, loop_depth)

    def walk(self, node_id: str, state: _State, active: frozenset, loop_depth: int = 0) -> list[_State]:
        self.steps += 1
        if self.steps > 4096:
            raise _Unresolved("This flow exceeds the bounded mapping analysis; simplify its branches or loops.")
        if len(active) > 128:
            raise _Unresolved("This connected path exceeds the bounded mapping analysis.")
        if node_id in active:
            raise _Unresolved("A cyclic flow prevents determining a single plate mapping context.")
        active = active | {node_id}
        node = self.nodes[node_id]
        kind = node.get("type")
        props = node.get("properties") or {}
        if node_id == self.target:
            if state.mounted is None:
                raise _Unresolved("No preceding Tips On task supplies mounted tips at this liquid task. Add or connect its tip pickup first.")
            self.contexts.append({"deck": deepcopy(state.deck), "location": self.location, **deepcopy(state.mounted)})
            # Outside a loop, later tasks cannot affect the selected task.
            if not loop_depth:
                return [state]
        if kind == "flow/End":
            return [state]
        if kind == "flow/IfElse":
            return self.follow(node_id, 0, deepcopy(state), active, loop_depth) + self.follow(node_id, 1, deepcopy(state), active, loop_depth)
        if kind == "flow/Loop":
            count = props.get("count", 1)
            if isinstance(count, str) and count.strip().isdecimal():
                count = int(count.strip())
            if type(count) is not int or not 0 <= count <= 32 or loop_depth:
                raise _Unresolved("Mapping cannot resolve dynamic, nested, or more than 32 loop iterations.")
            states = [state]
            for _ in range(count):
                next_states = []
                for current in states:
                    next_states.extend(self.follow(node_id, 0, current, active, loop_depth + 1))
                states = self.unique_states(next_states)
            result = []
            for current in states:
                result.extend(self.follow(node_id, 1, current, active, loop_depth))
            return result
        for key, value in props.items():
            if key == "location" or key.endswith("_location"):
                _location(value)
        state = deepcopy(state)
        if kind == "tips/TipsOn":
            if state.mounted is not None:
                raise _Unresolved("A Tips On task follows an existing pickup without Tips Off; mounted-tip context is invalid.")
            location = _location(props.get("location"))
            stack = state.deck.get(str(location), [])
            if not stack:
                raise _Unresolved(f"Tips On task {node['id']} has no loaded rack at deck position {location}.")
            override = _mode(props.get("head_mode"))
            if override is not None:
                state.configured_mode = override
            state.mounted = {
                "head_mode": deepcopy(state.configured_mode), "tip_node_id": node["id"],
                "tip_location": location, "tip_labware": deepcopy(stack[-1]),
                "tip_definition_id": stack[-1].get("tip_definition_id") or None,
            }
        elif kind == "tips/TipsOff":
            # The mounted footprint is unchanged by legacy TipsOff overrides,
            # but the executor retains that configured mode for a later pickup.
            override = _mode(props.get("head_mode"))
            if override is not None:
                state.configured_mode = override
            state.mounted = None
        elif kind in _MOVES:
            if loop_depth:
                raise _Unresolved("Plate moves inside a loop can change the mapping between iterations; inspect that loop in simulation.")
            source_key, destination_key = _MOVES[kind]
            source, destination = str(_location(props.get(source_key))), str(_location(props.get(destination_key)))
            stack = state.deck.get(source, [])
            if source == destination or not stack:
                raise _Unresolved(f"Task {node['id']} cannot resolve a plate move from position {source} to {destination}.")
            if stack[-1].get("is_mounted"):
                raise _Unresolved("A mounted plate pair needs runtime handling before its resulting deck context is known.")
            if kind == "plate/Destack" and state.deck.get(destination):
                raise _Unresolved(f"Destack task {node['id']} requires an empty destination at position {destination}.")
            state.deck.setdefault(destination, []).append(stack.pop())
            if not stack:
                state.deck.pop(source, None)
        elif kind not in _PASSIVE:
            raise _Unresolved(f"Task {node.get('title') or node['id']} ({kind}) can change runtime context; mapping is unresolved before this liquid task.")
        return self.follow(node_id, 0, state, active, loop_depth)

    @staticmethod
    def unique_states(states: list[_State]) -> list[_State]:
        unique = []
        for state in states:
            if state not in unique:
                unique.append(state)
        if len(unique) > 64:
            raise _Unresolved("Too many possible branch contexts; mapping needs a single known deck and mounted head.")
        return unique

    def resolve(self) -> dict:
        self.walk(self.start, self.initial, frozenset())
        if not self.contexts:
            raise _Unresolved("This liquid task is not reachable from Start on an executed flow path.")
        first = self.contexts[0]
        if any(context != first for context in self.contexts[1:]):
            raise _Unresolved("Branches or loop iterations reach this liquid task with different deck or mounted-tip contexts. Choose wells after resolving those differences.")
        return {"status": "resolved", "message": "Plate mapping context follows the connected task sequence; native geometry checks are still required.", **first}


def resolve_plate_context(workflow: dict, node_id: Any) -> dict:
    """Return one proven deck/mounted-tip context, or an explicit unresolved result.

    ``head_mode=None`` in a resolved result means the pickup inherits the
    initial configured mode. ``tip_labware`` records the rack at pickup time,
    even if a later Pick/Place moves that rack. Inputs are never modified.
    """
    try:
        return _Resolver(workflow, node_id).resolve()
    except (_Unresolved, KeyError, TypeError, ValueError, AttributeError) as exc:
        return {"status": "unresolved", "message": str(exc) or "The saved flow context could not be resolved.", **deepcopy(_EMPTY_CONTEXT)}
