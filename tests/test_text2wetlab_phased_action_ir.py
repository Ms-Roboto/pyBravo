"""Offline synthetic tests; no benchmark protocol content is authored here."""

from __future__ import annotations

import argparse
import json

import pytest

from pybravo.evals.text2wetlab.action_ir import (
    ActionPlanError,
    LabwareFacts,
    PipetteFacts,
)
from pybravo.evals.text2wetlab.phased_action_ir import (
    audit_prefix_material,
    material_handoff,
    merge_prefix,
    parse_setup,
    parse_stage,
    tip_handoff,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse
from scripts import experiment_text2wetlab_action_ir as one_shot
from scripts import experiment_text2wetlab_phased_action_ir as phased

_INSTRUCTION = (
    "Load 10 uL of stock in source A1.\n"
    "Transfer 5 uL from source A1 to target B1.\n"
    "Transfer 5 uL from source A1 to target B2.\n"
    "Target B1 initially holds 5 uL of liquid.\n"
)
_SPANS, _ = one_shot._line_spans(_INSTRUCTION, None)
_LABWARE = {
    "small_source": LabwareFacts(
        frozenset({"A1"}), ("A1",), well_capacity_ul={"A1": 20},
    ),
    "small_target": LabwareFacts(
        frozenset({"B1", "B2"}), ("B1", "B2"),
        well_capacity_ul={"B1": 12, "B2": 8},
    ),
    "small_tips": LabwareFacts(
        frozenset({"A1", "A2"}), ("A1", "A2"), is_tiprack=True,
        tip_capacity_ul=10, well_capacity_ul={"A1": 10, "A2": 10},
    ),
}
_PIPETTES = {"small_pipette": PipetteFacts(1, 10, 1)}


def _setup() -> dict:
    return {
        "labware": [
            {"id": "source", "load_name": "small_source", "slot": 1,
             "label": "source"},
            {"id": "target", "load_name": "small_target", "slot": 2,
             "label": "target"},
            {"id": "tips", "load_name": "small_tips", "slot": 3,
             "label": "tips"},
        ],
        "modules": [],
        "pipettes": [{"id": "pip", "model": "small_pipette", "mount": "left",
                      "tip_rack_ids": ["tips"]}],
        "stages": [
            {"id": "first", "goal": "Transfer the first aliquot",
             "evidence_refs": ["task.L1", "task.L2"]},
            {"id": "second", "goal": "Transfer the second aliquot",
             "evidence_refs": ["task.L1", "task.L3"]},
        ],
        "initial_supplies": [
            {"labware": "source", "selection": "wells", "wells": ["A1"],
             "columns": [], "volume_ul": 10, "material_id": "stock",
             "evidence_refs": ["task.L1"]},
            {"labware": "target", "selection": "wells", "wells": ["B1"],
             "columns": [], "volume_ul": 5, "material_id": "existing",
             "evidence_refs": ["task.L4"]},
        ],
    }


def _stage(id_: str, target: str, ref: str) -> dict:
    return {
        "stage_id": id_, "evidence_refs": [ref],
        "actions": [
            {"kind": "pickup", "pipette": "pip", "evidence_refs": [ref]},
            {"kind": "aspirate", "pipette": "pip", "labware": "source",
             "well": "A1", "volume_ul": 5, "evidence_refs": [ref]},
            {"kind": "dispense", "pipette": "pip", "labware": "target",
             "well": target, "volume_ul": 5, "evidence_refs": [ref]},
            {"kind": "drop", "pipette": "pip", "evidence_refs": [ref]},
        ],
    }


def _parsed_setup():
    return parse_setup(_setup(), spans=_SPANS, instruction=_INSTRUCTION,
                       paper=None, labware_catalog=_LABWARE)


def _parsed_stages(setup):
    return [
        parse_stage(_stage("first", "B1", "task.L2"), expected=setup.stages[0],
                    spans=_SPANS, instruction=_INSTRUCTION, paper=None),
        parse_stage(_stage("second", "B2", "task.L3"), expected=setup.stages[1],
                    spans=_SPANS, instruction=_INSTRUCTION, paper=None),
    ]


def test_setup_claims_need_exact_source_units_and_catalog_wells():
    assert _parsed_setup().initial_supplies[0].volume_ul == 10
    changed = _setup()
    changed["initial_supplies"][0]["volume_ul"] = 11
    with pytest.raises(ActionPlanError, match="cited source lines"):
        parse_setup(changed, spans=_SPANS, instruction=_INSTRUCTION,
                    paper=None, labware_catalog=_LABWARE)
    changed = _setup()
    changed["initial_supplies"][0]["wells"] = ["B1"]
    with pytest.raises(ActionPlanError, match="absent"):
        parse_setup(changed, spans=_SPANS, instruction=_INSTRUCTION,
                    paper=None, labware_catalog=_LABWARE)


def test_stage_merge_is_ordered_and_citations_are_scoped():
    setup = _parsed_setup()
    first, second = _parsed_stages(setup)
    merged = merge_prefix(setup, [first, second])
    assert len(merged.actions) == 8
    assert merged.actions[1].volume_ul == 5
    with pytest.raises(ActionPlanError, match="merged in"):
        merge_prefix(setup, [second])
    bad = _stage("first", "B1", "task.L3")
    with pytest.raises(ActionPlanError, match="evidence scope"):
        parse_stage(bad, expected=setup.stages[0], spans=_SPANS,
                    instruction=_INSTRUCTION, paper=None)


def test_tip_handoff_is_global_across_stages():
    setup = _parsed_setup()
    first, second = _parsed_stages(setup)
    one = tip_handoff(merge_prefix(setup, [first]), labware_catalog=_LABWARE,
                      pipette_catalog=_PIPETTES)["pip"]
    two = tip_handoff(merge_prefix(setup, [first, second]),
                      labware_catalog=_LABWARE, pipette_catalog=_PIPETTES)["pip"]
    assert one["effective_tip_capacity_ul"] == 10
    assert one["pickups_remaining_current_load"] == 1
    assert two["pickups_remaining_current_load"] == 0


def test_material_ledger_rejects_cumulative_source_exhaustion_and_overfill():
    setup = _parsed_setup()
    events = [
        {"kind": "pick", "instrument": "pip", "channels": 1},
        {"kind": "aspirate", "instrument": "pip", "channels": 1,
         "labware": "source on 1", "well": "A1", "volume": 5},
        {"kind": "dispense", "instrument": "pip", "channels": 1,
         "labware": "target on 2", "well": "B1", "volume": 5},
        {"kind": "drop", "instrument": "pip", "channels": 1},
    ]
    one = audit_prefix_material(events, {}, setup, labware_catalog=_LABWARE)
    assert not one.has_errors
    assert material_handoff(one)["unknown_absolute_well_count"] == 0
    twice = events + [
        events[0], {**events[1], "volume": 6},
        {**events[2], "well": "B2", "volume": 6}, events[3],
    ]
    depleted = audit_prefix_material(twice, {}, setup, labware_catalog=_LABWARE)
    assert {issue.code for issue in depleted.issues} >= {"source_volume_exhausted"}
    overfilled = audit_prefix_material(
        events + [events[0], events[1], events[2], events[3]],
        {}, setup, labware_catalog=_LABWARE,
    )
    assert {issue.code for issue in overfilled.issues} >= {"well_capacity_exceeded"}


def test_multichannel_resolves_true_plate_columns_and_centered_trough():
    catalog = {
        "trough": LabwareFacts(
            frozenset({"A1"}), ("A1",), multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1"}),
            well_capacity_ul={"A1": 100},
        ),
        "plate": LabwareFacts(
            frozenset(f"{row}1" for row in "ABCDEFGH"),
            tuple(f"{row}1" for row in "ABCDEFGH"),
            multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1"}),
            well_capacity_ul={f"{row}1": 10 for row in "ABCDEFGH"},
        ),
    }
    raw = {"labware": [
        {"id": "trough", "load_name": "trough", "slot": 1, "label": "trough"},
        {"id": "plate", "load_name": "plate", "slot": 2, "label": "plate"},
    ], "pipettes": [], "stages": [{"id": "stamp", "goal": "Stamp stock in plate",
                               "evidence_refs": ["task.L1"]}],
           "initial_supplies": [{"labware": "trough", "selection": "all",
                                 "volume_ul": 10, "material_id": "stock",
                                 "evidence_refs": ["task.L1"]}]}
    setup = parse_setup(raw, spans=_SPANS, instruction=_INSTRUCTION,
                        paper=None, labware_catalog=catalog)
    events = [
        {"kind": "pick", "instrument": "multi", "channels": 8},
        {"kind": "aspirate", "instrument": "multi",
         "labware": "trough on 1", "well": "A1", "volume": 1},
        {"kind": "dispense", "instrument": "multi",
         "labware": "plate on 2", "well": "A1", "volume": 1},
        {"kind": "drop", "instrument": "multi", "channels": 8},
    ]
    result = audit_prefix_material(events, {}, setup, labware_catalog=catalog)
    assert not result.has_errors
    assert result.final_wells[next(ref for ref in result.final_wells
                                   if ref.labware == "trough on 1")].volume_ul == 2
    assert sum(ref.labware == "plate on 2" for ref in result.final_wells) == 8


def test_prefix_defers_only_missing_future_heat_shock_but_checks_current_events(
    tmp_path, monkeypatch,
):
    setup = _parsed_setup()
    first = _parsed_stages(setup)[0]
    events = [
        {"kind": "pick", "instrument": "P20 Single-Channel GEN2 on left mount",
         "channels": 1},
        {"kind": "aspirate", "instrument": "P20 Single-Channel GEN2 on left mount",
         "channels": 1, "labware": "source on 1", "well": "A1", "volume": 5},
        {"kind": "dispense", "instrument": "P20 Single-Channel GEN2 on left mount",
         "channels": 1, "labware": "target on 2", "well": "B1", "volume": 5},
        {"kind": "drop", "instrument": "P20 Single-Channel GEN2 on left mount",
         "channels": 1},
    ]
    official = tmp_path / "official_events.json"
    contact = tmp_path / "contact_events.json"
    official.write_text(json.dumps(events))
    contact.write_text(json.dumps(events))
    monkeypatch.setattr(phased, "labware_geometry_context", lambda *a, **k: {})
    monkeypatch.setattr(phased, "validate_ot2_source", lambda *a, **k: None)
    monkeypatch.setattr(phased.runner, "_run", lambda *a, **k:
                        {"status": "passed"})
    safety_detail = "Requested on-deck heat shock has no timed high-temperature block command."

    def mock_log(*args, **kwargs):
        return {"status": "passed", "events_path": str(official),
                "contact_evidence_events_path": str(contact),
                "adapter_event_validation": {"status": "failed", "detail": safety_detail},
                "cross_well_aspiration_risk_count": 0,
                "labware": {"source on 1": "small_source",
                            "target on 2": "small_target"}}

    monkeypatch.setattr(phased.runner, "_official_runlog", mock_log)
    kwargs = dict(instruction=_INSTRUCTION, paper=None, spans=_SPANS,
                  labware=_LABWARE, pipettes=_PIPETTES, modules={},
                  simulator=tmp_path / "simulator",
                  task="synthetic", task_dir=tmp_path / "prefix",
                  dataset_root=None, labware_dir=None)
    result, handoff = phased._prefix_gate(setup, [first], **kwargs)
    assert result["status"] == "prefix_passed"
    assert result["event_safety"]["full_process_check"] == "pending_later_stage"
    assert handoff["materials"]["unknown_absolute_well_count"] == 0
    safety_detail = "Requested recovery has no lower-temperature block command after heat shock."
    result, handoff = phased._prefix_gate(setup, [first], **kwargs)
    assert result["status"] == "event_safety_rejected"
    assert handoff is None


@pytest.mark.asyncio
async def test_two_mocked_local_calls_checkpoint_each_stage(tmp_path, monkeypatch):
    simulator = tmp_path / "simulator"
    simulator.touch()
    output = tmp_path / "phased"
    monkeypatch.setattr(phased.runner, "_source_bytes", lambda task, name, root:
                        _INSTRUCTION.encode() if name == "instruction.md" else None)
    monkeypatch.setattr(phased, "load_trusted_catalog", lambda *a, **k:
                        (_LABWARE, _PIPETTES))
    monkeypatch.setattr(phased, "load_trusted_module_catalog", lambda *a, **k: {})
    monkeypatch.setattr(phased.LocalLLMConfig, "from_env", LocalLLMConfig)
    received: list[dict] = []
    responses = iter([_setup(), _stage("first", "B1", "task.L2"),
                      _stage("second", "B2", "task.L3")])

    async def mock_model(messages, schema, **kwargs):
        received.append({"messages": messages, "schema": schema})
        return StructuredResponse(payload=next(responses), metadata={"model": "mock"})

    checkpoints: list[list[str]] = []

    def mock_gate(setup, stages, **kwargs):
        ids = [stage.stage_id for stage in stages]
        checkpoints.append(ids)
        return {"status": "prefix_passed"}, {
            "tips": {"pip": {"effective_tip_capacity_ul": 10,
                             "pickups_remaining_current_load": 2 - len(stages)}},
            "materials": {"observed_net_change_ul": len(stages)},
            "completed_stage_ids": ids, "event_count": 4 * len(stages),
        }

    monkeypatch.setattr(phased, "_prefix_gate", mock_gate)
    monkeypatch.setattr(phased, "_check_compiled_plan", lambda *a, **k:
                        {"status": "mechanical_gates_passed", "official_score": None})
    args = argparse.Namespace(task="split-200ul-two-wells", simulator=simulator,
                              output_dir=output, dataset_root=None, paper_override=None,
                              model_timeout=30, max_output_tokens=1024)
    trace = await phased.run_experiment(args, completion=mock_model)
    assert trace["status"] == "mechanical_gates_passed"
    assert checkpoints == [["first"], ["first", "second"]]
    assert len(received) == 3
    assert "pickups_remaining_current_load" in received[2]["messages"][1]["content"]
    merged = json.loads((output / "model_authored_action_plan.json").read_text())
    assert len(merged["actions"]) == 8
    assert trace["official_score"] is None


@pytest.mark.asyncio
async def test_failed_prefix_stops_before_next_model_call(tmp_path, monkeypatch):
    simulator = tmp_path / "simulator"
    simulator.touch()
    monkeypatch.setattr(phased.runner, "_source_bytes", lambda task, name, root:
                        _INSTRUCTION.encode() if name == "instruction.md" else None)
    monkeypatch.setattr(phased, "load_trusted_catalog", lambda *a, **k:
                        (_LABWARE, _PIPETTES))
    monkeypatch.setattr(phased, "load_trusted_module_catalog", lambda *a, **k: {})
    monkeypatch.setattr(phased.LocalLLMConfig, "from_env", LocalLLMConfig)
    calls = 0

    async def mock_model(messages, schema, **kwargs):
        nonlocal calls
        calls += 1
        return StructuredResponse(payload=_setup() if calls == 1
                                  else _stage("first", "B1", "task.L2"),
                                  metadata={"model": "mock"})

    monkeypatch.setattr(phased, "_prefix_gate", lambda *a, **k:
                        ({"status": "material_rejected"}, None))
    args = argparse.Namespace(task="split-200ul-two-wells", simulator=simulator,
                              output_dir=tmp_path / "failed", dataset_root=None,
                              paper_override=None, model_timeout=30,
                              max_output_tokens=1024)
    trace = await phased.run_experiment(args, completion=mock_model)
    assert trace["status"] == "stage_rejected"
    assert calls == 2
    assert not (args.output_dir / "model_authored_action_plan.json").exists()
