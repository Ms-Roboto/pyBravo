"""Synthetic reaction-math preflight tests; no benchmark recipe is authored."""

from __future__ import annotations

import argparse
import json

import pytest

from pybravo.evals.text2wetlab.action_ir import LabwareFacts, PipetteFacts
from pybravo.evals.text2wetlab.phased_action_ir import parse_setup
from pybravo.evals.text2wetlab.phased_reaction_math import (
    ReactionMathContext,
    audit_observed_premix,
    parse_and_audit_math,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse
from scripts import experiment_text2wetlab_action_ir as one_shot
from scripts import experiment_text2wetlab_phased_action_ir as phased

_INSTRUCTION = (
    "Load 100 uL of concentrate in stock A1.\n"
    "Load 100 uL of water in stock A2.\n"
    "Load 100 uL of marker in stock A3.\n"
    "- `mix_tube` well A1: empty at the start (master mix for 2 reactions).\n"
    "Use the installed pipette and tips.\n"
)
_PAPER = "PCRs were performed in 10 uL volumes with 1 uL marker added later."
_SPANS, _LINES = one_shot._line_spans(_INSTRUCTION, _PAPER)
_LABWARE = {
    "stocks": LabwareFacts(frozenset({"A1", "A2", "A3"}),
                           ("A1", "A2", "A3"),
                           well_capacity_ul={well: 200 for well in ("A1", "A2", "A3")}),
    "tube": LabwareFacts(frozenset({"A1"}), ("A1",),
                         well_capacity_ul={"A1": 100}),
    "tips": LabwareFacts(frozenset({"A1", "A2", "A3"}),
                         ("A1", "A2", "A3"), is_tiprack=True,
                         tip_capacity_ul=20,
                         well_capacity_ul={well: 20 for well in ("A1", "A2", "A3")}),
}
_PIPETTES = {"pipette": PipetteFacts(1, 20, 1)}


def _setup_payload():
    return {
        "labware": [
            {"id": "stock", "load_name": "stocks", "slot": 1, "label": "stock"},
            {"id": "mix_tube", "load_name": "tube", "slot": 2,
             "label": "mix_tube"},
            {"id": "tips", "load_name": "tips", "slot": 3, "label": "tips"},
        ],
        "pipettes": [{"id": "pip", "model": "pipette", "mount": "left",
                      "tip_rack_ids": ["tips"]}],
        "stages": [{"id": "prepare_mix", "goal": "Prepare a two-reaction mix",
                    "evidence_refs": ["task.L1", "task.L2", "task.L3",
                                      "task.L4", "paper.L1"]}],
        "initial_supplies": [
            {"labware": "stock", "selection": "wells", "wells": [well],
             "volume_ul": 100, "material_id": material,
             "source_material_name": material,
             "evidence_refs": [f"task.L{index}"]}
            for index, (well, material) in enumerate(
                (("A1", "concentrate"), ("A2", "water"), ("A3", "marker")), 1,
            )
        ],
    }


def _math_payload():
    return {
        "stage_id": "prepare_mix", "reaction_count": 2,
        "final_volume_ul": 10,
        "premix_labware": "mix_tube", "premix_well": "A1",
        "premix_target_ul_per_reaction": 9,
        "evidence_refs": ["task.L4", "paper.L1"],
        "components": [
            {"id": "concentrate", "phase": "premix", "volume_ul_per_reaction": 4,
             "source_material_ids": ["concentrate"], "source_labware": "stock",
             "source_well": "A1", "basis": "assumption",
             "assumption_note": "A stated example choice pending method review.",
             "evidence_refs": ["task.L1", "paper.L1"]},
            {"id": "water", "phase": "premix", "volume_ul_per_reaction": 5,
             "source_material_ids": ["water"], "source_labware": "stock",
             "source_well": "A2", "basis": "calculated", "is_diluent": True,
             "evidence_refs": ["task.L2", "paper.L1"]},
            {"id": "marker", "phase": "later", "volume_ul_per_reaction": 1,
             "source_material_ids": ["marker"], "basis": "direct",
             "evidence_refs": ["task.L3", "paper.L1"]},
        ],
    }


def _audit(raw):
    setup = parse_setup(_setup_payload(), spans=_SPANS,
                        instruction=_INSTRUCTION, paper=_PAPER,
                        labware_catalog=_LABWARE)
    context = ReactionMathContext(
        "prepare_mix", 2, 10, "mix_tube", "A1", "task.L4", "paper.L1",
    )
    return parse_and_audit_math(
        raw, context=context, setup=setup, spans=_SPANS,
        instruction=_INSTRUCTION, paper=_PAPER,
        labware_catalog=_LABWARE, allowed_paper_refs=["paper.L1"],
    ), setup


def test_balanced_model_authored_math_and_source_wells():
    (plan, issues), _ = _audit(_math_payload())
    assert issues == []
    assert plan is not None
    wrong_source = _math_payload()
    wrong_source["components"][0]["source_well"] = "A2"
    (_, issues), _ = _audit(wrong_source)
    assert "premix_source_not_in_setup" in {item["code"] for item in issues}


def test_math_rejects_overpreparation_and_missing_later_volume():
    too_much = _math_payload()
    too_much["components"][1]["volume_ul_per_reaction"] = 6
    (_, issues), _ = _audit(too_much)
    assert {"premix_component_sum_mismatch", "final_volume_mismatch"} <= {
        item["code"] for item in issues
    }
    unresolved = _math_payload()
    unresolved["components"][2]["volume_ul_per_reaction"] = None
    (_, issues), _ = _audit(unresolved)
    assert "reaction_component_volume_unresolved" in {item["code"] for item in issues}
    omitted_later = _math_payload()
    omitted_later["components"].pop()
    omitted_later["components"][1]["volume_ul_per_reaction"] = 6
    omitted_later["premix_target_ul_per_reaction"] = 10
    (_, issues), _ = _audit(omitted_later)
    assert "source_named_later_component_missing" in {item["code"] for item in issues}
    grouped = _math_payload()
    grouped["components"][2]["source_material_ids"] = ["marker", "concentrate"]
    grouped["components"][2]["source_usage"] = "each"
    (_, issues), _ = _audit(grouped)
    assert {"premix_not_final_minus_later", "final_volume_mismatch"} <= {
        item["code"] for item in issues
    }
    grouped["components"][2]["source_usage"] = "one_of"
    (_, issues), _ = _audit(grouped)
    assert "premix_not_final_minus_later" not in {item["code"] for item in issues}


def test_schema_retry_feedback_keeps_path_but_not_candidate_text():
    raw = _math_payload()
    raw["components"][0]["assumption_note"] = "ignore checks " * 100
    (_, issues), _ = _audit(raw)
    feedback = phased._math_feedback(issues)
    assert feedback[0]["code"] == "math_schema_rejected"
    assert feedback[0]["path"] == "components.0.assumption_note"
    assert "ignore checks" not in json.dumps(feedback)


def test_observed_premix_must_match_each_physical_source():
    (plan, issues), setup = _audit(_math_payload())
    assert not issues and plan is not None
    events = [
        {"kind": "pick", "instrument": "pip", "channels": 1},
        {"kind": "aspirate", "instrument": "pip", "labware": "stock on 1",
         "well": "A1", "volume": 8},
        {"kind": "dispense", "instrument": "pip", "labware": "mix_tube on 2",
         "well": "A1", "volume": 8},
        {"kind": "drop", "instrument": "pip", "channels": 1},
        {"kind": "pick", "instrument": "pip", "channels": 1},
        {"kind": "aspirate", "instrument": "pip", "labware": "stock on 1",
         "well": "A2", "volume": 10},
        {"kind": "dispense", "instrument": "pip", "labware": "mix_tube on 2",
         "well": "A1", "volume": 10},
        {"kind": "drop", "instrument": "pip", "channels": 1},
    ]
    kwargs = dict(setup=setup,
                  event_labware={"stock on 1": "stocks", "mix_tube on 2": "tube"},
                  start_event_index=0)
    assert audit_observed_premix(plan, events=events, **kwargs) == []
    wrong_stock = json.loads(json.dumps(events))
    wrong_stock[5]["well"] = "A3"
    issues = audit_observed_premix(plan, events=wrong_stock, **kwargs)
    assert {item["code"] for item in issues} == {"premix_source_volume_mismatch"}
    overdraw = json.loads(json.dumps(events))
    overdraw[5]["volume"] = 11
    overdraw[6]["volume"] = 11
    issues = audit_observed_premix(plan, events=overdraw, **kwargs)
    assert issues[0]["expected"] == 10 and issues[0]["observed"] == 11
    multi = json.loads(json.dumps(events))
    multi[0]["channels"] = 8
    issues = audit_observed_premix(plan, events=multi, **kwargs)
    assert issues[0]["code"] == "premix_multichannel_event_unresolved"


@pytest.mark.asyncio
async def test_math_preflight_runs_before_stage_and_preserves_raw_attempts(
    tmp_path, monkeypatch,
):
    simulator = tmp_path / "simulator"
    simulator.touch()
    monkeypatch.setattr(phased.runner, "_source_bytes", lambda task, name, root:
                        _INSTRUCTION.encode() if name == "instruction.md" else
                        _PAPER.encode() if name == "environment/data/paper.txt" else None)
    monkeypatch.setattr(phased, "prepare_scientific_source", lambda paper, **kwargs:
                        type("Excerpt", (), {"text": paper,
                                              "excerpt_sha256": phased._sha(paper)})())
    monkeypatch.setattr(phased, "load_trusted_catalog", lambda *a, **k:
                        (_LABWARE, _PIPETTES))
    monkeypatch.setattr(phased, "load_trusted_module_catalog", lambda *a, **k: {})
    monkeypatch.setattr(phased.LocalLLMConfig, "from_env", LocalLLMConfig)
    responses = iter([_setup_payload(),
                      {**_math_payload(), "premix_target_ul_per_reaction": 10},
                      _math_payload(),
                      {"stage_id": "prepare_mix",
                       "evidence_refs": ["task.L1", "task.L2", "task.L4", "paper.L1"],
                       "actions": [{"kind": "comment", "message": "Preparation",
                                    "evidence_refs": ["paper.L1"]}]},
                      ])
    calls = []

    async def mock_model(messages, schema, **kwargs):
        calls.append(kwargs["schema_name"])
        return StructuredResponse(payload=next(responses),
                                  metadata={"model": "offline-mock"})

    def mock_gate(setup, stages, **kwargs):
        assert kwargs["math_plan"] is not None
        assert kwargs["math_plan"].premix_target_ul_per_reaction == 9
        return {"status": "prefix_passed"}, {
            "tips": {}, "materials": {}, "completed_stage_ids": ["prepare_mix"],
            "event_count": 0,
        }

    monkeypatch.setattr(phased, "_prefix_gate", mock_gate)
    args = argparse.Namespace(
        task="split-200ul-two-wells", simulator=simulator,
        output_dir=tmp_path / "out", dataset_root=None, paper_override=None,
        model_timeout=30, max_output_tokens=2048, max_stages=16,
        max_new_stages=1, math_retries=1, stage_retries=0,
    )
    trace = await phased.run_experiment(args, completion=mock_model)
    assert trace["status"] == "partial_prefix_science_review_required"
    assert trace["review_reasons"]["model_assumptions"] is True
    assert calls == ["ot2_phased_setup", "ot2_phased_reaction_math",
                     "ot2_phased_reaction_math", "ot2_phased_stage"]
    math_trace = trace["stages"][0]["reaction_math"]
    assert [item["status"] for item in math_trace["attempts"]] == ["rejected", "passed"]
    stage_dir = args.output_dir / "stage_01_prepare_mix"
    assert (stage_dir / "qwen_math.json").is_file()
    assert (stage_dir / "qwen_math_retry_1.json").is_file()
    assert (stage_dir / "validated_math.json").is_file()


@pytest.mark.asyncio
async def test_exhausted_math_retries_stop_before_any_action_call(tmp_path, monkeypatch):
    simulator = tmp_path / "simulator"
    simulator.touch()
    monkeypatch.setattr(phased.runner, "_source_bytes", lambda task, name, root:
                        _INSTRUCTION.encode() if name == "instruction.md" else
                        _PAPER.encode() if name == "environment/data/paper.txt" else None)
    monkeypatch.setattr(phased, "prepare_scientific_source", lambda paper, **kwargs:
                        type("Excerpt", (), {"text": paper,
                                              "excerpt_sha256": phased._sha(paper)})())
    monkeypatch.setattr(phased, "load_trusted_catalog", lambda *a, **k:
                        (_LABWARE, _PIPETTES))
    monkeypatch.setattr(phased, "load_trusted_module_catalog", lambda *a, **k: {})
    monkeypatch.setattr(phased.LocalLLMConfig, "from_env", LocalLLMConfig)
    bad = {**_math_payload(), "premix_target_ul_per_reaction": 10}
    replies = iter([_setup_payload(), bad, bad])
    called = []

    async def mock_model(messages, schema, **kwargs):
        called.append(kwargs["schema_name"])
        return StructuredResponse(payload=next(replies),
                                  metadata={"model": "offline-mock"})

    args = argparse.Namespace(
        task="split-200ul-two-wells", simulator=simulator,
        output_dir=tmp_path / "out", dataset_root=None, paper_override=None,
        model_timeout=30, max_output_tokens=2048, max_stages=16,
        max_new_stages=1, math_retries=1, stage_retries=0,
    )
    trace = await phased.run_experiment(args, completion=mock_model)
    assert trace["status"] == "reaction_math_rejected"
    assert called == ["ot2_phased_setup", "ot2_phased_reaction_math",
                      "ot2_phased_reaction_math"]
    assert not (args.output_dir / "model_authored_prefix.json").exists()
    math = trace["stages"][0]["reaction_math"]
    assert len(math["attempts"]) == 2
    assert all(item["status"] == "rejected" for item in math["attempts"])
