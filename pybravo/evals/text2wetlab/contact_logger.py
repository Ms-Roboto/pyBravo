"""Annotate the pinned OT-2 run log with trusted dispense-position evidence.

The dataset's logger records well identities but drops the location height.
This wrapper runs that exact logger and adds a Boolean only when the simulator's
raw dispense location is at or above the destination well rim. It does not
change the pinned event order, quantities, or labware identities. The annotation
is a narrow noncontact signal for local carryover review, not an assertion that
the overall protocol is ready for a physical robot.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path


def _at_or_above_well_rim(payload: dict) -> bool | None:
    location = payload.get("location")
    try:
        well = location.labware
        if hasattr(well, "as_well"):
            well = well.as_well()
        z = float(location.point.z)
        rim_z = float(well.top().point.z)
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(z) or not math.isfinite(rim_z):
        return None
    return z >= rim_z - 1e-6


def record(logger_path: Path, protocol_path: Path, labware_dir: Path) -> dict:
    spec = importlib.util.spec_from_file_location("pinned_text2wetlab_runlog", logger_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load pinned run logger: {logger_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_parse = module.parse

    def annotated_parse(text: str, payload: dict) -> dict | None:
        event = original_parse(text, payload)
        if event is not None and event.get("kind") == "dispense":
            evidence = _at_or_above_well_rim(payload)
            if evidence is not None:
                event["at_or_above_well_rim"] = evidence
        return event

    module.parse = annotated_parse
    result = module.main(str(protocol_path), str(labware_dir))
    if not isinstance(result, dict):
        raise RuntimeError("Pinned run logger returned no result object")
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        raise SystemExit("usage: contact_logger.py PINNED_LOGGER PROTOCOL LABWARE_DIR OUTPUT")
    _, logger, protocol, labware_dir, output = argv
    result = record(Path(logger), Path(protocol), Path(labware_dir))
    Path(output).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
