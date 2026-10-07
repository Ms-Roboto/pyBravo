"""Compact, read-only OT-2 labware geometry for benchmark code generation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _summary(definition: dict[str, Any]) -> dict[str, Any] | None:
    ordering = definition.get("ordering")
    if (not isinstance(ordering, list) or not ordering
            or not all(isinstance(column, list) and column
                           and all(isinstance(well, str) for well in column)
                           for column in ordering)):
        return None
    parameters = definition.get("parameters") or {}
    return {
        "columns": len(ordering),
        "rows_per_column": len(ordering[0]),
        "well_count": sum(len(column) for column in ordering),
        "first_column": ordering[0],
        "last_column": ordering[-1],
        "is_tiprack": bool(parameters.get("isTiprack")),
    }


def labware_geometry_context(
    instruction: str, *, simulator_command: str | Path | None,
    labware_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve named task labware against the simulator's pinned catalog.

    Only the installed shared-data library and explicitly supplied custom JSON
    definitions are read. Unknown names are omitted rather than guessed.
    """
    names = sorted(set(re.findall(r"`([a-z][a-z0-9_]{2,})`", instruction)))[:50]
    result: dict[str, dict[str, Any]] = {}
    simulator = shutil.which(str(simulator_command or "opentrons_simulate"))
    if simulator is not None:
        interpreter = Path(simulator).with_name("python")
        if interpreter.is_file():
            script = (
                "import json,sys\n"
                "from opentrons_shared_data.labware import load_definition\n"
                "out={}\n"
                "for name in json.loads(sys.stdin.read()):\n"
                " try: out[name]=load_definition(name,1)\n"
                " except (FileNotFoundError,KeyError,ValueError): pass\n"
                "print(json.dumps(out))\n"
            )
            try:
                completed = subprocess.run(
                    [str(interpreter), "-I", "-c", script], input=json.dumps(names),
                    capture_output=True, text=True, timeout=20, check=False,
                )
                definitions = json.loads(completed.stdout) if completed.returncode == 0 else {}
            except (subprocess.TimeoutExpired, OSError, ValueError):
                definitions = {}
            if isinstance(definitions, dict):
                for name, definition in definitions.items():
                    if name in names and isinstance(definition, dict):
                        summary = _summary(definition)
                        if summary is not None:
                            result[name] = summary
    if labware_dir is not None:
        for path in sorted(labware_dir.glob("*.json"))[:20]:
            try:
                definition = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(definition, dict):
                continue
            name = (definition.get("parameters") or {}).get("loadName")
            if isinstance(name, str) and name in names:
                summary = _summary(definition)
                if summary is not None:
                    result[name] = summary
    return result
