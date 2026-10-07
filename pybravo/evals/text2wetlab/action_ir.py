"""A small, non-authoring compiler for model-supplied OT-2 actions.

This is an experimental boundary, not a replacement for the evidence plan or
scientific review. The model must supply every ordered action. Trusted catalog
facts, provided separately, determine which labware, wells, and pipette strokes
are physically admissible. No reagent choice, volume, or step is inferred here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class LabwareLoad(_Strict):
    id: str = Field(min_length=1)
    load_name: str = Field(min_length=1)
    slot: int | None = Field(default=None, ge=1, le=11)
    module_id: str | None = None


class ModuleLoad(_Strict):
    id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    slot: int | None = Field(default=None, ge=1, le=11)


class PipetteLoad(_Strict):
    id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    mount: Literal["left", "right"]
    tip_rack_ids: list[str] = Field(min_length=1)


class Pickup(_Strict):
    kind: Literal["pickup"]
    pipette: str


class Drop(_Strict):
    kind: Literal["drop"]
    pipette: str


class Stroke(_Strict):
    kind: Literal["aspirate", "dispense"]
    pipette: str
    labware: str
    well: str
    volume_ul: float = Field(gt=0)


class Mix(_Strict):
    kind: Literal["mix"]
    pipette: str
    labware: str
    well: str
    cycles: int = Field(gt=0)
    volume_ul: float = Field(gt=0)


class Delay(_Strict):
    kind: Literal["delay"]
    seconds: float = Field(gt=0)


class Pause(_Strict):
    kind: Literal["pause"]
    message: str = Field(min_length=1)


class RefillTips(_Strict):
    kind: Literal["refill_tips"]
    pipette: str
    message: str = Field(min_length=1)


class SetTemperature(_Strict):
    kind: Literal["set_temperature"]
    module: str
    celsius: float


class SetBlockTemperature(_Strict):
    kind: Literal["set_block_temperature"]
    module: str
    celsius: float
    hold_seconds: float | None = Field(default=None, gt=0)


class SetLidTemperature(_Strict):
    kind: Literal["set_lid_temperature"]
    module: str
    celsius: float


class LidState(_Strict):
    kind: Literal["open_lid", "close_lid"]
    module: str


class MagnetEngage(_Strict):
    kind: Literal["magnet_engage"]
    module: str
    height_from_base_mm: float = Field(ge=0)


class MagnetDisengage(_Strict):
    kind: Literal["magnet_disengage"]
    module: str


PrimitiveAction = Annotated[
    Pickup | Drop | Stroke | Mix | Delay | Pause | RefillTips |
    SetTemperature | SetBlockTemperature | SetLidTemperature | LidState |
    MagnetEngage | MagnetDisengage,
    Field(discriminator="kind"),
]


class ForEach(_Strict):
    """Repeat explicit actions over model-supplied well-name bindings.

    A body may use a whole-well placeholder such as ``$source_well``. The
    compiler does not derive any source/destination pairing or volume.
    """

    kind: Literal["for_each"]
    bindings: list[dict[str, str]] = Field(min_length=1, max_length=384)
    actions: list[PrimitiveAction] = Field(min_length=1, max_length=32)


Action = Annotated[
    Pickup | Drop | Stroke | Mix | Delay | Pause | RefillTips |
    SetTemperature | SetBlockTemperature | SetLidTemperature | LidState |
    MagnetEngage | MagnetDisengage | ForEach,
    Field(discriminator="kind"),
]


class ActionPlan(_Strict):
    """All experimental content is supplied by the caller, never the compiler."""

    labware: list[LabwareLoad]
    modules: list[ModuleLoad] = Field(default_factory=list)
    pipettes: list[PipetteLoad]
    actions: list[Action]


@dataclass(frozen=True)
class LabwareFacts:
    """Trusted catalog details; these values must not come from model output."""

    wells: frozenset[str]
    is_tiprack: bool = False
    tip_capacity_ul: float | None = None
    multichannel_compatible: bool = False
    # Trusted anchors for an entire multichannel pickup. A boolean alone does
    # not establish that B1 or an irregular column is a valid 8-channel anchor.
    multichannel_anchor_wells: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PipetteFacts:
    min_volume_ul: float
    max_volume_ul: float
    channels: int


@dataclass(frozen=True)
class ModuleFacts:
    """Trusted OT-2 module footprint and operating limits for one load name."""

    kind: Literal["temperature", "thermocycler", "magnetic"]
    fixed_occupied_slots: frozenset[int] = frozenset()
    allowed_slots: frozenset[int] = frozenset()
    occupied_slots_by_slot: Mapping[int, frozenset[int]] = field(default_factory=dict)
    compatible_labware_load_names: frozenset[str] = frozenset()
    temperature_range_c: tuple[float, float] | None = None
    lid_temperature_range_c: tuple[float, float] | None = None
    magnet_height_range_mm: tuple[float, float] | None = None


class ActionPlanError(ValueError):
    """The proposed action sequence violates a known structural constraint."""


_MAX_EXPANDED_ACTIONS = 10_000
_BINDING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WELL_PLACEHOLDER = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\Z")


def _expand_actions(plan: ActionPlan) -> tuple[list[PrimitiveAction], list[str]]:
    """Expand finite, flat bindings and retain paths for actionable errors."""
    expanded: list[PrimitiveAction] = []
    paths: list[str] = []
    for index, action in enumerate(plan.actions):
        path = f"actions[{index}]"
        if not isinstance(action, ForEach):
            expanded.append(action)
            paths.append(path)
            continue
        names = set(action.bindings[0])
        if not names or any(_BINDING_NAME.fullmatch(name) is None for name in names):
            raise ActionPlanError(f"{path} has invalid or absent binding names.")
        used: set[str] = set()
        for body_index, item in enumerate(action.actions):
            if isinstance(item, (Stroke, Mix)) and item.well.startswith("$"):
                matched = _WELL_PLACEHOLDER.fullmatch(item.well)
                if matched is None or matched.group(1) not in names:
                    raise ActionPlanError(
                        f"{path}.actions[{body_index}] uses an unknown well binding {item.well!r}."
                    )
                used.add(matched.group(1))
        if used != names:
            raise ActionPlanError(f"{path} has unused well bindings: {sorted(names - used)}.")
        if len(expanded) + len(action.bindings) * len(action.actions) > _MAX_EXPANDED_ACTIONS:
            raise ActionPlanError(f"{path} exceeds the {_MAX_EXPANDED_ACTIONS} action expansion limit.")
        for binding_index, binding in enumerate(action.bindings):
            if set(binding) != names or any(not value for value in binding.values()):
                raise ActionPlanError(
                    f"{path}.bindings[{binding_index}] must supply the same nonempty well names."
                )
            for body_index, item in enumerate(action.actions):
                if isinstance(item, (Stroke, Mix)) and item.well.startswith("$"):
                    symbol = _WELL_PLACEHOLDER.fullmatch(item.well)
                    assert symbol is not None
                    item = item.model_copy(update={"well": binding[symbol.group(1)]})
                expanded.append(item)
                paths.append(f"{path}.bindings[{binding_index}].actions[{body_index}]")
    if len(expanded) > _MAX_EXPANDED_ACTIONS:
        raise ActionPlanError(f"Plan exceeds the {_MAX_EXPANDED_ACTIONS} action expansion limit.")
    return expanded, paths


def _valid_range(value: tuple[float, float] | None) -> bool:
    return (value is None or
            (len(value) == 2 and all(math.isfinite(x) for x in value) and
             value[0] <= value[1]))


def _facts_are_valid(labware: Mapping[str, LabwareFacts],
                     pipettes: Mapping[str, PipetteFacts],
                     modules: Mapping[str, ModuleFacts]) -> None:
    for name, facts in labware.items():
        if not facts.wells:
            raise ValueError(f"Trusted labware {name!r} has no wells.")
        if not facts.multichannel_anchor_wells <= facts.wells:
            raise ValueError(f"Trusted labware {name!r} has an anchor outside its well set.")
        if facts.multichannel_anchor_wells and not facts.multichannel_compatible:
            raise ValueError(f"Trusted labware {name!r} marks anchors as incompatible.")
        if facts.is_tiprack and (facts.tip_capacity_ul is None or
                                 not math.isfinite(facts.tip_capacity_ul) or
                                 facts.tip_capacity_ul <= 0):
            raise ValueError(f"Trusted tip rack {name!r} needs a positive capacity.")
    for name, facts in pipettes.items():
        if (not math.isfinite(facts.min_volume_ul) or
                not math.isfinite(facts.max_volume_ul) or
                not 0 < facts.min_volume_ul <= facts.max_volume_ul or
                facts.channels < 1):
            raise ValueError(f"Trusted pipette {name!r} has invalid working limits.")
    for name, facts in modules.items():
        if bool(facts.fixed_occupied_slots) == bool(facts.allowed_slots):
            raise ValueError(f"Trusted module {name!r} needs one placement mode.")
        if (any(not 1 <= slot <= 11 for slot in facts.fixed_occupied_slots | facts.allowed_slots)
                or set(facts.occupied_slots_by_slot) - facts.allowed_slots):
            raise ValueError(f"Trusted module {name!r} has invalid deck locations.")
        for location, occupied in facts.occupied_slots_by_slot.items():
            if location not in occupied or any(not 1 <= slot <= 11 for slot in occupied):
                raise ValueError(f"Trusted module {name!r} has an invalid footprint.")
        if not all(_valid_range(value) for value in (
            facts.temperature_range_c,
            facts.lid_temperature_range_c,
            facts.magnet_height_range_mm,
        )):
            raise ValueError(f"Trusted module {name!r} has invalid operating limits.")


def compile_actions(
    raw: ActionPlan | dict,
    *,
    labware_catalog: Mapping[str, LabwareFacts],
    pipette_catalog: Mapping[str, PipetteFacts],
    module_catalog: Mapping[str, ModuleFacts] | None = None,
    refill_authorized: bool = False,
) -> str:
    """Validate and lower explicit actions to fixed Opentrons API primitives.

    This slice supports direct liquid primitives, finite mapping expansion,
    trusted module placements and state settings, and authorized fresh-tip
    refill handoffs. It deliberately has no deck moves or scientific source
    interpretation. The output must still pass simulator, event, and science
    gates before use.
    """
    try:
        plan = raw if isinstance(raw, ActionPlan) else ActionPlan.model_validate(raw)
    except ValidationError as exc:
        raise ActionPlanError(str(exc)) from exc
    if not isinstance(refill_authorized, bool):
        raise ValueError("refill_authorized must be a trusted boolean.")
    trusted_modules = module_catalog or {}
    _facts_are_valid(labware_catalog, pipette_catalog, trusted_modules)
    expanded_actions, action_paths = _expand_actions(plan)

    module_loads: dict[str, ModuleLoad] = {}
    occupied_slots: set[int] = set()
    for item in plan.modules:
        if item.id in module_loads:
            raise ActionPlanError(f"Duplicate module ID {item.id!r}.")
        facts = trusted_modules.get(item.model)
        if facts is None:
            raise ActionPlanError(f"Module {item.model!r} is absent from the trusted catalog.")
        if facts.fixed_occupied_slots:
            if item.slot is not None:
                raise ActionPlanError(f"Fixed module {item.id!r} must not specify a slot.")
            footprint = facts.fixed_occupied_slots
        else:
            if item.slot not in facts.allowed_slots:
                raise ActionPlanError(f"Module {item.id!r} cannot load at slot {item.slot!r}.")
            assert item.slot is not None
            footprint = facts.occupied_slots_by_slot.get(item.slot, frozenset({item.slot}))
        overlap = occupied_slots & footprint
        if overlap:
            raise ActionPlanError(f"Module {item.id!r} overlaps occupied slot {min(overlap)}.")
        occupied_slots.update(footprint)
        module_loads[item.id] = item

    loads: dict[str, LabwareLoad] = {}
    module_labware: set[str] = set()
    for item in plan.labware:
        if item.id in loads or item.id in module_loads:
            raise ActionPlanError(f"Duplicate labware ID {item.id!r}.")
        if item.load_name not in labware_catalog:
            raise ActionPlanError(f"Labware {item.load_name!r} is absent from the trusted catalog.")
        if (item.slot is None) == (item.module_id is None):
            raise ActionPlanError(
                f"Labware {item.id!r} needs exactly one deck slot or module ID."
            )
        if item.module_id is not None:
            if item.module_id not in module_loads:
                raise ActionPlanError(f"Labware {item.id!r} names an unloaded module.")
            if item.module_id in module_labware:
                raise ActionPlanError(f"Module {item.module_id!r} already holds labware.")
            if labware_catalog[item.load_name].is_tiprack:
                raise ActionPlanError("A tip rack cannot be loaded onto a module in this IR.")
            module_facts = trusted_modules[module_loads[item.module_id].model]
            if item.load_name not in module_facts.compatible_labware_load_names:
                raise ActionPlanError(
                    f"Labware {item.load_name!r} has no trusted compatibility with "
                    f"module {item.module_id!r}."
                )
            module_labware.add(item.module_id)
        else:
            assert item.slot is not None
            if item.slot in occupied_slots:
                raise ActionPlanError(f"Deck slot {item.slot} is assigned more than once.")
            occupied_slots.add(item.slot)
        loads[item.id] = item

    instruments: dict[str, PipetteLoad] = {}
    mounts: set[str] = set()
    assigned_racks: set[str] = set()
    effective_ranges: dict[str, tuple[float, float]] = {}
    remaining_pickups: dict[str, int] = {}
    pickups_per_load: dict[str, int] = {}
    for item in plan.pipettes:
        if item.id in instruments:
            raise ActionPlanError(f"Duplicate pipette ID {item.id!r}.")
        if item.mount in mounts:
            raise ActionPlanError(f"Mount {item.mount!r} is assigned more than once.")
        specs = pipette_catalog.get(item.model)
        if specs is None:
            raise ActionPlanError(f"Pipette {item.model!r} is absent from the trusted catalog.")
        capacities: list[float] = []
        pickups = 0
        if len(item.tip_rack_ids) != len(set(item.tip_rack_ids)):
            raise ActionPlanError(f"Pipette {item.id!r} lists the same rack twice.")
        for rack_id in item.tip_rack_ids:
            if rack_id in assigned_racks:
                raise ActionPlanError(f"Tip rack {rack_id!r} is assigned to multiple pipettes.")
            rack = loads.get(rack_id)
            if rack is None:
                raise ActionPlanError(f"Pipette {item.id!r} refers to unloaded rack {rack_id!r}.")
            facts = labware_catalog[rack.load_name]
            if not facts.is_tiprack or facts.tip_capacity_ul is None:
                raise ActionPlanError(f"{rack_id!r} is not a trusted tip rack.")
            if specs.channels > 1 and (not facts.multichannel_compatible or
                                        not facts.multichannel_anchor_wells):
                raise ActionPlanError(f"Tip rack {rack_id!r} is not multichannel compatible.")
            capacities.append(facts.tip_capacity_ul)
            pickups += (len(facts.multichannel_anchor_wells) if specs.channels > 1
                        else len(facts.wells))
            assigned_racks.add(rack_id)
        upper = min(specs.max_volume_ul, *capacities)
        if upper < specs.min_volume_ul:
            raise ActionPlanError(f"Pipette {item.id!r} has no working range with its tips.")
        instruments[item.id] = item
        mounts.add(item.mount)
        effective_ranges[item.id] = (specs.min_volume_ul, upper)
        remaining_pickups[item.id] = pickups
        pickups_per_load[item.id] = pickups

    # Validate state before emitting code. A bad draft can never be partially
    # compiled into a runnable file.
    attached = {name: False for name in instruments}
    held = {name: 0.0 for name in instruments}
    lid_state = {name: "unknown" for name, item in module_loads.items()
                 if trusted_modules[item.model].kind == "thermocycler"}
    for action, path in zip(expanded_actions, action_paths, strict=True):
        if isinstance(action, (Delay, Pause)):
            continue
        if isinstance(action, (SetTemperature, SetBlockTemperature,
                               SetLidTemperature, LidState, MagnetEngage,
                               MagnetDisengage)):
            module = module_loads.get(action.module)
            if module is None:
                raise ActionPlanError(f"{path} refers to unknown module {action.module!r}.")
            module_facts = trusted_modules[module.model]
            if isinstance(action, SetTemperature):
                required_kind = "temperature"
                allowed_range = module_facts.temperature_range_c
                value = action.celsius
            elif isinstance(action, SetBlockTemperature):
                required_kind = "thermocycler"
                allowed_range = module_facts.temperature_range_c
                value = action.celsius
            elif isinstance(action, SetLidTemperature):
                required_kind = "thermocycler"
                allowed_range = module_facts.lid_temperature_range_c
                value = action.celsius
            elif isinstance(action, LidState):
                required_kind = "thermocycler"
                allowed_range = None
                value = None
            else:
                required_kind = "magnetic"
                allowed_range = (module_facts.magnet_height_range_mm
                                 if isinstance(action, MagnetEngage) else None)
                value = (action.height_from_base_mm
                         if isinstance(action, MagnetEngage) else None)
            if module_facts.kind != required_kind:
                raise ActionPlanError(f"{path} is not supported by module {action.module!r}.")
            if value is not None and (
                allowed_range is None or not allowed_range[0] <= value <= allowed_range[1]
            ):
                raise ActionPlanError(f"{path} exceeds trusted limits for module {action.module!r}.")
            if isinstance(action, LidState):
                lid_state[action.module] = "open" if action.kind == "open_lid" else "closed"
            continue
        if action.pipette not in instruments:
            raise ActionPlanError(f"{path} refers to unknown pipette {action.pipette!r}.")
        name = action.pipette
        if isinstance(action, RefillTips):
            if not refill_authorized:
                raise ActionPlanError(f"{path} has no task-authorized fresh-tip refill.")
            if attached[name]:
                raise ActionPlanError(f"{path} cannot refill while {name!r} has a tip attached.")
            if remaining_pickups[name] != 0:
                raise ActionPlanError(f"{path} resets {name!r} before its tip racks are exhausted.")
            remaining_pickups[name] = pickups_per_load[name]
            continue
        if isinstance(action, Pickup):
            if attached[name]:
                raise ActionPlanError(f"{path} picks up while {name!r} already has a tip.")
            if remaining_pickups[name] <= 0:
                raise ActionPlanError(f"{path} exceeds the loaded fresh-tip supply for {name!r}.")
            remaining_pickups[name] -= 1
            attached[name] = True
            held[name] = 0.0
            continue
        if not attached[name]:
            raise ActionPlanError(f"{path} uses {name!r} without an attached tip.")
        if isinstance(action, Drop):
            if held[name] > 1e-9:
                raise ActionPlanError(f"{path} discards {name!r} with liquid still in its tip.")
            attached[name] = False
            continue
        item = loads.get(action.labware)
        if item is None:
            raise ActionPlanError(f"{path} refers to unloaded labware {action.labware!r}.")
        if item.module_id in lid_state and lid_state[item.module_id] != "open":
            raise ActionPlanError(f"{path} pipettes into a thermocycler without an open lid.")
        facts = labware_catalog[item.load_name]
        if facts.is_tiprack or action.well not in facts.wells:
            raise ActionPlanError(f"{path} names an invalid liquid well {action.well!r}.")
        if pipette_catalog[instruments[name].model].channels > 1 and (
            not facts.multichannel_compatible or
            action.well not in facts.multichannel_anchor_wells
        ):
            raise ActionPlanError(f"{path} uses an invalid multichannel column anchor.")
        minimum, maximum = effective_ranges[name]
        if not minimum <= action.volume_ul <= maximum:
            raise ActionPlanError(
                f"{path} requests {action.volume_ul:g} µL outside {name!r}'s "
                f"{minimum:g}–{maximum:g} µL range with loaded tips."
            )
        if isinstance(action, Stroke):
            if action.kind == "aspirate":
                if held[name] + action.volume_ul > maximum + 1e-9:
                    raise ActionPlanError(f"{path} would exceed {name!r}'s tip capacity.")
                held[name] += action.volume_ul
            else:
                if action.volume_ul > held[name] + 1e-9:
                    raise ActionPlanError(f"{path} dispenses more than {name!r} holds.")
                held[name] -= action.volume_ul
    if any(attached.values()):
        raise ActionPlanError("Every picked-up tip must be discarded before the protocol ends.")

    var_by_labware = {item.id: f"lw_{index}" for index, item in enumerate(plan.labware)}
    var_by_module = {item.id: f"mod_{index}" for index, item in enumerate(plan.modules)}
    var_by_pipette = {item.id: f"pip_{index}" for index, item in enumerate(plan.pipettes)}
    lines = [
        "from opentrons import protocol_api",
        "",
        'metadata = {"apiLevel": "2.15"}',
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
    ]
    for item in plan.modules:
        location = "" if item.slot is None else f", {item.slot!r}"
        lines.append(f"    {var_by_module[item.id]} = protocol.load_module("
                     f"{item.model!r}{location})")
    for item in plan.labware:
        if item.module_id is not None:
            lines.append(f"    {var_by_labware[item.id]} = "
                         f"{var_by_module[item.module_id]}.load_labware({item.load_name!r})")
        else:
            lines.append(f"    {var_by_labware[item.id]} = protocol.load_labware("
                         f"{item.load_name!r}, {item.slot!r})")
    for item in plan.pipettes:
        racks = ", ".join(var_by_labware[rack] for rack in item.tip_rack_ids)
        lines.append(f"    {var_by_pipette[item.id]} = protocol.load_instrument("
                     f"{item.model!r}, {item.mount!r}, tip_racks=[{racks}])")
    for action in expanded_actions:
        if isinstance(action, Delay):
            lines.append(f"    protocol.delay(seconds={action.seconds!r})")
        elif isinstance(action, Pause):
            lines.append(f"    protocol.pause({action.message!r})")
        elif isinstance(action, (SetTemperature, SetBlockTemperature,
                                 SetLidTemperature, LidState, MagnetEngage,
                                 MagnetDisengage)):
            module = var_by_module[action.module]
            if isinstance(action, SetTemperature):
                lines.append(f"    {module}.set_temperature({action.celsius!r})")
            elif isinstance(action, SetBlockTemperature):
                hold = ("" if action.hold_seconds is None else
                        f", hold_time_seconds={action.hold_seconds!r}")
                lines.append(f"    {module}.set_block_temperature({action.celsius!r}{hold})")
            elif isinstance(action, SetLidTemperature):
                lines.append(f"    {module}.set_lid_temperature({action.celsius!r})")
            elif isinstance(action, LidState):
                lines.append(f"    {module}.{action.kind}()")
            elif isinstance(action, MagnetEngage):
                lines.append(f"    {module}.engage("
                             f"height_from_base={action.height_from_base_mm!r})")
            else:
                lines.append(f"    {module}.disengage()")
        else:
            pip = var_by_pipette[action.pipette]
            if isinstance(action, RefillTips):
                lines.append(f"    protocol.pause({action.message!r})")
                lines.append(f"    {pip}.reset_tipracks()")
            elif isinstance(action, Pickup):
                lines.append(f"    {pip}.pick_up_tip()")
            elif isinstance(action, Drop):
                lines.append(f"    {pip}.drop_tip()")
            elif isinstance(action, (Stroke, Mix)):
                well = f"{var_by_labware[action.labware]}.wells_by_name()[{action.well!r}]"
                if isinstance(action, Mix):
                    lines.append(f"    {pip}.mix({action.cycles!r}, {action.volume_ul!r}, {well})")
                else:
                    lines.append(f"    {pip}.{action.kind}({action.volume_ul!r}, {well})")
    if not expanded_actions and not plan.labware and not plan.modules and not plan.pipettes:
        lines.append("    pass")
    return "\n".join(lines) + "\n"
