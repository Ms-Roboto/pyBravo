"""Bounded Designer repair is local only and never calls a live model here."""

from __future__ import annotations

import pytest

from pybravo.workflow.drafter.llm import DrafterConfig, DraftResult
from pybravo.workflow.drafter.schema import DraftedWorkflow
from pybravo.workflow.drafter.scientific_patterns import StageRequirement
from pybravo.workflow.drafter.scientific_repair import (
    repair_scientific_workflow,
    validate_scientific_repair_candidate,
)

SOURCE = "## The protocol to implement\n1. Transfer 10 µL from source to destination.\n"
CONFIG = DrafterConfig(provider="local", model="qwen", max_repair_attempts=2)


def _draft(*types: str, name: str = "Model draft") -> DraftedWorkflow:
    nodes = []
    for index, kind in enumerate(types, start=1):
        props: dict = {}
        if kind == "tips/TipsOn" or kind == "tips/TipsOff":
            props = {"location": 3}
        elif kind == "liquid/Aspirate":
            props = {"location": 1, "anchor": "A1", "volume": 10, "liquid_class": "Aqueous"}
        elif kind == "liquid/Dispense":
            props = {"location": 2, "anchor": "A1", "volume": 10, "liquid_class": "Aqueous"}
        elif kind == "system/Manual":
            props = {"message": "Operator reviews this setup."}
        elif kind == "logic/Script":
            props = {"script": "pass"}
        nodes.append({"id": index, "type": kind, "properties": props})
    links = [{"id": index, "origin_id": index, "origin_slot": 0,
              "target_id": index + 1, "target_slot": 0, "link_type": -1}
             for index in range(1, len(nodes))]
    return DraftedWorkflow.model_validate({"name": name, "graph": {"nodes": nodes, "links": links}})


def _response(workflow: DraftedWorkflow, *, provider: str = "local", model: str = "qwen") -> DraftResult:
    return DraftResult(workflow=workflow, issues=[], attempts=1, provider=provider, model=model)


async def test_already_valid_graph_uses_no_model_call():
    complete = _draft("flow/Start", "tips/TipsOn", "liquid/Aspirate",
                      "liquid/Dispense", "tips/TipsOff", "flow/End").to_designer_json()

    async def fake_drafter(prompt, **kwargs):
        raise AssertionError("No repair is needed")

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="", current_workflow=complete,
        validator_issues=[], config=CONFIG, drafter=fake_drafter,
    )
    assert result.attempts == 0
    assert result.passed_checks is True
    assert result.candidate_workflow is None
    assert result.workflow == complete


async def test_repairs_second_candidate_and_preserves_original_provenance():
    incomplete = _draft("flow/Start", "flow/End")
    complete = _draft("flow/Start", "tips/TipsOn", "liquid/Aspirate",
                      "liquid/Dispense", "tips/TipsOff", "flow/End", name="Complete")
    original = incomplete.to_designer_json()
    original["protocol_generated_provenance"] = {"source_id": "pinned-task", "model": "qwen"}
    calls = []

    async def fake_drafter(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return _response(incomplete if len(calls) == 1 else complete)

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="One source well feeds the destination.",
        current_workflow=original, validator_issues=[{
            "severity": "error", "code": "MISSING_TRANSFER",
            "message": "The transfer is absent.", "path": "/graph",
        }], config=CONFIG, max_attempts=2, drafter=fake_drafter,
    )
    assert result.passed_checks is True
    assert result.attempts == 2
    assert result.workflow["name"] == "Complete"
    assert result.workflow["protocol_generated_provenance"] == original["protocol_generated_provenance"]
    assert result.provenance == original["protocol_generated_provenance"]
    assert original["name"] == "Model draft"
    assert "MISSING_TRANSFER" in calls[0][0]
    assert "One source well feeds" in calls[0][0]
    assert "SOURCE_STAGE_UNACCOUNTED" in calls[1][0]
    assert all(call[1]["config"].max_repair_attempts == 0 for call in calls)
    assert all(call[1]["config"].provider == "local" for call in calls)
    assert all(call[1]["include_exemplars"] is False for call in calls)


async def test_exhausted_repairs_leave_original_untouched_and_expose_candidate():
    original = _draft("flow/Start", "flow/End").to_designer_json()
    still_incomplete = _draft("flow/Start", "flow/End", name="Still incomplete")
    calls = []

    async def fake_drafter(prompt, **kwargs):
        calls.append(prompt)
        return _response(still_incomplete)

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="", current_workflow=original,
        validator_issues=[], config=CONFIG, max_attempts=2, drafter=fake_drafter,
    )
    assert len(calls) == 2
    assert result.attempts == 2
    assert result.passed_checks is False
    assert result.workflow == original
    assert result.candidate_workflow["name"] == "Still incomplete"
    assert {issue["code"] for issue in result.issues} == {"SOURCE_STAGE_UNACCOUNTED"}


async def test_cloud_or_other_model_config_is_rejected_before_any_call():
    original = _draft("flow/Start", "flow/End").to_designer_json()
    calls = []

    async def fake_drafter(prompt, **kwargs):
        calls.append(prompt)
        raise AssertionError("must not call")

    for cfg in (DrafterConfig(provider="openai", model="qwen"),
                DrafterConfig(provider="local", model="other-model"),
                DrafterConfig(provider="local", model="not-qwen")):
        with pytest.raises(ValueError, match="local Qwen"):
            await repair_scientific_workflow(
                instruction=SOURCE, source_excerpt="", current_workflow=original,
                validator_issues=[], config=cfg, drafter=fake_drafter,
            )
    assert calls == []


async def test_call_budget_is_bounded_even_when_model_raises():
    original = _draft("flow/Start", "flow/End").to_designer_json()
    calls = []

    async def fake_drafter(prompt, **kwargs):
        calls.append(prompt)
        raise RuntimeError("offline")

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="", current_workflow=original,
        validator_issues=[], config=CONFIG, max_attempts=2, drafter=fake_drafter,
    )
    assert len(calls) == 2
    assert result.workflow == original
    assert result.candidate_workflow is None
    assert result.issues[0]["code"] == "REPAIR_CALL_FAILED"
    with pytest.raises(ValueError, match="max_attempts"):
        await repair_scientific_workflow(
            instruction=SOURCE, source_excerpt="", current_workflow=original,
            validator_issues=[], config=CONFIG, max_attempts=6, drafter=fake_drafter,
        )
    assert len(calls) == 2


def test_schema_graph_cycle_and_script_are_rejected():
    scripted = _draft("flow/Start", "logic/Script", "flow/End").to_designer_json()
    scripted["graph"]["links"].append([3, 2, 0, 1, 0, -1])
    codes = {issue["code"] for issue in validate_scientific_repair_candidate(
        scripted, instruction="Inspect the plate.")}
    assert {"UNSUPPORTED_NODE", "GRAPH_CYCLE"} <= codes
    scripted["graph"]["nodes"][1]["type"] = "made/up"
    assert {issue["code"] for issue in validate_scientific_repair_candidate(
        scripted, instruction="Inspect the plate.")} == {"WORKFLOW_SCHEMA_INVALID"}


async def test_extra_validator_must_pass_before_repair_is_accepted():
    original = _draft("flow/Start", "flow/End").to_designer_json()
    complete = _draft("flow/Start", "tips/TipsOn", "liquid/Aspirate",
                      "liquid/Dispense", "tips/TipsOff", "flow/End")
    calls = []

    async def fake_drafter(prompt, **kwargs):
        calls.append(prompt)
        return _response(complete)

    def still_unresolved(workflow):
        return [{"severity": "error", "code": "CATALOG_REVIEW", "message": "Catalog entry unresolved.", "path": "/deck"}]

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="", current_workflow=original,
        validator_issues=[], config=CONFIG, max_attempts=2,
        extra_validator=still_unresolved, drafter=fake_drafter,
    )
    assert len(calls) == 2
    assert result.passed_checks is False
    assert result.workflow == original
    assert "CATALOG_REVIEW" in calls[1]


async def test_prior_hardware_error_cannot_be_declared_fixed_without_recheck():
    original = _draft("flow/Start", "flow/End").to_designer_json()
    complete = _draft("flow/Start", "tips/TipsOn", "liquid/Aspirate",
                      "liquid/Dispense", "tips/TipsOff", "flow/End")

    async def fake_drafter(prompt, **kwargs):
        return _response(complete)

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="", current_workflow=original,
        validator_issues=[{"severity": "error", "code": "UNAVAILABLE_LIQUID_CLASS",
                           "message": "Class is absent from catalog.", "path": "/graph/nodes/2"}],
        config=CONFIG, max_attempts=1, drafter=fake_drafter,
    )
    assert result.passed_checks is False
    assert result.workflow == original
    assert result.candidate_workflow is not None
    assert "EXTERNAL_REVALIDATION_REQUIRED" in {issue["code"] for issue in result.issues}


async def test_reviewed_stage_contract_is_rechecked_after_model_response():
    original = _draft("flow/Start", "flow/End").to_designer_json()
    transfer_only = _draft("flow/Start", "tips/TipsOn", "liquid/Aspirate",
                           "liquid/Dispense", "tips/TipsOff", "flow/End")

    async def fake_drafter(prompt, **kwargs):
        return _response(transfer_only)

    result = await repair_scientific_workflow(
        instruction=SOURCE, source_excerpt="Thermocycling follows setup.",
        current_workflow=original, validator_issues=[], config=CONFIG,
        expected_stages=[StageRequirement("transfer", "Assemble reaction", volume_ul=10),
                         StageRequirement("manual", "Thermocycle reaction", marker="thermocycl")],
        max_attempts=1, drafter=fake_drafter,
    )
    assert result.passed_checks is False
    assert "SOURCE_STAGE_UNACCOUNTED" in {issue["code"] for issue in result.issues}
