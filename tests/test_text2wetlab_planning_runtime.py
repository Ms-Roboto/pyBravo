"""The optional planning pass admits only cited, audited local-model data."""

from __future__ import annotations

import copy
import json

import pytest

from pybravo.evals.text2wetlab.planning_runtime import run_grounded_plan
from pybravo.workflow.protocols.llm import StructuredResponse

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
