"""Optional local-model planning pass before OT-2 code generation.

The model authors the plan. Grounding and arithmetic checks decide whether it
can be shown to the code drafter; a failed plan falls back to the original
instruction, rather than being treated as experimental truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from pybravo.evals.text2wetlab.planning import (
    PLAN_SCHEMA,
    PLAN_SYSTEM_PROMPT,
    OT2Plan,
    PlanParseError,
    audit_plan,
    parse_plan,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse


@dataclass(frozen=True)
class PlanningResult:
    plan: OT2Plan | None
    attempts: tuple[dict[str, Any], ...]


async def run_grounded_plan(
    *,
    instruction: str,
    scientific_source: str | None,
    geometry: Mapping[str, Mapping[str, Any]] | None,
    directory: Path,
    completion: Callable[..., Awaitable[StructuredResponse]],
    config: LocalLLMConfig | None,
    http_client: Any = None,
    max_attempts: int = 2,
) -> PlanningResult:
    """Ask the local model for a cited plan and accept only an audited revision."""
    if not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    user_text = "Benchmark task instruction:\n" + instruction.strip()
    if scientific_source:
        user_text += "\n\nSupplied scientific source passage:\n" + scientific_source
    if geometry:
        user_text += (
            "\n\nRead-only installed Opentrons labware geometry (this does not supply "
            "reagents or liquid volumes):\n" + json.dumps(geometry, sort_keys=True)
        )
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    records: list[dict[str, Any]] = []
    for number in range(1, max_attempts + 1):
        response = await completion(
            messages, PLAN_SCHEMA, config=config, schema_name="ot2_evidence_plan",
            http_client=http_client,
        )
        payload = response.payload
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        record: dict[str, Any] = {
            "number": number,
            "model": response.metadata.get("model"),
            "usage": response.metadata.get("usage"),
            "model_elapsed_s": response.metadata.get("elapsed_s"),
            "payload_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        }
        (directory / f"planning_attempt_{number}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        try:
            plan = parse_plan(payload, instruction=instruction, scientific_source=scientific_source)
        except PlanParseError as exc:
            record["status"] = "ungrounded"
            record["error"] = str(exc)
            feedback = "Your plan was not grounded in the supplied source: " + str(exc)
        else:
            issues = audit_plan(plan, geometry=geometry)
            record["issues"] = [issue.__dict__ for issue in issues]
            errors = [issue for issue in issues if issue.severity == "error"]
            if not errors:
                record["status"] = "accepted"
                records.append(record)
                return PlanningResult(plan, tuple(records))
            record["status"] = "audit_failed"
            feedback = "Your plan failed these deterministic checks:\n" + "\n".join(
                f"- {issue.path}: {issue.message}" for issue in errors[:20]
            )
        records.append(record)
        if number < max_attempts:
            messages.append({"role": "assistant", "content": serialized})
            messages.append({
                "role": "user",
                "content": feedback + "\nReturn a corrected complete JSON plan with grounded quotes. "
                "Do not add absent deck reagents or silently waive tip or module constraints.",
            })
    return PlanningResult(None, tuple(records))
