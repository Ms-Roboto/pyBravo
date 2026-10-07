"""Constrained line edits for model-authored OT-2 protocol repair.

This module never authors protocol code. It asks the local model for small
source edits and applies them mechanically before the normal validation gates.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pybravo.evals.text2wetlab.adapter import EventLog


class PatchError(ValueError):
    """A proposed edit cannot be applied to the exact rejected candidate."""


LINE_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "replacement": {"type": "string"},
                },
                "required": ["start_line", "end_line", "replacement"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["edits"],
    "additionalProperties": False,
}


def numbered_source(source: str) -> str:
    """Show exact 1-based line coordinates without changing the source."""
    return "\n".join(f"{number:04d}| {line}" for number, line in enumerate(source.splitlines(), 1))


def line_patch_messages(
    source: str,
    *,
    instruction: str,
    diagnostic: str,
    scientific_source: str | None = None,
) -> list[dict[str, str]]:
    """Ask for localized edits grounded in the original task and failure."""
    if not source.strip() or not instruction.strip() or not diagnostic.strip():
        raise PatchError("Source, task instruction, and failure diagnostic are required.")
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    task = "Benchmark task instruction (experimental facts to preserve):\n" + instruction.strip()
    if scientific_source and scientific_source.strip():
        task += "\n\nSupplied scientific method passage:\n" + scientific_source.strip()
    return [
        {
            "role": "system",
            "content": (
                "Edit a previously generated Opentrons OT-2 Python protocol to correct a specific "
                "validation or simulation failure. Return JSON with only an edits array. Each edit "
                "has 1-based start_line, inclusive end_line, and replacement text. To insert before "
                "line N, set start_line=N and end_line=N-1; line N=len(source)+1 appends. "
                "Make the smallest changes that fix the reported failure. Preserve all task-stated "
                "labware, deck slots, wells, volumes, temperatures, timing, source-to-destination "
                "mapping, and contamination policy. Do not remove a required operation to silence a "
                "failure. Do not use benchmark reference output or add simulation-only branches. "
                "Do not return a complete protocol or Markdown. The proposed edits will be applied "
                "mechanically and revalidated; invalid line ranges or broad rewrites are rejected."
            ),
        },
        {"role": "user", "content": task},
        {
            "role": "user",
            "content": (
                f"Rejected protocol SHA-256: {source_digest}\n"
                f"Failure diagnostic:\n{diagnostic[:4000]}\n\n"
                "Rejected protocol with exact 1-based line numbers:\n" + numbered_source(source)
            ),
        },
    ]


def apply_line_patch(source: str, payload: dict[str, Any]) -> str:
    """Apply a bounded set of nonoverlapping model edits to *source*.

    Editing is text-only. Callers must re-run static, simulator, event, and
    task-fact checks before accepting the resulting protocol.
    """
    edits = payload.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise PatchError("Expected one to eight line edits.")
    lines = source.splitlines()
    line_count = len(lines)
    normalized: list[tuple[int, int, str]] = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"start_line", "end_line", "replacement"}:
            raise PatchError("Each edit needs only start_line, end_line, and replacement.")
        start, end, replacement = edit["start_line"], edit["end_line"], edit["replacement"]
        if type(start) is not int or type(end) is not int or not isinstance(replacement, str):
            raise PatchError("Line coordinates must be integers and replacement must be text.")
        if not 1 <= start <= line_count + 1 or not 0 <= end <= line_count or start > end + 1:
            raise PatchError("Edit line range is outside the rejected protocol.")
        if "\x00" in replacement or "\r" in replacement or len(replacement) > 12_000:
            raise PatchError("Replacement contains unsupported characters or is too large.")
        if start <= end and end - start + 1 > 60:
            raise PatchError("A patch cannot replace more than 60 original lines at once.")
        normalized.append((start, end, replacement))
    normalized.sort()
    for (left_start, left_end, _), (right_start, right_end, _) in zip(normalized, normalized[1:]):
        if right_start <= max(left_end + 1, left_start):
            raise PatchError("Edits overlap or share an ambiguous insertion boundary.")
        if left_start == left_end + 1 and right_start == right_end + 1 and left_start == right_start:
            raise PatchError("Two edits insert at the same line boundary.")
    result = list(lines)
    for start, end, replacement in reversed(normalized):
        replacement_lines = replacement.splitlines()
        result[start - 1:end] = replacement_lines
    patched = "\n".join(result) + "\n"
    if patched == source:
        raise PatchError("The proposed edits did not change the protocol.")
    return patched


def _liquid_actions(log: EventLog) -> list[tuple[str, str, str, str, float, int]]:
    return [
        (str(event.get("kind")), str(event.get("instrument")),
         str(event.get("labware")), str(event.get("well")),
         float(event.get("volume") or 0), int(event.get("channels") or 1))
        for event in log.events if event.get("kind") in {"aspirate", "dispense"}
    ]


def _timed_module_actions(log: EventLog) -> Counter[str]:
    return Counter(
        str(event.get("text")) for event in log.events
        if event.get("kind") in {"thermocycler", "temperature", "magnet"}
        and "hold time" in str(event.get("text"))
    )


def preserve_existing_task_actions(baseline: EventLog, candidate: EventLog) -> str | None:
    """Protect existing transfers, deck bindings, and timed steps in a process repair."""
    if candidate.labware != baseline.labware:
        return "The patch changed the loaded labware catalog or deck mapping."
    if _liquid_actions(candidate) != _liquid_actions(baseline):
        return "The patch changed existing liquid transfers, volumes, wells, or order."
    if _timed_module_actions(candidate) != _timed_module_actions(baseline):
        return "The patch removed or changed a timed module action."
    return None
