"""Optional local-model planning pass before OT-2 code generation.

The model authors the plan. Grounding and arithmetic checks decide whether it
can be shown to the code drafter; a failed plan falls back to the original
instruction, rather than being treated as experimental truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
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
from pybravo.evals.text2wetlab.source_fidelity import (
    SOURCE_FIDELITY_GUIDANCE,
    audit_cited_plan_quantities,
    audit_inventory_strength_claims,
    audit_manual_addition_stages,
    audit_parameterized_manual_stages,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, ProtocolLLMError, StructuredResponse

_PLANNING_CHECKLIST = """Before returning the plan, check these distinctions against the task and paper:
- A vessel described as empty at the start is a destination or later intermediate, not an initially available deck reagent. Give a generated source produced_by_stage equal to its earlier pipette or explicit manual handoff stage; never use its starting inventory quote as evidence that it already contains liquid. Its robot additions need stage_name equal to a later pipette stage.
- Name each starting source component from the actual inventory contents, rather than from the vessel label alone. For a plate of several named primers or templates, a plural category is acceptable only when the cited inventory quote identifies that category and its contents.
- A reaction record describes one physical final vessel unless its name explicitly says it is a batch preparation. Link each prepared batch to exactly one generated deck source with output_source_id; a revised recipe replaces the earlier executable preparation instead of adding a second preparation in the same vessel. Do not count the same liquid both as an existing product and as its component additions. Sum all additions and check each stated stock-to-final concentration. Keep separate fragment or sample identities when their volumes or destinations differ.
- Every robot-delivered addition needs stage_name equal to a named pipette stage. Every pipette stage needs a tip-demand row whose stage string is exactly the stage name, including case and spaces. A manual stage describes an actual operator action, not a substitute for on-deck pipetting.
- A reaction's total or a batch's total is not one pipette stroke. In each tip-demand row, stroke_volumes_ul contains physically executable single aspirate or dispense amounts within that named pipette's working range and tip capacity. Split larger transfers or choose the provided suitable pipette. Never round a below-minimum aliquot upward or claim a stock was diluted without supporting inventory evidence; mark an unautomatable addition as an explicit operator handoff. Count tips for every pipette stage, including shared-reagent distribution, repeated samples, mixing, and cross-sample changes. Add the demands across stages before calculating refill cycles. Plan a rack reset only when the task authorizes fresh-rack replenishment and demand actually exceeds loaded capacity; an exhausted rack must be replaced and reset before the next pickup, even when that pickup occurs inside a repeated stage.
- For a multichannel pipette, count one pickup per addressed, pitch-compatible full column and consume one tip per channel. A tube rack or partly populated geometry cannot be treated as a full multichannel plate merely because its wells have names. Keep individual sample identities and fresh-tip changes visible when samples enter or leave pooled reagent steps.
- Record temperature, lid, and magnet state changes before the pipetting that depends on them; distinguish a timed incubation from a mere setpoint change. Parameterized manual stages need their cited duration_s and temperature_c, one phase per stage, and after_stage links that preserve order. If a destination must be held at a specified temperature, set it before the first dispense there and record that requirement on the pipette stage. Preserve the source's stated order and duration.
If the source lacks a scientific setting, do not fabricate a value just to complete the schema; keep it unknown or make the required manual handoff explicit."""

_REPAIR_HINTS: dict[str, str] = {
    "empty_initial_source": "Treat initially empty labware as a generated intermediate, available only after its preparation stage.",
    "unknown_producer_stage": "Set produced_by_stage to the exact name of an existing earlier pipette or explicit manual handoff stage.",
    "invalid_producer_stage": "Only a pipette or explicit manual handoff stage can produce an intermediate liquid.",
    "intermediate_use_stage_missing": "Set stage_name on the intermediate's robot addition to its exact later pipette stage.",
    "intermediate_used_before_production": "Move the producer stage before the pipette stage consuming its intermediate.",
    "unknown_addition_stage": "Set each robot addition's stage_name to an exact existing pipette-stage name.",
    "missing_deck_source": "Use robot delivery only for a supplied on-deck source or an intermediate already prepared in an earlier stage; otherwise mark and stage an actual manual addition.",
    "final_volume_mismatch": "Recalculate one final vessel or one explicitly named batch, counting each component once.",
    "stock_dilution_mismatch": "Use stock concentration × added volume = target concentration × final reaction volume.",
    "realized_strength_mismatch": "Check both the target dilution and the sum of all proposed additions.",
    "robot_addition_stage_missing": "Give each robot-delivered component an executable pipette stage; a manual comment is not a pipetting stage.",
    "missing_stage_tip_demand": "Add a tip-demand row for each pipette stage, using the stage name verbatim.",
    "unknown_tip_stage": "Make every tip-demand stage string exactly match an existing pipette stage name.",
    "insufficient_tips": "Count pickups across all stages; plan fresh-rack reloads only if explicitly authorized by the task.",
    "pipette_range_mismatch": "Use a provided pipette and tip rack that support each single stroke; split a large total into valid strokes.",
    "module_state_mismatch": "Place the required state-setting stage before the dependent pipetting stage.",
    "source_strength_conflicts_with_inventory": "Keep the stock concentration named in the starting-inventory quote; do not reinterpret a loaded stock as a prepared dilution.",
    "addition_stock_strength_conflicts_with_inventory": "Use the inventory-cited stock concentration for this addition, then recalculate its required volume.",
    "manual_addition_stage_missing": "Name an explicit, ordered manual handoff stage for each manual liquid addition and link it with stage_name.",
    "duplicate_intermediate_preparation": "Replace the earlier batch reaction; never execute an old recipe followed by a corrected recipe in the same vessel.",
    "cited_final_volume_mismatch": "Use the final volume in the reaction's cited source and rebalance every component, including later additions.",
    "cited_addition_volume_mismatch": "Preserve the cited per-vessel addition volume, or cite the source-supported preparation that changes it.",
    "cited_stock_equivalent_mismatch": "Calculate target mass divided by the starting stock concentration; preserve the resulting stock-equivalent dose.",
    "cited_final_concentration_mismatch": "Use C1×V1 = C2×V2 with the cited stock concentration, target concentration, and final vessel volume.",
    "multiple_manual_conditions_in_one_stage": "Split distinct manual temperature/time phases into ordered stages with separate source citations.",
    "cited_manual_duration_mismatch": "Put the cited incubation time in duration_s, converted to seconds.",
    "cited_manual_temperature_mismatch": "Put the cited temperature in temperature_c for that manual phase.",
    "manual_stage_order_unlinked": "Set after_stage to the preceding workflow stage for a timed or temperature-controlled manual phase.",
    "stage_predecessor_not_earlier": "Move the predecessor before this stage, or correct the named dependency.",
}


def _audit_feedback(errors: list[Any]) -> str:
    """Give a bounded, actionable correction request without authoring a protocol."""
    visible = errors[:24]
    findings = "\n".join(
        f"- {issue.code} at {issue.path}: {issue.message}" for issue in visible
    )
    if len(errors) > len(visible):
        findings += f"\n- … and {len(errors) - len(visible)} more check(s)."
    hints = list(dict.fromkeys(
        _REPAIR_HINTS[issue.code] for issue in errors if issue.code in _REPAIR_HINTS
    ))
    guidance = "\nCorrection rules:\n" + "\n".join(f"- {hint}" for hint in hints) if hints else ""
    return "Your plan failed these deterministic checks:\n" + findings + guidance


def _parse_feedback(error: PlanParseError) -> str:
    message = str(error)
    response = "Your plan was not grounded in the supplied source: " + message
    if "initially empty vessel" in message:
        response += (
            "\nThe cited vessel is physically present but contains no starting reagent. "
            "If an earlier ordered pipette or explicit manual handoff stage produces its "
            "contents, set produced_by_stage to that exact stage name. Otherwise omit "
            "it as a liquid source."
        )
    elif "inventory quote does not name its claimed component" in message:
        response += (
            "\nName this source by the actual substance or category identified in its "
            "on-deck inventory quote. For a plate with multiple primers or templates, "
            "cite the matching starting-inventory lines; do not invent a stock."
        )
    return response


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
        {"role": "system", "content": (PLAN_SYSTEM_PROMPT + "\n\n" + _PLANNING_CHECKLIST
                                       + "\n\n" + SOURCE_FIDELITY_GUIDANCE)},
        {"role": "user", "content": user_text},
    ]
    base_config = config or LocalLLMConfig.from_env()
    # Planning is optional. One bounded local HTTP request per plan revision
    # leaves time for the source-grounded code drafter when the model is slow.
    planning_config = replace(base_config, timeout_s=min(base_config.timeout_s, 240),
                              retries=0)
    records: list[dict[str, Any]] = []
    for number in range(1, max_attempts + 1):
        try:
            response = await completion(
                messages, PLAN_SCHEMA, config=planning_config, schema_name="ot2_evidence_plan",
                http_client=http_client,
            )
        except ProtocolLLMError as exc:
            records.append({"number": number, "status": "model_failed",
                            "error": f"{type(exc).__name__}: {exc}"})
            return PlanningResult(None, tuple(records))
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
            feedback = _parse_feedback(exc)
        else:
            issues = (audit_plan(plan, geometry=geometry)
                      + audit_inventory_strength_claims(plan)
                      + audit_manual_addition_stages(plan)
                      + audit_cited_plan_quantities(plan)
                      + audit_parameterized_manual_stages(plan))
            record["issues"] = [issue.__dict__ for issue in issues]
            errors = [issue for issue in issues if issue.severity == "error"]
            if not errors:
                record["status"] = "accepted"
                records.append(record)
                return PlanningResult(plan, tuple(records))
            record["status"] = "audit_failed"
            feedback = _audit_feedback(errors)
        records.append(record)
        if number < max_attempts:
            messages.append({"role": "assistant", "content": serialized})
            messages.append({
                "role": "user",
                "content": feedback + "\nReturn a corrected complete JSON plan with grounded quotes. "
                "Do not add absent deck reagents or silently waive tip or module constraints.",
            })
    return PlanningResult(None, tuple(records))
