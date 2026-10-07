"""Generate a benchmark OT-2 Python protocol from a written task.

This adapter never calls Bravo controllers or the Opentrons execution API. The
candidate is parsed and, when installed, run through ``opentrons_simulate`` in
a separate process. Simulation establishes API compatibility, not scientific
correctness or readiness for physical execution.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pybravo.evals.text2wetlab.geometry import labware_geometry_context
from pybravo.evals.text2wetlab.reaction import PipetteRange, Stroke, audit_strokes
from pybravo.evals.text2wetlab.source_context import prepare_scientific_source
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse, structured_json


class GenerationError(RuntimeError):
    """No validated OT-2 protocol was produced."""


class ProtocolValidationError(GenerationError):
    """A candidate is not a structurally valid OT-2 Python protocol."""


class EventSimulationError(GenerationError):
    """The candidate failed while the structured Opentrons log was generated."""


@dataclass(frozen=True)
class SimulationResult:
    status: str  # passed, failed, unavailable
    detail: str


@dataclass(frozen=True)
class GenerationResult:
    protocol_path: Path
    trace_path: Path
    attempts: int
    simulation: SimulationResult


@dataclass(frozen=True)
class EventLog:
    """Structured Opentrons actions from a trusted simulation logger."""

    events: list[dict[str, Any]]
    labware: dict[str, str]


@dataclass(frozen=True)
class EventValidationResult:
    status: str  # passed, failed
    detail: str
    event_count: int


_CODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """You write a single, self-contained Python Protocol API v2 file for an Opentrons OT-2 benchmark task. Return a JSON object with exactly one key, code, containing the complete Python source. The task text and any supplied scientific source are the only evidence for experimental requirements. Do not use a memorized public solution or assume benchmark reference output.

Use `from opentrons import protocol_api`, `metadata = {"apiLevel": "2.15"}` unless the task specifies another compatible API level, and `def run(protocol: protocol_api.ProtocolContext):`. This benchmark uses an OT-2 and Opentrons software 7.5.0. Use OT-2 deck slots 1 through 11; slot 12 is fixed trash. Load labware with its Opentrons load name and deck location, and load OT-2 pipettes with an explicit mount and suitable tip racks. For a ThermocyclerContext, `set_block_temperature(temperature, hold_time_minutes=...)` waits for the block and accepts 4–99 °C; it can instead take `hold_time_seconds=...`. `close_lid()` and `open_lid()` control the lid. The optional heated-lid `set_lid_temperature()` accepts only 37–110 °C. There is no public `wait_for_block()` or `wait_for_block_temperature()` method on ThermocyclerContext. If a task requires samples to be handled on a preconditioned module, explicitly set that module's temperature before the first sample-handling action; start the timed incubation only after all requested additions are complete. After a timed temperature pulse, change the block to the required next-stage temperature before serial pipetting into every sample; do not leave the first samples at the pulse temperature while the remaining additions are made. The protocol must remain importable and simulatable by Opentrons. Do not use Bravo APIs, robot connection APIs, external files, network access, environment variables, or code execution helpers.

Translate the written task faithfully: preserve stated deck slots, labware, pipettes, tip use, liquid volumes, well order, repetitions, mixing, pauses, and any specified parameters. Use loops and helper functions when they make the mapping clear. Opentrons labware indexing uses well-name strings, not integer positions or Well objects from another plate: for positional loops use `plate.wells()[i]` or zip the actual well objects from multiple labware; never use `plate[i]` or `plate[other_plate_well]`. A single `aspirate()` or `dispense()` location must be one Well, not a Python list of wells; iterate or use a suitable transfer helper. `.wells()` is column-major: derive the number of rows from `len(plate.columns()[0])` when calculating indices, and confirm that multichannel spacing matches the actual labware geometry. A multichannel pipette can address one full, pitch-compatible column at a time, not a four-row tube rack as if it were an eight-row plate. Budget tip pickups against the loaded racks before writing loops; reserve source-isolated tips for sample contact, and use a compatible multichannel pipette for full-column operations when that reduces rack use without crossing samples. Always pass an explicit target well to `pipette.mix(repetitions, volume, well)`, because the implicit current location may be a tip rack. Balance reaction volumes and stated reagent concentrations from the materials actually loaded; do not create an unlisted water or reagent source. For a 2× reagent that must end at 1×, its transferred volume must be exactly half the final reaction volume. Sum every component, including primers and template, and check the final concentration before writing pipetting code. If a stock concentration or diluent is missing, state the narrowest workable assumption in a protocol comment instead of silently changing the final concentration; a source solution may be assumed pre-diluted only when that is consistent with the stated reagents and the assumed stock concentration is explicit. Distinguish one aspiration feeding several dispenses from separate aspirations and tip changes: follow the task's requested grouping and tip policy; when grouping is requested, split at pipette-capacity boundaries. For loops over distinct sample/reaction wells, pick up and drop a fresh tip inside each per-sample iteration once the tip touches sample liquid; do not carry one used tip across sample wells or back into a shared reagent reservoir. A tip that has entered a reagent stock or dispensed into a mixture cannot enter a different stock well; change tips between different reagent stocks. `transfer()`, `distribute()`, and similar helpers manage their own tip pickup by default. Never call a helper with its default tip policy while a manually picked-up tip is already attached: either let the helper manage tips, or use explicit aspirate/dispense (or `new_tip='never'`) with a manual pick/drop cycle. Call `reset_tipracks()` only after a rack is exhausted and the task permits refilling. The OT-2 GEN2 working ranges are P20 1–20 µL, P300 20–300 µL, and P1000 100–1000 µL. Effective capacity is the smaller of pipette capacity and loaded tip capacity; never aspirate beyond either one, dispense more than the current tip holds, or perform a liquid action without an attached tip. Finish with no tip attached. When a task is underspecified, make the narrowest workable assumption and state it in a protocol comment. Include no Markdown fences or explanation outside the code string."""

_ALLOWED_IMPORTS = {
    "opentrons.protocol_api",
    "opentrons.types",
    "typing",
    "math",
    "itertools",
    "collections",
}
_FORBIDDEN_CALLS = {
    "__import__", "breakpoint", "compile", "delattr", "dir", "eval", "exec",
    "getattr", "globals", "input", "locals", "open", "setattr", "vars",
}
_FORBIDDEN_NAMES = _FORBIDDEN_CALLS | {
    "BaseException", "KeyboardInterrupt", "SystemExit", "exit", "hasattr",
    "help", "memoryview", "quit", "type", "os", "sys", "subprocess",
    "builtins", "importlib", "print",
}
_FORBIDDEN_ATTRIBUTES = {
    "builtins", "environ", "exit", "getenv", "importlib", "modules", "open",
    "os", "popen", "read_text", "remove", "stderr", "stdin", "stdout",
    "subprocess", "sys", "system", "unlink", "write", "write_text", "writelines",
}
_CONTEXT_ACTIONS = {
    "delay", "load_adapter", "load_labware", "load_module", "load_instrument",
    "move_labware", "pause",
}
_SOURCE_TRAPS = (
    re.compile(
        r"\b(grader|grading|rubric|llm[ -]judge|the judge|evaluator)\b|score (this|it|all|each)\b"
        r"|full marks|ignore (all |any )?(previous|prior|above) instructions"
        r"|you are (an? )?(ai|language model|assistant)", re.IGNORECASE,
    ),
    re.compile(r"is_simulating", re.IGNORECASE),
    re.compile(r"\b(curl|wget)\b|os\.system|os\.popen", re.IGNORECASE),
    re.compile(r"load_labware_from_definition|\.json\b", re.IGNORECASE),
    re.compile(r"T2WL-(?:CANARY|HONEYPOT)-", re.IGNORECASE),
)


def _clean_code(code: str) -> str:
    code = code.strip()
    if code.startswith("```python\n") and code.endswith("```"):
        code = code[len("```python\n"):-3].strip()
    elif code.startswith("```\n") and code.endswith("```"):
        code = code[len("```\n"):-3].strip()
    return code + "\n" if code else ""


def _repair_guidance(error: str, code: str) -> str:
    """Turn a simulator diagnostic into a concise correction, without a solution."""
    line_match = re.search(r"\[line (\d+)\]", error)
    snippet = ""
    if line_match:
        line_number = int(line_match.group(1))
        lines = code.splitlines()
        start = max(1, line_number - 3)
        stop = min(len(lines), line_number + 3)
        snippet = "\nCode near the reported line:\n" + "\n".join(
            f"{number}: {lines[number - 1]}" for number in range(start, stop + 1)
        )
    if re.search(r"KeyError\s*\[line \d+\]:\s*\d+", error):
        return (
            "Opentrons Labware.__getitem__ expects a well name such as 'A1', not a numeric "
            "index. At the reported line, replace `plate[i]` with `plate.wells()[i]`, "
            "or iterate over zipped well objects from the source and destination labware. "
            "Check all labware lookups in the complete protocol for this error. Preserve "
            "every source-to-destination pairing and stated volume."
            + snippet
        )
    if re.search(r"KeyError\s*\[line \d+\]:\s*\w+\d+ of .+ on (?:slot|module)", error):
        return (
            "This KeyError shows a Well object from one labware was used as the key for "
            "another labware. `plate[well]` expects a well-name string; use "
            "`plate[well.well_name]`, or zip the `.wells()` lists of corresponding plates "
            "and use each Well object directly. Check every plate lookup and preserve "
            "one-to-one source/reaction mapping."
            + snippet
        )
    if "IndexError" in error and "list index out of range" in error:
        return (
            "Check the labware well indexing at the reported line. Opentrons `.wells()` "
            "is column-major: derive the row count with `len(plate.columns()[0])`, "
            "or use `plate.columns()[column_index][row_index]` directly. Do not use "
            "the number of columns as the stride, and do not assume a tube rack "
            "has the same row count or channel spacing as a 96-well plate. Preserve "
            "the requested sample-to-destination mapping."
            + snippet
        )
    if "Thermocycler lid temperature must be between" in error:
        return (
            "The Thermocycler block, not its heated lid, controls a 4 °C incubation. "
            "Use `set_block_temperature(4, hold_time_minutes=...)` for the requested "
            "block temperature. `set_lid_temperature()` only accepts 37–110 °C; "
            "`close_lid()` controls lid position without setting its heater. "
            "Preserve the task's temperature and hold time."
            + snippet
        )
    if ("ThermocyclerContext" in error
            and re.search(r"has no attribute '(?:wait_for_block|wait_for_block_temperature)'", error)):
        return (
            "ThermocyclerContext in Opentrons 7.5 has no public wait_for_block method. "
            "`set_block_temperature(temperature, hold_time_minutes=...)` already waits "
            "for target temperature and the requested "
            "hold. Remove the nonexistent call; preserve each temperature and duration."
            + snippet
        )
    if "OutOfTipsError" in error:
        return (
            "This pipette has consumed every tip in its configured rack. Check the number "
            "of fresh tips needed by all loops, not only one step. If the task explicitly "
            "allows rack refilling, call this pipette's `reset_tipracks()` after its rack is "
            "exhausted and before its next `pick_up_tip()`; do not reset early or silently "
            "reuse a contaminated tip. If refilling is not authorized, plan a new rack or "
            "an operator handoff instead. Preserve sample-isolated tip changes."
            + snippet
        )
    if "TipAttachedError" not in error:
        return ""
    return (
        "TipAttachedError means this pipette tried to pick up another tip while one was attached. "
        "Check the reported line and the current tip state. A manual `pick_up_tip()` followed by "
        "`transfer()` or `distribute()` with the default tip policy can cause this. Either remove the "
        "manual pickup and let the helper manage tips, or keep manual pickup/drop and use explicit "
        "aspirate/dispense or `new_tip='never'`. For distinct sample wells, drop the used tip and "
        "pick up a fresh one inside each sample iteration. Preserve all stated volumes and steps."
        + snippet
    )


def _validate_literal_pipette_volumes(run: ast.FunctionDef) -> None:
    """Reject literal strokes outside pipette or loaded-tip bounds before simulation."""
    instruments: dict[str, tuple[float, float]] = {}
    bounds = {"p20": (1.0, 20.0), "p300": (20.0, 300.0), "p1000": (100.0, 1000.0)}
    tipracks: dict[str, float] = {}

    def tip_capacity(value: ast.expr) -> float | None:
        if isinstance(value, ast.Name):
            return tipracks.get(value.id)
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "load_labware" and value.args
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)):
            match = re.search(r"tiprack_(\d+)ul\b", value.args[0].value, re.IGNORECASE)
            return float(match.group(1)) if match else None
        return None

    for node in ast.walk(run):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        value = node.value
        capacity = tip_capacity(value)
        if capacity is not None:
            tipracks[node.targets[0].id] = capacity
    for node in ast.walk(run):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        value = node.value
        if (not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute)
                or value.func.attr != "load_instrument" or not value.args
                or not isinstance(value.args[0], ast.Constant)
                or not isinstance(value.args[0].value, str)):
            continue
        rack_arg = next((item.value for item in value.keywords if item.arg == "tip_racks"), None)
        rack_capacities = [tip_capacity(rack) for rack in rack_arg.elts] if isinstance(rack_arg, (ast.List, ast.Tuple)) else []
        known_capacities = [item for item in rack_capacities if item is not None]
        for prefix, instrument_bounds in bounds.items():
            if value.args[0].value.startswith(prefix + "_"):
                minimum, maximum = instrument_bounds
                if known_capacities:
                    maximum = min(maximum, *known_capacities)
                instruments[node.targets[0].id] = (minimum, maximum)
                break
    volume_positions = {"aspirate": 0, "dispense": 0, "transfer": 0, "distribute": 0, "mix": 1}
    for node in ast.walk(run):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id not in instruments
                or node.func.attr not in volume_positions):
            continue
        if (node.func.attr == "mix" and len(node.args) < 3
                and not any(item.arg == "location" for item in node.keywords)):
            raise ProtocolValidationError(
                f"Line {node.lineno}: {node.func.value.id}.mix() must name its target well "
                "explicitly; an implicit current location may be the tip rack."
            )
        position = volume_positions[node.func.attr]
        volume_arg = node.args[position] if len(node.args) > position else next(
            (item.value for item in node.keywords if item.arg == "volume"), None
        )
        if not isinstance(volume_arg, ast.Constant) or isinstance(volume_arg.value, bool) or not isinstance(
                volume_arg.value, (int, float)):
            continue
        minimum, maximum = instruments[node.func.value.id]
        if not minimum <= volume_arg.value <= maximum:
            raise ProtocolValidationError(
                f"Line {node.lineno}: {node.func.value.id}.{node.func.attr}({volume_arg.value:g} µL) "
                f"is outside this pipette's {minimum:g}–{maximum:g} µL working range."
            )


def validate_ot2_source(code: str) -> None:
    """Check the protocol entry point and exclude obvious non-protocol code.

    This is a structural check; Opentrons simulation is the API/runtime gate.
    It is not a security sandbox for adversarial Python.
    """
    if not code.strip() or len(code) > 500_000:
        raise ProtocolValidationError("Candidate source is empty or exceeds 500 KB.")
    for pattern in _SOURCE_TRAPS:
        if match := pattern.search(code):
            raise ProtocolValidationError(f"Candidate contains prohibited benchmark text: {match.group(0)!r}.")
    try:
        tree = ast.parse(code, filename="protocol.py")
    except SyntaxError as exc:
        raise ProtocolValidationError(f"Python syntax error at line {exc.lineno}: {exc.msg}.") from exc

    api_level: str | None = None
    run_functions: list[ast.FunctionDef] = []
    has_opentrons_import = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_IMPORTS:
                    raise ProtocolValidationError(f"Unsupported import: {alias.name}.")
                has_opentrons_import |= alias.name.startswith("opentrons")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ProtocolValidationError("Relative imports are unsupported.")
            if node.module == "__future__":
                if any(alias.name != "annotations" for alias in node.names):
                    raise ProtocolValidationError("Only future annotations are supported.")
            elif node.module == "opentrons":
                if any(alias.name not in {"protocol_api", "types"} for alias in node.names):
                    raise ProtocolValidationError("Only Opentrons protocol_api and types can be imported.")
                has_opentrons_import = True
            elif node.module == "opentrons.protocol_api":
                if any(alias.name != "ProtocolContext" for alias in node.names):
                    raise ProtocolValidationError("Only ProtocolContext can be imported from opentrons.protocol_api.")
                has_opentrons_import = True
            elif node.module == "opentrons.types":
                if any(alias.name != "Point" for alias in node.names):
                    raise ProtocolValidationError("Only Point can be imported from opentrons.types.")
                has_opentrons_import = True
            elif node.module not in _ALLOWED_IMPORTS or any(alias.name == "*" for alias in node.names):
                raise ProtocolValidationError(f"Unsupported import: {node.module or ''}.")
            else:
                has_opentrons_import |= node.module.startswith("opentrons")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(not isinstance(target, ast.Name) for target in targets):
                raise ProtocolValidationError("Top-level assignments must use simple names.")
            if any(target.id in {"protocol_api", "run"} for target in targets):
                raise ProtocolValidationError("Cannot overwrite the Opentrons API or run entry point.")
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
                raise ProtocolValidationError("Top-level assignments must be literal data.") from exc
            if any(target.id in {"metadata", "requirements"} for target in targets):
                if not isinstance(value, dict):
                    raise ProtocolValidationError("metadata/requirements must be literal dictionaries.")
                if value.get("robotType") not in {None, "OT-2"}:
                    raise ProtocolValidationError("Protocol must target an OT-2.")
                level = value.get("apiLevel")
                if level is not None:
                    api_level = level
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(node, ast.AsyncFunctionDef):
                raise ProtocolValidationError("Protocol functions must be synchronous.")
            if node.decorator_list:
                raise ProtocolValidationError("Top-level function decorators are unsupported.")
            for default in [*node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)]:
                try:
                    ast.literal_eval(default)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
                    raise ProtocolValidationError("Function defaults must be literal data.") from exc
            annotations = [node.returns, *(arg.annotation for arg in [*node.args.posonlyargs, *node.args.args,
                                                                      *node.args.kwonlyargs])]
            if any(isinstance(item, (ast.Call, ast.Lambda, ast.IfExp, ast.NamedExpr))
                   for annotation in annotations if annotation is not None for item in ast.walk(annotation)):
                raise ProtocolValidationError("Executable function annotations are unsupported.")
            if node.name == "run":
                run_functions.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # Module docstring.
        else:
            raise ProtocolValidationError("Executable top-level statements are unsupported.")

    if not has_opentrons_import:
        raise ProtocolValidationError("Import the Opentrons Python Protocol API.")
    if not isinstance(api_level, str) or not re.fullmatch(r"2\.\d+", api_level):
        raise ProtocolValidationError("Declare a literal Protocol API v2 apiLevel.")
    if len(run_functions) != 1:
        raise ProtocolValidationError("Define exactly one run(protocol) function.")
    run = run_functions[0]
    if len(run.args.posonlyargs) + len(run.args.args) != 1 or run.args.vararg or run.args.kwarg or run.args.kwonlyargs:
        raise ProtocolValidationError("run() must take exactly one protocol context argument.")
    if run.args.defaults or run.args.kw_defaults:
        raise ProtocolValidationError("run() must not have default arguments.")
    _validate_literal_pipette_volumes(run)
    context_name = (run.args.posonlyargs + run.args.args)[0].arg
    run_nodes = set(ast.walk(run))
    action_seen = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body:
            raise ProtocolValidationError("Imports inside protocol functions are unsupported.")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ProtocolValidationError("Global or nonlocal mutation is unsupported.")
        if isinstance(node, ast.Name) and (node.id in _FORBIDDEN_NAMES or node.id.startswith("__")):
            raise ProtocolValidationError(f"Unsupported name: {node.id}.")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRIBUTES):
            raise ProtocolValidationError(f"Unsupported attribute: {node.attr}.")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete, ast.For, ast.withitem)):
            if isinstance(node, (ast.Assign, ast.Delete)):
                targets = node.targets
            elif isinstance(node, ast.withitem):
                targets = [node.optional_vars] if node.optional_vars else []
            else:
                targets = [node.target]
            for target in targets:
                for attribute in (item for item in ast.walk(target) if isinstance(item, ast.Attribute)
                                  and not isinstance(item.ctx, ast.Load)):
                    flow_rate = (isinstance(node, ast.Assign) and attribute.attr in {
                        "aspirate", "dispense", "blow_out",
                    } and isinstance(attribute.value, ast.Attribute)
                        and attribute.value.attr == "flow_rate"
                        and isinstance(attribute.value.value, ast.Name))
                    if not flow_rate:
                        raise ProtocolValidationError("Assignment to object attributes is unsupported.")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(
                part in node.value for part in ("/tests", "/logs", "/solution")):
            raise ProtocolValidationError("Candidate contains a path to benchmark grader files.")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                raise ProtocolValidationError(f"Unsupported call: {node.func.id}().")
            if isinstance(node.func, ast.Attribute) and node.func.attr in _FORBIDDEN_CALLS:
                raise ProtocolValidationError(f"Unsupported call: {node.func.attr}().")
            if node in run_nodes and (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == context_name
                    and node.func.attr in _CONTEXT_ACTIONS):
                action_seen = True
    if not action_seen:
        raise ProtocolValidationError("run() must contain an OT-2 protocol action, not only comments or pass.")


def simulate_protocol(
    path: Path, *, simulator_command: str | Path | None = None,
    labware_dir: str | Path | None = None, timeout_s: float = 180.0,
) -> SimulationResult:
    """Run only the Opentrons simulator CLI in a bounded subprocess."""
    command = str(simulator_command or "opentrons_simulate")
    executable = shutil.which(command)
    if executable is None:
        return SimulationResult("unavailable", f"Simulator command {command!r} is not installed.")
    simulator_env = _sanitized_simulator_env()
    arguments = [executable]
    if labware_dir is not None:
        arguments.extend(["-L", str(labware_dir)])
    arguments.append(str(path))
    try:
        completed = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout_s,
            check=False, env=simulator_env,
        )
    except subprocess.TimeoutExpired:
        return SimulationResult("failed", f"Opentrons simulation exceeded {timeout_s:g} seconds.")
    except OSError as exc:
        return SimulationResult("failed", f"Could not start Opentrons simulator: {exc}.")
    if completed.returncode == 0:
        return SimulationResult("passed", "opentrons_simulate completed successfully.")
    output = (completed.stderr or completed.stdout or "No simulator diagnostic output.").strip()
    return SimulationResult("failed", f"opentrons_simulate exited {completed.returncode}: {output[-4000:]}")


def _sanitized_simulator_env() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items()
        if not re.search(r"token|api.?key|secret|password|credential|auth|proxy", key, re.IGNORECASE)
    }


def record_simulation_events(
    path: Path,
    *,
    event_logger_path: str | Path | None,
    simulator_command: str | Path | None,
    labware_dir: str | Path | None = None,
    timeout_s: float = 180.0,
) -> EventLog:
    """Run a supplied event logger with the simulator's Python interpreter.

    The benchmark runner supplies the task's pinned Opentrons runlog script.
    The script and protocol run only after static source validation.
    """
    if event_logger_path is None:
        raise GenerationError("A structured event logger is required before accepting a simulated protocol; provide --event-logger.")
    logger_path = Path(event_logger_path).expanduser().resolve()
    if not logger_path.is_file():
        raise GenerationError(f"Structured event logger does not exist: {logger_path}.")
    simulator = shutil.which(str(simulator_command or "opentrons_simulate"))
    if simulator is None:
        raise GenerationError("Opentrons simulator disappeared before event validation.")
    interpreter = Path(simulator).with_name("python")
    if not interpreter.is_file():
        raise GenerationError(f"Simulator environment has no Python interpreter: {interpreter}.")
    with tempfile.TemporaryDirectory(prefix=".ot2-events-", dir=path.parent) as directory:
        temp_dir = Path(directory)
        event_labware_dir = Path(labware_dir) if labware_dir is not None else temp_dir / "labware"
        if labware_dir is None:
            event_labware_dir.mkdir()
        output = temp_dir / "events.json"
        try:
            completed = subprocess.run(
                [str(interpreter), str(logger_path), str(path), str(event_labware_dir), str(output)],
                capture_output=True, text=True, timeout=timeout_s, check=False,
                env=_sanitized_simulator_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GenerationError(f"Structured event logging exceeded {timeout_s:g} seconds.") from exc
        except OSError as exc:
            raise GenerationError(f"Could not run structured event logger: {exc}.") from exc
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GenerationError("Structured event logger did not write valid JSON.") from exc
        if payload.get("ok") is False and isinstance(payload.get("error"), str):
            raise EventSimulationError(f"Structured Opentrons simulation failed: {payload['error'][-3000:]}")
        if completed.returncode != 0 or payload.get("ok") is not True:
            detail = str(completed.stderr or "unknown error")[-3000:]
            raise GenerationError(f"Structured event logger failed: {detail}")
        events = payload.get("events")
        labware = payload.get("labware")
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise GenerationError("Structured event logger did not return an events array.")
        if not isinstance(labware, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                    for key, value in labware.items()):
            raise GenerationError("Structured event logger did not return a labware map.")
        return EventLog(events, labware)


def validate_event_contamination(log: EventLog) -> EventValidationResult:
    """Reject tip carryover between wells of a specimen-source labware.

    A source is any non-reservoir labware that is aspirated somewhere in the
    simulated protocol. This distinguishes wells that contain samples from an
    empty destination-only plate; one reservoir may feed many empty wells.
    """
    aspirated_labware = {
        event.get("labware") for event in log.events if event.get("kind") == "aspirate"
    }
    specimen_labware = {
        name for name in aspirated_labware if isinstance(name, str)
        and "reservoir" not in (log.labware.get(name, name)).lower()
        and "trash" not in (log.labware.get(name, name)).lower()
        and "waste" not in (log.labware.get(name, name)).lower()
    }
    active: dict[str, bool] = {}
    touched: dict[str, set[tuple[str, str]]] = {}
    reagent_sources: dict[str, set[tuple[str, str]]] = {}
    errors: list[str] = []
    backflow_error: str | None = None
    for index, event in enumerate(log.events, start=1):
        kind = event.get("kind")
        instrument = event.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            continue
        if kind == "pick":
            active[instrument] = True
            touched[instrument] = set()
            reagent_sources[instrument] = set()
            continue
        if kind == "drop":
            active[instrument] = False
            touched[instrument] = set()
            reagent_sources[instrument] = set()
            continue
        if kind not in {"aspirate", "dispense"}:
            continue
        labware = event.get("labware")
        well = event.get("well")
        if not isinstance(labware, str) or not isinstance(well, str):
            errors.append(f"Event {index} has no labware/well identity for a liquid action.")
            break
        if not active.get(instrument, False):
            errors.append(f"Event {index}: {instrument} {kind}s at {well} of {labware} without a tracked tip.")
            break
        if kind == "aspirate" and "reservoir" in (log.labware.get(labware, labware)).lower():
            source = (labware, well)
            prior_sources = reagent_sources.setdefault(instrument, set())
            if prior_sources and source not in prior_sources:
                first_labware, first_well = sorted(prior_sources)[0]
                errors.append(
                    f"Event {index}: one {instrument} tip aspirated from reagent source "
                    f"{first_well} of {first_labware} and then {well} of {labware}. "
                    "Use a fresh tip between distinct reagent stocks."
                )
                break
            prior_sources.add(source)
        if labware in specimen_labware:
            current = (labware, well)
            prior = touched.setdefault(instrument, set())
            if current not in prior and prior:
                first_labware, first_well = sorted(prior)[0]
                errors.append(
                    f"Event {index}: one {instrument} tip touched {first_well} of {first_labware} and then "
                    f"{well} of {labware} before being dropped. Use a fresh tip between distinct "
                    "sample/reaction wells, including mixes and additions."
                )
                break
            prior.add(current)
        elif kind == "aspirate" and touched.get(instrument):
            first_labware, first_well = sorted(touched[instrument])[0]
            backflow_error = backflow_error or (
                f"Event {index}: {instrument} re-entered {well} of {labware} after touching "
                f"{first_well} of {first_labware} with the same tip. Drop that tip before returning "
                "to a shared reagent source."
            )
    if errors:
        return EventValidationResult("failed", errors[0], len(log.events))
    if backflow_error:
        return EventValidationResult("failed", backflow_error, len(log.events))
    attached = sorted(instrument for instrument, has_tip in active.items() if has_tip)
    if attached:
        return EventValidationResult(
            "failed", f"Protocol ended with an attached tip on {', '.join(attached)}. Drop the tip before finishing.",
            len(log.events),
        )
    return EventValidationResult("passed", "No per-tip carryover across distinct specimen wells was detected.",
                                 len(log.events))


def validate_event_pipette_ranges(log: EventLog) -> EventValidationResult:
    """Check OT-2 GEN2 pipette working ranges on the structured action log.

    Opentrons simulation does not reject every below-range liquid action. The
    benchmark tasks explicitly state these working ranges, so a simulated
    protocol must still use an appropriate pipette for each stroke.
    """
    bounds = {20: (1.0, 20.0), 300: (20.0, 300.0), 1000: (100.0, 1000.0)}
    for index, event in enumerate(log.events, start=1):
        if event.get("kind") not in {"aspirate", "dispense"}:
            continue
        instrument = event.get("instrument")
        match = re.search(r"\bP(20|300|1000)\b", instrument, re.IGNORECASE) if isinstance(instrument, str) else None
        if match is None:
            continue
        minimum, maximum = bounds[int(match.group(1))]
        volume = event.get("volume")
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not minimum <= volume <= maximum:
            return EventValidationResult(
                "failed",
                f"Event {index}: {instrument} {event['kind']} volume {volume!r} µL is outside "
                f"its stated {minimum:g}–{maximum:g} µL working range.",
                len(log.events),
            )
    return EventValidationResult("passed", "OT-2 GEN2 liquid strokes are within working ranges.",
                                 len(log.events))


def validate_event_stroke_accounting(log: EventLog) -> EventValidationResult:
    """Track tip state and held liquid from observed simulator actions."""
    bounds = {20: (1.0, 20.0), 300: (20.0, 300.0), 1000: (100.0, 1000.0)}
    ranges: dict[str, PipetteRange] = {}
    strokes: list[Stroke] = []
    actions = {"pick": "pick_up_tip", "drop": "drop_tip", "aspirate": "aspirate",
               "dispense": "dispense", "blow_out": "blow_out"}
    for event in log.events:
        kind = event.get("kind")
        if kind not in actions:
            continue
        instrument = event.get("instrument")
        if not isinstance(instrument, str):
            return EventValidationResult("failed", "Liquid event has no instrument identity.", len(log.events))
        match = re.search(r"\bP(20|300|1000)\b", instrument, re.IGNORECASE)
        if match is None:
            return EventValidationResult(
                "failed", f"Unknown pipette model in structured event log: {instrument}.", len(log.events)
            )
        minimum, maximum = bounds[int(match.group(1))]
        ranges[instrument] = PipetteRange(instrument, minimum, maximum)
        labware, well = event.get("labware"), event.get("well")
        location = f"{labware}:{well}" if isinstance(labware, str) and isinstance(well, str) else None
        strokes.append(Stroke(instrument, actions[kind], event.get("volume"), location))
    issues = audit_strokes(strokes, ranges.values())
    if issues:
        return EventValidationResult("failed", issues[0].message, len(log.events))
    return EventValidationResult("passed", "Tip and held-volume accounting is consistent.", len(log.events))


def validate_heat_shock_transition(log: EventLog, instruction: str) -> EventValidationResult:
    """Keep samples out of a short heat-shock pulse during serial recovery work.

    This rule applies only when the scientist's instruction calls for an
    on-deck thermocycler heat shock followed by recovery. It does not infer a
    temperature program for other protocols.
    """
    text = instruction.lower()
    if not (re.search(r"heat[ -]shock", text) and "thermocycler" in text and "recover" in text):
        return EventValidationResult("passed", "No on-deck heat-shock transition requested.", len(log.events))
    pulse_index = None
    for index, event in enumerate(log.events):
        if event.get("kind") != "thermocycler":
            continue
        command = event.get("text")
        if not isinstance(command, str):
            continue
        match = re.search(r"block temperature to (\d+(?:\.\d+)?)\s*°C", command)
        if match and float(match.group(1)) >= 40 and "hold time" in command:
            pulse_index = index
            break
    if pulse_index is None:
        return EventValidationResult(
            "failed", "Requested on-deck heat shock has no timed high-temperature block command.",
            len(log.events),
        )
    for index in range(pulse_index + 1, len(log.events)):
        event = log.events[index]
        if event.get("kind") == "thermocycler" and isinstance(event.get("text"), str):
            match = re.search(r"block temperature to (\d+(?:\.\d+)?)\s*°C", event["text"])
            if match and float(match.group(1)) < 40:
                return EventValidationResult(
                    "passed", "Thermocycler left the heat-shock temperature before recovery work.",
                    len(log.events),
                )
        if event.get("kind") in {"aspirate", "dispense", "pick", "drop"}:
            return EventValidationResult(
                "failed", f"Event {index + 1}: liquid handling occurs after a timed heat-shock pulse "
                "while the block remains at the high temperature. Change the block to the "
                "recovery temperature before serial additions.", len(log.events),
            )
    return EventValidationResult(
        "failed", "Requested recovery has no lower-temperature block command after heat shock.",
        len(log.events),
    )


def validate_event_safety(log: EventLog, *, instruction: str | None = None) -> EventValidationResult:
    """Apply contamination, range, stroke, and supplied-process checks."""
    contamination = validate_event_contamination(log)
    if contamination.status != "passed":
        return contamination
    ranges = validate_event_pipette_ranges(log)
    if ranges.status != "passed":
        return ranges
    strokes = validate_event_stroke_accounting(log)
    if strokes.status != "passed":
        return strokes
    return validate_heat_shock_transition(log, instruction) if instruction else strokes


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=".trace-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


async def generate_ot2_protocol(
    instruction: str,
    task_dir: str | Path,
    *,
    scientific_source: str | None = None,
    config: LocalLLMConfig | None = None,
    simulator_command: str | Path | None = None,
    event_logger_path: str | Path | None = None,
    labware_dir: str | Path | None = None,
    repair_attempts: int = 2,
    simulation_timeout_s: float = 180.0,
    http_client: Any = None,
    completion: Callable[..., Awaitable[StructuredResponse]] | None = None,
    event_reader: Callable[[Path], EventLog] | None = None,
) -> GenerationResult:
    """Generate, validate, and optionally simulate one benchmark protocol.

    ``task_dir`` is the sole output directory. A failed run leaves its trace
    there but never leaves a stale or rejected ``protocol.py`` for a grader.
    """
    if not instruction.strip():
        raise ValueError("A nonempty benchmark instruction is required.")
    if not 0 <= repair_attempts <= 5:
        raise ValueError("repair_attempts must be between 0 and 5.")
    directory = Path(task_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    resolved_labware_dir = Path(labware_dir).expanduser().resolve() if labware_dir is not None else None
    if resolved_labware_dir is not None and not resolved_labware_dir.is_dir():
        raise ValueError(f"Custom labware directory does not exist: {resolved_labware_dir}.")
    output_path = directory / "protocol.py"
    trace_path = directory / "generation_trace.json"
    output_path.unlink(missing_ok=True)
    for prior_candidate in directory.glob("candidate_attempt_*.py"):
        if re.fullmatch(r"candidate_attempt_\d+\.py", prior_candidate.name):
            prior_candidate.unlink()
    trace: dict[str, Any] = {
        "task_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "attempts": [],
        "status": "running",
        "static_validation_passed": False,
        "event_validation_passed": False,
    }
    task_text = "Benchmark task instruction:\n" + instruction.strip()
    if scientific_source and scientific_source.strip():
        source_context = prepare_scientific_source(scientific_source)
        trace["scientific_source"] = {
            "strategy": source_context.strategy,
            "source_sha256": source_context.source_sha256,
            "excerpt_sha256": source_context.excerpt_sha256,
            "start_line": source_context.start_line,
            "end_line": source_context.end_line,
        }
        task_text += "\n\nVerbatim scientific source passage supplied with the task:\n" + source_context.text
    geometry = await asyncio.to_thread(
        labware_geometry_context, instruction,
        simulator_command=simulator_command, labware_dir=resolved_labware_dir,
    )
    trace["labware_geometry"] = geometry
    base_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": task_text},
    ]
    if geometry:
        base_messages.append({
            "role": "user",
            "content": (
                "Read-only geometry from the installed Opentrons simulator catalog for labware "
                "named in the task. Use these facts for well indexing and multichannel layout; "
                "they do not establish experimental volumes or reagent choices:\n"
                + json.dumps(geometry, sort_keys=True)
            ),
        })
    complete = completion or structured_json
    prior_code: str | None = None
    prior_error: str | None = None
    rejected_hashes: dict[str, int] = {}
    try:
        for index in range(repair_attempts + 1):
            messages = list(base_messages)
            if prior_error is not None:
                guidance = _repair_guidance(prior_error, prior_code or "")
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous generated code failed validation or simulation. Correct it while preserving "
                        "all experimental requirements. Return a fresh complete code string.\n\n"
                        f"Failure: {prior_error[:4000]}\n\n"
                        + (f"Specific correction:\n{guidance}\n\n" if guidance else "")
                        + f"Previous code:\n{(prior_code or '')[:100_000]}"
                    ),
                })
            response = await complete(
                messages, _CODE_SCHEMA, config=config, schema_name="ot2_protocol",
                http_client=http_client,
            )
            raw_code = response.payload.get("code")
            if not isinstance(raw_code, str):
                code = ""
                failure = "Model response has no code string."
            else:
                code = _clean_code(raw_code)
                try:
                    validate_ot2_source(code)
                except ProtocolValidationError as exc:
                    failure = str(exc)
                else:
                    failure = None
            model_metadata = response.metadata
            attempt: dict[str, Any] = {
                "number": index + 1,
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "model": model_metadata.get("model"),
                "usage": model_metadata.get("usage"),
                "model_elapsed_s": model_metadata.get("elapsed_s"),
                "validation": "passed" if failure is None else "failed",
            }
            if failure is not None:
                attempt["error"] = failure
                trace["attempts"].append(attempt)
                prior_code, prior_error = code, failure
                continue
            candidate = directory / f"candidate_attempt_{index + 1}.py"
            candidate.write_text(code, encoding="utf-8")
            attempt["candidate_path"] = str(candidate)
            repeated_from = rejected_hashes.get(attempt["code_sha256"])
            if repeated_from is not None:
                attempt["duplicate_of_attempt"] = repeated_from
                attempt["simulation"] = "skipped_duplicate"
                duplicate_error = (
                    f"Generated code is byte-for-byte identical to rejected attempt {repeated_from}. "
                    "A repeated simulator run cannot fix this failure. Change the failing code before "
                    f"submitting another candidate. Previous failure: {prior_error or 'validation failed'}"
                )
                attempt["error"] = duplicate_error
                trace["attempts"].append(attempt)
                prior_code, prior_error = code, duplicate_error
                continue
            rejected_hashes[attempt["code_sha256"]] = index + 1
            trace["attempts"].append(attempt)
            simulation = await asyncio.to_thread(
                simulate_protocol, candidate, simulator_command=simulator_command,
                labware_dir=resolved_labware_dir,
                timeout_s=simulation_timeout_s,
            )
            attempt["simulation"] = simulation.status
            attempt["simulation_detail"] = simulation.detail
            if simulation.status == "passed":
                try:
                    if event_reader is not None:
                        event_log = await asyncio.to_thread(event_reader, candidate)
                    else:
                        event_log = await asyncio.to_thread(
                            record_simulation_events, candidate,
                            event_logger_path=event_logger_path,
                            simulator_command=simulator_command,
                            labware_dir=resolved_labware_dir,
                            timeout_s=simulation_timeout_s,
                        )
                except EventSimulationError as exc:
                    attempt["event_validation"] = "failed"
                    attempt["event_detail"] = str(exc)
                    prior_code, prior_error = code, str(exc)
                    continue
                event_validation = validate_event_safety(event_log, instruction=instruction)
                attempt["event_validation"] = event_validation.status
                attempt["event_count"] = event_validation.event_count
                attempt["event_detail"] = event_validation.detail
            if simulation.status == "failed":
                prior_code, prior_error = code, simulation.detail
                continue
            if simulation.status == "passed" and event_validation.status == "failed":
                prior_code, prior_error = code, event_validation.detail
                continue
            # An unavailable simulator is stated explicitly in the trace and result.
            os.replace(candidate, output_path)
            attempt.pop("candidate_path")
            trace["status"] = "simulated" if simulation.status == "passed" else "static_validated_only"
            trace["static_validation_passed"] = True
            trace["event_validation_passed"] = simulation.status == "passed"
            _write_json_atomic(trace_path, trace)
            return GenerationResult(output_path, trace_path, index + 1, simulation)
        trace["status"] = "failed"
        _write_json_atomic(trace_path, trace)
        raise GenerationError(
            f"No valid OT-2 protocol after {repair_attempts + 1} attempt(s); "
            f"last error: {prior_error}. Trace: {trace_path}"
        )
    except Exception as exc:
        if trace["status"] == "running":
            trace["status"] = "failed"
            trace["error"] = str(exc)
            _write_json_atomic(trace_path, trace)
        raise
