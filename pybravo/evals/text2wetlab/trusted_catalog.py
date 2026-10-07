"""Read-only OT-2 catalog facts for the experimental action-plan compiler.

Only definitions shipped with the selected Opentrons simulator (or a pinned
task's custom labware directory) may contribute physical facts. An instruction
can nominate a load name, but it cannot supply its well geometry or pipette
working limits.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from .action_ir import LabwareFacts, ModuleFacts, PipetteFacts

_NAME = re.compile(r"[a-z][a-z0-9_.-]*\Z")


def catalog_names(source: str) -> tuple[str, ...]:
    """Find possible Opentrons load names without treating them as facts."""
    return tuple(sorted({value for value in re.findall(r"`([^`\n]+)`", source)
                         if _NAME.fullmatch(value)}))


def _column_anchor(column: list[str], wells: dict[str, Any]) -> str | None:
    """A full 8-channel anchor needs eight real, 9 mm-spaced wells."""
    if len(column) != 8 or any(name not in wells for name in column):
        return None
    try:
        xs = [float(wells[name]["x"]) for name in column]
        ys = [float(wells[name]["y"]) for name in column]
    except (KeyError, TypeError, ValueError):
        return None
    if (not all(math.isfinite(value) for value in xs + ys)
            or any(abs(value - xs[0]) > 0.5 for value in xs)
            or any(abs(abs(ys[index] - ys[index + 1]) - 9.0) > 0.5
                   for index in range(7))):
        return None
    return column[0]


def _trough_anchors(
    definition: dict[str, Any], ordered: tuple[str, ...],
    *, largest_tip_diameter_mm: float | None,
) -> frozenset[str]:
    """Accept centered eight-tip trough access only with catalog-proven clearance.

    The catalog's `centerMultichannelOnWells` quirk specifies centering. Eight
    9 mm-spaced tips span 63 mm; an installed tip-rack well opening is a
    conservative diameter for a tip inside the trough. Missing dimensions or
    tip geometry mean no proof and therefore no multichannel anchor.
    """
    parameters = definition.get("parameters") or {}
    if (parameters.get("format") != "trough"
            or "centerMultichannelOnWells" not in parameters.get("quirks", ())
            or largest_tip_diameter_mm is None):
        return frozenset()
    wells = definition["wells"]
    anchors: set[str] = set()
    for name in ordered:
        details = wells[name]
        if not isinstance(details, dict) or details.get("shape") != "rectangular":
            continue
        try:
            width = float(details["xDimension"])
            length = float(details["yDimension"])
        except (KeyError, TypeError, ValueError):
            continue
        if (math.isfinite(width) and math.isfinite(length)
                and width >= largest_tip_diameter_mm
                and length >= 63.0 + largest_tip_diameter_mm):
            anchors.add(name)
    return frozenset(anchors)


def facts_from_definitions(
    definitions: dict[str, Any], pipette_definitions: dict[str, Any],
) -> tuple[dict[str, LabwareFacts], dict[str, PipetteFacts]]:
    """Validate simulator definitions and retain only compilable facts."""
    labware: dict[str, LabwareFacts] = {}
    tip_diameters: list[float] = []
    for definition in definitions.values():
        if not isinstance(definition, dict):
            continue
        parameters = definition.get("parameters") or {}
        if parameters.get("isTiprack") is not True:
            continue
        wells = definition.get("wells") or {}
        if not isinstance(wells, dict):
            continue
        for details in wells.values():
            if isinstance(details, dict):
                value = details.get("diameter")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
                    tip_diameters.append(float(value))
    largest_tip_diameter = max(tip_diameters) if tip_diameters else None
    for name, definition in definitions.items():
        if not isinstance(definition, dict):
            continue
        ordering = definition.get("ordering")
        wells = definition.get("wells")
        parameters = definition.get("parameters") or {}
        if (not isinstance(ordering, list) or not ordering
                or not all(isinstance(column, list) and column
                           and all(isinstance(well, str) for well in column)
                           for column in ordering)
                or not isinstance(wells, dict) or not isinstance(parameters, dict)):
            continue
        ordered = tuple(well for column in ordering for well in column)
        if len(ordered) != len(set(ordered)) or set(ordered) != set(wells):
            continue
        is_tiprack = parameters.get("isTiprack") is True
        capacity: float | None = None
        if is_tiprack:
            capacities = {details.get("totalLiquidVolume")
                          for details in wells.values() if isinstance(details, dict)}
            if len(capacities) != 1:
                continue
            try:
                capacity = float(next(iter(capacities)))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(capacity) or capacity <= 0:
                continue
        anchors = frozenset(anchor for column in ordering
                            if (anchor := _column_anchor(column, wells)) is not None)
        if not is_tiprack:
            anchors |= _trough_anchors(
                definition, ordered, largest_tip_diameter_mm=largest_tip_diameter,
            )
        labware[name] = LabwareFacts(
            wells=frozenset(ordered), ordered_wells=ordered,
            is_tiprack=is_tiprack, tip_capacity_ul=capacity,
            multichannel_compatible=bool(anchors),
            multichannel_anchor_wells=anchors,
        )
    pipettes: dict[str, PipetteFacts] = {}
    for name, definition in pipette_definitions.items():
        if not isinstance(definition, dict):
            continue
        try:
            minimum = float(definition["minVolume"])
            maximum = float(definition["maxVolume"])
            channels = int(definition["channels"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not math.isfinite(minimum) or not math.isfinite(maximum)
                or not 0 < minimum <= maximum or channels not in {1, 8}):
            continue
        pipettes[name] = PipetteFacts(minimum, maximum, channels)
    return labware, pipettes


def load_trusted_catalog(
    instruction: str, *, simulator: Path, labware_dir: Path | None = None,
) -> tuple[dict[str, LabwareFacts], dict[str, PipetteFacts]]:
    """Resolve named definitions using the simulator's installed shared data."""
    python = simulator.with_name("python")
    if not simulator.is_file() or not python.is_file():
        raise ValueError("The selected simulator and its Python interpreter are required")
    names = catalog_names(instruction)
    query = {"names": names}
    script = (
        "import json,sys\n"
        "from opentrons_shared_data.labware import load_definition\n"
        "from opentrons_shared_data.pipette import name_config\n"
        "names=json.loads(sys.stdin.read())['names']\n"
        "labware={}\n"
        "for name in names:\n"
        " try: labware[name]=load_definition(name,1)\n"
        " except (FileNotFoundError,KeyError,ValueError): pass\n"
        "pipettes={name:spec for name,spec in name_config().items() if name in names}\n"
        "print(json.dumps({'labware':labware,'pipettes':pipettes}))\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", script], input=json.dumps(query),
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Could not read installed Opentrons definitions: {exc}") from exc
    if result.returncode != 0:
        raise ValueError(f"Installed Opentrons catalog lookup failed: {result.stderr[-500:]}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError("Installed Opentrons catalog returned invalid JSON") from exc
    definitions = payload.get("labware")
    pipette_definitions = payload.get("pipettes")
    if not isinstance(definitions, dict) or not isinstance(pipette_definitions, dict):
        raise ValueError("Installed Opentrons catalog returned invalid definitions")
    if labware_dir is not None:
        for path in sorted(labware_dir.glob("*.json"))[:20]:
            try:
                definition = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(definition, dict):
                name = (definition.get("parameters") or {}).get("loadName")
                if isinstance(name, str) and name in names:
                    definitions[name] = definition
    return facts_from_definitions(definitions, pipette_definitions)


def module_deck_requests(
    instruction: str, *, known_labware: set[str],
) -> list[dict[str, Any]]:
    """Read only fixed module rows that name an installed module alias."""
    requests: list[dict[str, Any]] = []
    for line in instruction.splitlines():
        cells = [cell.strip() for cell in line.strip().split("|")[1:-1]]
        if len(cells) < 3 or "module" not in cells[1].casefold():
            continue
        if not re.fullmatch(r"[0-9,\s]+", cells[0]):
            continue
        aliases = re.findall(r"`'([^']+)'`", cells[1])
        if len(aliases) != 1:
            continue
        slots = sorted({int(value) for value in re.findall(r"\d+", cells[0])})
        if not slots or any(not 1 <= slot <= 11 for slot in slots):
            continue
        possible_labware = sorted({value for value in re.findall(r"`([^`\n]+)`", line)
                                   if value in known_labware})
        requests.append({
            "alias": aliases[0], "expected_slots": slots,
            "labware_load_names": possible_labware,
        })
    return requests


def load_trusted_module_catalog(
    instruction: str, *, simulator: Path, labware: dict[str, LabwareFacts],
    labware_dir: Path | None = None,
) -> dict[str, ModuleFacts]:
    """Preflight task-named module placements using the pinned simulator API.

    Module aliases, ranges, footprint and attached-labware acceptance are read
    from the installed Opentrons SDK. The task may restrict the candidate deck
    placement, but cannot create a new physical capability.
    """
    requests = module_deck_requests(instruction, known_labware=set(labware))
    if not requests:
        return {}
    python = simulator.with_name("python")
    if not python.is_file():
        raise ValueError("The selected simulator has no Python interpreter")
    custom: dict[str, Any] = {}
    if labware_dir is not None:
        for path in sorted(labware_dir.glob("*.json"))[:20]:
            try:
                definition = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(definition, dict):
                name = (definition.get("parameters") or {}).get("loadName")
                if isinstance(name, str) and name in labware:
                    custom[name] = definition
    query = {"requests": requests, "custom": custom}
    script = (
        "import json,sys\n"
        "from opentrons.simulate import get_protocol_api\n"
        "from opentrons.protocol_api.validation import ensure_module_model\n"
        "from opentrons_shared_data.module import load_definition\n"
        "from opentrons.drivers.thermocycler.driver import BLOCK_TARGET_MIN,BLOCK_TARGET_MAX,LID_TARGET_MIN,LID_TARGET_MAX\n"
        "from opentrons.protocol_engine.state.module_substates.temperature_module_substate import TEMP_MODULE_TEMPERATURE_RANGE\n"
        "from opentrons.hardware_control.modules.magdeck import OFFSET_TO_LABWARE_BOTTOM,MAX_ENGAGE_HEIGHT\n"
        "from opentrons.protocol_engine.types import ModuleModel\n"
        "query=json.loads(sys.stdin.read())\n"
        "out={}\n"
        "for row in query['requests']:\n"
        " alias=row['alias']; expected=row['expected_slots']\n"
        " try:\n"
        "  model=ensure_module_model(alias)\n"
        "  definition=load_definition('3',model.value)\n"
        "  module_type=definition['moduleType']\n"
        "  kind={'thermocyclerModuleType':'thermocycler','temperatureModuleType':'temperature','magneticModuleType':'magnetic'}.get(module_type)\n"
        "  if kind is None: continue\n"
        "  if (kind=='thermocycler') != (len(expected)>1): continue\n"
        "  slot=None if kind=='thermocycler' else expected[0]\n"
        "  protocol=get_protocol_api('2.15',extra_labware=query['custom'],robot_type='OT-2')\n"
        "  loaded=protocol.load_module(alias) if slot is None else protocol.load_module(alias,slot)\n"
        "  occupied=[i for i in range(1,12) if protocol.deck[i] is not None]\n"
        "  if occupied!=expected: continue\n"
        "  compatible=[]\n"
        "  for load_name in row['labware_load_names']:\n"
        "   try:\n"
        "    check=get_protocol_api('2.15',extra_labware=query['custom'],robot_type='OT-2')\n"
        "    module=check.load_module(alias) if slot is None else check.load_module(alias,slot)\n"
        "    module.load_labware(load_name)\n"
        "   except Exception: continue\n"
        "   compatible.append(load_name)\n"
        "  if kind=='thermocycler': temperature=[BLOCK_TARGET_MIN,BLOCK_TARGET_MAX]; lid=[LID_TARGET_MIN,LID_TARGET_MAX]; magnet=None\n"
        "  elif kind=='temperature': temperature=[TEMP_MODULE_TEMPERATURE_RANGE.min,TEMP_MODULE_TEMPERATURE_RANGE.max]; lid=None; magnet=None\n"
        "  else:\n"
        "   temperature=None; lid=None\n"
        "   scale=2 if model==ModuleModel.MAGNETIC_MODULE_V1 else 1\n"
        "   magnet=[0,(MAX_ENGAGE_HEIGHT[model]-OFFSET_TO_LABWARE_BOTTOM[model])/scale]\n"
        "  out[alias]={'kind':kind,'physical_model':model.value,'occupied_slots':occupied,'slot':slot,'compatible_labware':compatible,'temperature_range_c':temperature,'lid_temperature_range_c':lid,'magnet_height_range_mm':magnet}\n"
        " except Exception: continue\n"
        "print(json.dumps(out))\n"
    )
    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", script], input=json.dumps(query),
            capture_output=True, text=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Could not preflight installed Opentrons modules: {exc}") from exc
    if completed.returncode != 0:
        raise ValueError(f"Opentrons module preflight failed: {completed.stderr[-500:]}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise ValueError("Opentrons module preflight returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Opentrons module preflight returned invalid facts")
    modules: dict[str, ModuleFacts] = {}
    for request in requests:
        alias = request["alias"]
        response = payload.get(alias)
        if not isinstance(response, dict) or alias in modules:
            continue
        occupied = frozenset(response.get("occupied_slots") or ())
        slot = response.get("slot")
        kind = response.get("kind")
        if (occupied != frozenset(request["expected_slots"])
                or kind not in {"thermocycler", "temperature", "magnetic"}
                or (kind == "thermocycler") != (slot is None)):
            continue
        modules[alias] = ModuleFacts(
            kind=kind,
            fixed_occupied_slots=occupied if slot is None else frozenset(),
            allowed_slots=frozenset({slot}) if slot is not None else frozenset(),
            occupied_slots_by_slot={slot: occupied} if slot is not None else {},
            compatible_labware_load_names=frozenset(response.get("compatible_labware") or ()),
            temperature_range_c=(tuple(response["temperature_range_c"])
                                 if response.get("temperature_range_c") else None),
            lid_temperature_range_c=(tuple(response["lid_temperature_range_c"])
                                     if response.get("lid_temperature_range_c") else None),
            magnet_height_range_mm=(tuple(response["magnet_height_range_mm"])
                                    if response.get("magnet_height_range_mm") else None),
        )
    return modules
