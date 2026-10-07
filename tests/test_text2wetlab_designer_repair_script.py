"""The saved-draft repair command stays pinned, local, and non-executable."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import repair_text2wetlab_designer as script  # noqa: E402

from pybravo.workflow.drafter.llm import DraftResult  # noqa: E402
from pybravo.workflow.drafter.schema import DraftedWorkflow  # noqa: E402
from pybravo.workflow.storage import WorkflowStorage  # noqa: E402

TASK = "a1-a12-100ul"
INSTRUCTION = b"## The protocol to implement\n1. Transfer 10 uL from source to destination.\n"


def _graph(*types: str) -> dict:
    nodes = []
    for index, kind in enumerate(types, 1):
        props = {}
        if kind in {"tips/TipsOn", "tips/TipsOff"}:
            props = {"location": 3}
        elif kind == "liquid/Aspirate":
            props = {"location": 1, "anchor": "A1", "volume": 10, "liquid_class": "Aqueous"}
        elif kind == "liquid/Dispense":
            props = {"location": 2, "anchor": "A1", "volume": 10, "liquid_class": "Aqueous"}
        nodes.append({"id": index, "type": kind, "properties": props})
    links = [{"id": index, "origin_id": index, "origin_slot": 0,
              "target_id": index + 1, "target_slot": 0, "link_type": -1}
             for index in range(1, len(nodes))]
    return DraftedWorkflow.model_validate({
        "name": "Text2WetLab · Qwen draft",
        "graph": {"nodes": nodes, "links": links},
    }).to_designer_json()


def _saved() -> dict:
    draft = _graph("flow/Start", "flow/End")
    draft.update({
        "id": "original-workflow", "protocol_generated_draft": True,
        "protocol_generated_root_id": "original-workflow",
        "protocol_generated_provenance": {
            "source_kind": "text2wetlab_pinned_task", "source_id": TASK,
            "dataset_revision": script.REVISION,
            "source_sha256": hashlib.sha256(INSTRUCTION).hexdigest(),
            "model": "qwen", "model_url": script.MODEL_URL,
        },
    })
    return draft


def _source(task, name, dataset_root):
    assert task == TASK
    return INSTRUCTION if name == "instruction.md" else None


CONTEXT = {
    "head_type": "HT_96_D_200",
    "labware": [{"id": "rack-1", "name": "Compatible tip box", "base_class": "tip_box"}],
    "liquid_classes": [{"name": "Aqueous", "tip_id": "st_10ul", "tip_capacity_ul": 10}],
    "tipbox_choices": [{"labware_id": "rack-1", "tip_definition_id": "st_10ul",
                        "execution_ready": True}],
}


async def test_default_audit_does_not_call_qwen_or_save(monkeypatch, tmp_path):
    monkeypatch.setattr(script, "_source_bytes", _source)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No model call in audit mode")

    monkeypatch.setattr(script, "_post_draft", forbidden)
    record = await script.repair_saved_draft(
        _saved(), context=CONTEXT, output_dir=tmp_path,
        api_url="http://test", execute=False, drafter=forbidden,
    )
    assert record["status"] == "audit_only"
    assert record["initial_error_count"] > 0
    assert not list(tmp_path.iterdir())


async def test_passing_qwen_repair_creates_new_unreviewed_draft_with_source_lineage(monkeypatch, tmp_path):
    monkeypatch.setattr(script, "_source_bytes", _source)
    complete = DraftedWorkflow.model_validate({
        "name": "Repaired by local model",
        "deck": {"3": [{"labware_id": "rack-1", "name": "Compatible tip box",
                         "base_class": "tip_box", "wells": 96}]},
        "graph": {
            "nodes": [
                {"id": 1, "type": "flow/Start"},
                {"id": 2, "type": "tips/TipsOn", "properties": {"location": 3}},
                {"id": 3, "type": "liquid/Aspirate", "properties": {
                    "location": 1, "anchor": "A1", "volume": 10, "liquid_class": "Aqueous"}},
                {"id": 4, "type": "liquid/Dispense", "properties": {
                    "location": 2, "anchor": "A1", "volume": 10, "liquid_class": "Aqueous"}},
                {"id": 5, "type": "tips/TipsOff", "properties": {"location": 3}},
                {"id": 6, "type": "flow/End"},
            ],
            "links": [{"id": i, "origin_id": i, "origin_slot": 0,
                       "target_id": i + 1, "target_slot": 0, "link_type": -1}
                      for i in range(1, 6)],
        },
    })
    calls = []

    async def fake_qwen(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return DraftResult(workflow=complete, issues=[], attempts=1,
                           provider="local", model="qwen")

    posted = []

    def fake_post(api_url, workflow, provenance, issues):
        posted.append((workflow, provenance, issues))
        return {"workflow_id": "repaired-workflow", "url": "/designer?workflow=repaired-workflow"}

    monkeypatch.setattr(script, "_post_draft", fake_post)
    record = await script.repair_saved_draft(
        _saved(), context=CONTEXT, output_dir=tmp_path,
        api_url="http://test", execute=True, max_attempts=2, drafter=fake_qwen,
    )
    assert record["status"] == "saved_unreviewed"
    assert record["workflow_id"] == "repaired-workflow"
    assert len(calls) == 1
    assert calls[0][1]["config"].provider == "local"
    assert calls[0][1]["config"].model == "qwen"
    assert "ACTIVE BRAVO PROFILE OPTIONS" in calls[0][0]
    assert "Aqueous" in calls[0][0]
    assert "id" not in posted[0][0]
    assert "protocol_generated_draft" not in posted[0][0]
    assert posted[0][1]["source_sha256"] == _saved()["protocol_generated_provenance"]["source_sha256"]
    assert posted[0][1]["generation_trace_sha256"] == record["generation_trace_sha256"]
    assert (tmp_path / TASK / "repair_candidate.json").is_file()
    assert record["candidate_error_count"] == 0


async def test_incomplete_candidate_is_not_saved_without_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(script, "_source_bytes", _source)
    incomplete = DraftedWorkflow.model_validate({
        "name": "Still incomplete",
        "graph": {"nodes": [{"id": 1, "type": "flow/Start"},
                             {"id": 2, "type": "system/Manual", "properties": {"message": "Review transfer"}},
                             {"id": 3, "type": "flow/End"}],
                  "links": [{"id": 1, "origin_id": 1, "origin_slot": 0,
                             "target_id": 2, "target_slot": 0, "link_type": -1},
                            {"id": 2, "origin_id": 2, "origin_slot": 0,
                             "target_id": 3, "target_slot": 0, "link_type": -1}]},
    })

    async def fake_qwen(prompt, **kwargs):
        return DraftResult(workflow=incomplete, issues=[], attempts=1,
                           provider="local", model="qwen")

    def forbidden(*args, **kwargs):
        raise AssertionError("An incomplete candidate must not be posted")

    monkeypatch.setattr(script, "_post_draft", forbidden)
    result = await script.repair_saved_draft(
        _saved(), context=CONTEXT, output_dir=tmp_path, api_url="http://test",
        execute=True, max_attempts=1, drafter=fake_qwen,
    )
    assert result["status"] == "repair_failed"
    assert result["passed_checks"] is False
    assert (tmp_path / TASK / "repair_candidate.json").is_file()


def test_selection_chooses_latest_but_exact_id_is_unambiguous(tmp_path):
    storage = WorkflowStorage(tmp_path)
    old = _saved()
    old["id"] = "old"
    old["modified"] = "2026-01-01"
    storage.create_workflow(old)
    current = _saved()
    current["id"] = "new"
    storage.create_workflow(current)
    storage.update_workflow("new", {"modified": "2026-02-01"})
    chosen = script._select_workflows(storage, tasks=[TASK], workflow_id=None)
    assert len(chosen) == 1
    assert chosen[0]["id"] == "new"
    assert script._select_workflows(storage, tasks=None, workflow_id="old")[0]["id"] == "old"
    with pytest.raises(ValueError, match="not found"):
        script._select_workflows(storage, tasks=None, workflow_id="unknown")


def test_unknown_catalog_deck_and_ot2_remap_get_distinct_severity():
    workflow = _graph("flow/Start", "flow/End")
    workflow["deck"] = {"1": [{"labware_id": "invented"}]}
    issues = script._catalog_issues(workflow, CONTEXT, "OT-2 procedure")
    by_code = {item["code"]: item for item in issues}
    assert by_code["UNRESOLVED_LABWARE"]["severity"] == "error"
    assert by_code["OT2_DECK_REMAP_REVIEW"]["severity"] == "warning"


def test_catalog_gate_rejects_oversize_stroke_and_unready_tip_pairing():
    workflow = _graph("flow/Start", "tips/TipsOn", "liquid/Aspirate",
                      "liquid/Dispense", "tips/TipsOff", "flow/End")
    workflow["deck"] = {"3": [{"labware_id": "rack-1", "base_class": "tip_box"}]}
    workflow["graph"]["nodes"][2]["properties"]["volume"] = 20
    context = {**CONTEXT, "tipbox_choices": [
        {"labware_id": "rack-1", "tip_definition_id": "st_10ul", "execution_ready": False},
    ]}
    codes = {issue["code"] for issue in script._catalog_issues(workflow, context, "Transfer 20 uL.")}
    assert "LIQUID_CLASS_VOLUME_OUT_OF_RANGE" in codes
    assert "LIQUID_CLASS_TIP_NOT_READY" in codes


def test_pinned_source_digest_failure_prevents_repair(monkeypatch):
    monkeypatch.setattr(script, "_source_bytes", lambda *args: b"different source")
    with pytest.raises(ValueError, match="digest differs"):
        script._pinned_source(TASK, _saved()["protocol_generated_provenance"],
                              dataset_root=None, ecoli_paper=None)
