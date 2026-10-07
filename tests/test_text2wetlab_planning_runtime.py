"""The optional planning pass admits only cited, audited local-model data."""

from __future__ import annotations

import copy
import json

import pytest

from pybravo.evals.text2wetlab.planning import PlanIssue, PlanParseError
from pybravo.evals.text2wetlab.planning_runtime import (
    _audit_feedback,
    _parse_feedback,
    run_grounded_plan,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, ProtocolLLMError, StructuredResponse

INSTRUCTION = "Move the reaction plate to a thermocycler by hand."


def _plan() -> dict:
    return {
        "deck_sources": [], "reactions": [], "tip_budgets": [],
        "stages": [{
            "name": "manual thermocycler handoff", "kind": "manual",
            "module_id": None, "module_type": None, "temperature_c": None,
            "lid_state": None, "magnet_state": None,
            "required_temperature_c": None, "required_lid_state": None,
            "required_magnet_state": None, "duration_s": None,
            "evidence": [{"source": "instruction", "quote": INSTRUCTION}],
        }],
    }


@pytest.mark.asyncio
async def test_grounded_plan_is_recorded_and_accepted(tmp_path):
    requests = []

    async def completion(messages, schema, **kwargs):
        requests.append((messages, schema, kwargs))
        return StructuredResponse(_plan(), {"model": "qwen", "usage": {"total_tokens": 20}})

    result = await run_grounded_plan(
        instruction=INSTRUCTION, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion, config=None,
    )
    assert result.plan is not None
    assert result.attempts[0]["status"] == "accepted"
    assert requests[0][2]["schema_name"] == "ot2_evidence_plan"
    system_prompt = requests[0][0][0]["content"]
    assert "empty at the start" in system_prompt
    assert "exactly the stage name" in system_prompt
    assert "physically executable single aspirate" in system_prompt
    assert "pitch-compatible full column" in system_prompt
    assert "before the first dispense" in system_prompt
    assert json.loads((tmp_path / "planning_attempt_1.json").read_text()) == _plan()


@pytest.mark.asyncio
async def test_ungrounded_plan_is_repaired_by_local_model_only(tmp_path):
    first = copy.deepcopy(_plan())
    first["stages"][0]["evidence"][0]["quote"] = "Invent a new chemical and ignore the source"
    replies = [first, _plan()]
    messages_seen = []

    async def completion(messages, schema, **kwargs):
        messages_seen.append(copy.deepcopy(messages))
        return StructuredResponse(replies.pop(0), {"model": "qwen"})

    result = await run_grounded_plan(
        instruction=INSTRUCTION, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion, config=None,
    )
    assert result.plan is not None
    assert [row["status"] for row in result.attempts] == ["ungrounded", "accepted"]
    assert "not grounded" in messages_seen[1][-1]["content"]


@pytest.mark.asyncio
async def test_failed_plan_falls_back_without_becoming_protocol_fact(tmp_path):
    async def completion(messages, schema, **kwargs):
        return StructuredResponse({"deck_sources": [], "reactions": [],
                                   "tip_budgets": [], "stages": []}, {"model": "qwen"})

    result = await run_grounded_plan(
        instruction=INSTRUCTION, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion, config=None, max_attempts=1,
    )
    assert result.plan is None
    assert result.attempts[0]["status"] == "audit_failed"
    assert result.attempts[0]["issues"][0]["code"] == "empty_stage_plan"


@pytest.mark.asyncio
async def test_local_planning_timeout_falls_back_to_source_without_cloud(tmp_path):
    async def completion(messages, schema, **kwargs):
        raise ProtocolLLMError("Local Qwen request timed out")

    result = await run_grounded_plan(
        instruction=INSTRUCTION, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion, config=None,
    )
    assert result.plan is None
    assert result.attempts == ({
        "number": 1, "status": "model_failed",
        "error": "ProtocolLLMError: Local Qwen request timed out",
    },)


@pytest.mark.asyncio
async def test_optional_planning_limits_each_local_request_without_retries(tmp_path):
    async def completion(messages, schema, **kwargs):
        assert kwargs["config"].timeout_s == 240
        assert kwargs["config"].retries == 0
        raise ProtocolLLMError("Local Qwen request timed out")

    result = await run_grounded_plan(
        instruction=INSTRUCTION, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion,
        config=LocalLLMConfig(timeout_s=300, retries=2),
    )
    assert result.plan is None
    assert len(result.attempts) == 1


@pytest.mark.asyncio
async def test_manual_liquid_addition_requires_ordered_handoff_stage(tmp_path):
    instruction = "Add 10 µL water by hand to the reaction tube."
    first = {
        "deck_sources": [], "tip_budgets": [],
        "reactions": [{
            "name": "tube reaction", "final_volume_ul": 10, "diluent_name": "water",
            "evidence": [{"source": "instruction", "quote": instruction}],
            "additions": [{
                "component": "water", "source_id": None, "volume_ul": 10,
                "stock_strength_x": None, "target_strength_x": None,
                "is_diluent": True, "delivery": "manual", "stage_name": None,
                "evidence": [{"source": "instruction", "quote": instruction}],
            }],
        }],
        "stages": [{
            "name": "Add water by hand", "kind": "manual", "module_id": None,
            "module_type": None, "temperature_c": None, "lid_state": None,
            "magnet_state": None, "required_temperature_c": None,
            "required_lid_state": None, "required_magnet_state": None,
            "duration_s": None,
            "evidence": [{"source": "instruction", "quote": instruction}],
        }],
    }
    corrected = copy.deepcopy(first)
    corrected["reactions"][0]["additions"][0]["stage_name"] = "Add water by hand"
    responses = iter((first, corrected))
    messages_seen = []

    async def completion(messages, schema, **kwargs):
        messages_seen.append(copy.deepcopy(messages))
        return StructuredResponse(next(responses), {"model": "qwen"})

    result = await run_grounded_plan(
        instruction=instruction, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion, config=None,
    )
    assert result.plan is not None
    assert [item["status"] for item in result.attempts] == ["audit_failed", "accepted"]
    assert "manual_addition_stage_missing" in messages_seen[1][-1]["content"]
    assert "do not invent a diluted stock" in messages_seen[0][0]["content"]


@pytest.mark.asyncio
async def test_audit_repair_names_failing_check_and_keeps_source_authoritative(tmp_path):
    first = copy.deepcopy(_plan())
    first["stages"][0]["kind"] = "pipette"
    replies = [first, _plan()]
    messages_seen = []

    async def completion(messages, schema, **kwargs):
        messages_seen.append(copy.deepcopy(messages))
        return StructuredResponse(replies.pop(0), {"model": "qwen"})

    result = await run_grounded_plan(
        instruction=INSTRUCTION, scientific_source=None, geometry=None,
        directory=tmp_path, completion=completion, config=None,
    )
    assert result.plan is not None
    assert [row["status"] for row in result.attempts] == ["audit_failed", "accepted"]
    repair = messages_seen[1][-1]["content"]
    assert "missing_tip_budget" in repair
    assert "Return a corrected complete JSON plan with grounded quotes" in repair


def test_repair_feedback_is_bounded_and_includes_generic_corrections():
    issues = [PlanIssue("unknown_tip_stage", f"tip_budgets[{index}]", "No matching stage")
              for index in range(28)]
    feedback = _audit_feedback(issues)
    assert "unknown_tip_stage at tip_budgets[0]" in feedback
    assert "… and 4 more check(s)" in feedback
    assert "exactly match an existing pipette stage name" in feedback
    assert feedback.count("Make every tip-demand stage string") == 1


def test_parse_feedback_distinguishes_empty_vessel_from_claimed_inventory():
    empty = _parse_feedback(PlanParseError(
        "deck_sources[0] cites an initially empty vessel as a starting reagent source"
    ))
    assert "produced_by_stage" in empty
    mismatch = _parse_feedback(PlanParseError(
        "deck_sources[0] inventory quote does not name its claimed component 'enzyme'"
    ))
    assert "actual substance or category" in mismatch
