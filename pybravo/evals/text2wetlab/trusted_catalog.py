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

from .action_ir import LabwareFacts, PipetteFacts

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


def facts_from_definitions(
    definitions: dict[str, Any], pipette_definitions: dict[str, Any],
) -> tuple[dict[str, LabwareFacts], dict[str, PipetteFacts]]:
    """Validate simulator definitions and retain only compilable facts."""
    labware: dict[str, LabwareFacts] = {}
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
