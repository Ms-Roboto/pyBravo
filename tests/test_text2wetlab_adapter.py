"""The OT-2 benchmark adapter must reject unsafe code before simulation."""

from __future__ import annotations

import json

import pytest

from pybravo.evals.text2wetlab import adapter
from pybravo.evals.text2wetlab.planning import DeckSource, OT2Plan, PlannedAddition, PlannedReaction
from pybravo.evals.text2wetlab.planning_runtime import PlanningResult
from pybravo.evals.text2wetlab.reaction import Addition
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse

VALID_PROTOCOL = '''from opentrons import protocol_api
metadata = {"apiLevel": "2.15"}

def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 11)
    source = protocol.load_labware("nest_1_reservoir_195ml", 1, label="reservoir")
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2, label="plate")
    pipette = protocol.load_instrument("p300_single_gen2", "right", tip_racks=[tips])
    for well in plate.rows()[0]:
        pipette.transfer(100, source["A1"], well, new_tip="always")
'''
REVISED_PROTOCOL = VALID_PROTOCOL.replace("    for well in plate.rows()[0]:", "    # Revised tip sequence\n    for well in plate.rows()[0]:")

SOURCE_DRIFT_PROTOCOL = '''from opentrons import protocol_api
metadata = {"apiLevel": "2.15"}

def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_96_tiprack_20ul", 10)
    primer = protocol.load_labware("corning_96_wellplate_360ul_flat", 4, label="primer_plate")
    reaction = protocol.load_labware("corning_96_wellplate_360ul_flat", 2, label="reaction_plate")
    pipette = protocol.load_instrument("p20_single_gen2", "left", tip_racks=[tips])
    pipette.transfer(1, primer["A1"], reaction["A1"], new_tip="always")
'''


def _local_science_audit(status: str, *, evidence: str = "") -> dict:
    return {"items": [{"id": "local_check", "status": status,
                       "checks": [{"name": "sample volumes", "status": status,
                                   "evidence": evidence}]}],
            "status": status, "official_score": None}


@pytest.mark.parametrize("source, reason", [
    ("import os\n" + VALID_PROTOCOL, "Unsupported import"),
    (VALID_PROTOCOL.replace("from opentrons import protocol_api", "from opentrons import execute"),
     "Only Opentrons protocol_api"),
    (VALID_PROTOCOL.replace("from opentrons import protocol_api", "from opentrons.protocol_api import sys"),
     "Only ProtocolContext"),
    (VALID_PROTOCOL.replace("    tips =", "    open('/tmp/file')\n    tips ="), "Unsupported call: open"),
    (VALID_PROTOCOL.replace("    tips =", "    protocol._implementation\n    tips ="), "Unsupported attribute"),
    (VALID_PROTOCOL.replace("    tips =", "    protocol.comment('/tests/answer')\n    tips ="),
     "path to benchmark grader"),
    (VALID_PROTOCOL.replace("    tips =", "    plate.read_text()\n    tips ="), "Unsupported attribute"),
    (VALID_PROTOCOL.replace("    tips =", "    pipette.some_flag = True\n    tips ="),
     "Assignment to object attributes"),
    (VALID_PROTOCOL.replace("    tips =", "    del pipette.some_flag\n    tips ="),
     "Assignment to object attributes"),
    (VALID_PROTOCOL.replace("    tips =", "    protocol.comment('rubric')\n    tips ="),
     "prohibited benchmark text"),
    (VALID_PROTOCOL.replace("    tips =", "    protocol.comment('is_simulating')\n    tips ="),
     "prohibited benchmark text"),
    (VALID_PROTOCOL.replace("    tips =", "    protocol.load_labware_from_definition({})\n    tips ="),
     "prohibited benchmark text"),
    (VALID_PROTOCOL.replace("metadata =", "print('at import time')\nmetadata ="),
     "Executable top-level statements"),
    (VALID_PROTOCOL.replace("    tips =", "    pass\n    tips =").replace(
        "    tips = protocol.load_labware(\"opentrons_96_tiprack_300ul\", 11)",
        "    tips = None").replace(
        "    source = protocol.load_labware(\"nest_1_reservoir_195ml\", 1, label=\"reservoir\")",
        "    source = None").replace(
        "    plate = protocol.load_labware(\"corning_96_wellplate_360ul_flat\", 2, label=\"plate\")",
        "    plate = None").replace(
        "    pipette = protocol.load_instrument(\"p300_single_gen2\", \"right\", tip_racks=[tips])",
        "    pipette = None"), "must contain an OT-2 protocol action"),
])
def test_static_gate_rejects_unsafe_or_empty_protocols(source, reason):
    with pytest.raises(adapter.ProtocolValidationError, match=reason):
        adapter.validate_ot2_source(source)


def test_static_gate_accepts_normal_ot2_protocol():
    adapter.validate_ot2_source(VALID_PROTOCOL)


def test_static_gate_rejects_a_literal_p300_stroke_below_working_range():
    source = VALID_PROTOCOL.replace(
        '    for well in plate.rows()[0]:',
        '    pipette.aspirate(9, source["A1"])\n    for well in plate.rows()[0]:',
    )
    with pytest.raises(adapter.ProtocolValidationError, match=r"aspirate\(9 µL\).*20–300 µL"):
        adapter.validate_ot2_source(source)


def test_static_gate_requires_explicit_mix_well():
    source = VALID_PROTOCOL.replace(
        '    for well in plate.rows()[0]:',
        '    pipette.mix(5, 40)\n    for well in plate.rows()[0]:',
    )
    with pytest.raises(adapter.ProtocolValidationError, match=r"mix\(\) must name its target well"):
        adapter.validate_ot2_source(source)


def test_static_gate_uses_loaded_tip_capacity_for_literal_strokes():
    source = VALID_PROTOCOL.replace("opentrons_96_tiprack_300ul", "opentrons_96_filtertiprack_200ul")
    source = source.replace("pipette.transfer(100,", "pipette.transfer(250,")
    with pytest.raises(adapter.ProtocolValidationError, match=r"transfer\(250 µL\).*20–200 µL"):
        adapter.validate_ot2_source(source)


def test_static_gate_rejects_p1000_below_its_gen2_minimum():
    source = VALID_PROTOCOL.replace("p300_single_gen2", "p1000_single_gen2")
    source = source.replace("opentrons_96_tiprack_300ul", "opentrons_96_tiprack_1000ul")
    source = source.replace("pipette.transfer(100,", "pipette.transfer(40,")
    with pytest.raises(adapter.ProtocolValidationError, match=r"transfer\(40 µL\).*100–1000 µL"):
        adapter.validate_ot2_source(source)


def test_tip_attached_diagnostic_explains_mixed_tip_policies_and_source_line():
    advice = adapter._repair_guidance(
        "ProtocolCommandFailedError [line 9]: TipAttachedError: Pipette should not have a tip attached",
        VALID_PROTOCOL,
    )
    assert "manual `pick_up_tip()`" in advice
    assert "`new_tip='never'`" in advice
    assert "9:     for well in plate.rows()[0]:" in advice


def test_numeric_labware_index_diagnostic_preserves_well_mapping():
    source = VALID_PROTOCOL.replace("    for well in plate.rows()[0]:", "    for i in range(12):\n        well = plate[i]")
    advice = adapter._repair_guidance("KeyError [line 9]: 0", source)
    assert "plate.wells()[i]" in advice
    assert "source-to-destination pairing" in advice
    assert "9:     for i in range(12):" in advice


def test_well_object_labware_key_diagnostic_preserves_pairing():
    advice = adapter._repair_guidance(
        "KeyError [line 9]: A1 of pcr_plate on slot 2",
        VALID_PROTOCOL,
    )
    assert "plate[well.well_name]" in advice
    assert "zip the `.wells()` lists" in advice
    assert "one-to-one source/reaction mapping" in advice


def test_out_of_range_well_index_diagnostic_uses_actual_labware_geometry():
    advice = adapter._repair_guidance("IndexError [line 9]: list index out of range", VALID_PROTOCOL)
    assert "plate.columns()[column_index][row_index]" in advice
    assert "tube rack" in advice
    assert "sample-to-destination mapping" in advice


def test_thermocycler_diagnostics_use_public_opentrons_75_api():
    lid = adapter._repair_guidance(
        "ProtocolCommandFailedError [line 9]: Thermocycler lid temperature must be between 37 and 110, but got 4.0.",
        VALID_PROTOCOL,
    )
    missing_wait = adapter._repair_guidance(
        "AttributeError [line 9]: 'ThermocyclerContext' object has no attribute 'wait_for_block_temperature'",
        VALID_PROTOCOL,
    )
    assert "set_block_temperature(4, hold_time_minutes=...)" in lid
    assert "close_lid()" in lid
    assert "set_block_temperature(temperature, hold_time_minutes=...)" in missing_wait
    assert "Remove the nonexistent call" in missing_wait


def test_out_of_tips_diagnostic_respects_task_refill_permission():
    advice = adapter._repair_guidance("OutOfTipsError [line 98]: Pipette ran out of tips", VALID_PROTOCOL)
    assert "only one step" in advice
    assert "If the task explicitly allows rack refilling" in advice
    assert "reset_tipracks()" in advice
    assert "At the exact exhaustion boundary" in advice


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [False, True])
async def test_fresh_tip_refill_skill_is_loaded_only_when_task_allows_it(
    tmp_path, monkeypatch, permission,
):
    monkeypatch.setattr(adapter, "labware_geometry_context", lambda *a, **k: {})
    monkeypatch.setattr(adapter, "simulate_protocol",
                        lambda *a, **k: adapter.SimulationResult("passed", "ok"))
    instruction = ("Transfer 100 µL. Tips are unlimited: call pipette.reset_tipracks() "
                   "after loading fresh racks." if permission else "Transfer 100 µL.")

    async def completion(messages, schema, **kwargs):
        assert ("per-pipette pickup counter" in messages[0]["content"]) is permission
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    await adapter.generate_ot2_protocol(
        instruction, tmp_path, completion=completion,
        event_reader=lambda _: adapter.EventLog([], {}),
    )


def test_out_of_range_stroke_diagnostic_preserves_stock_equivalent_dose():
    advice = adapter._repair_guidance(
        "Line 106: p20.dispense(0.5 µL) is outside this pipette's 1–20 µL working range.",
        VALID_PROTOCOL,
    )
    assert "never round up" in advice
    assert "batch dilution" in advice
    assert "stock-equivalent dose" in advice
    assert "explicit manual handoff" in advice


@pytest.mark.asyncio
async def test_simulation_error_repairs_protocol_and_sets_trace_gate(tmp_path, monkeypatch):
    calls = []
    model_messages = []
    simulations = iter([
        adapter.SimulationResult("failed", "deck slot 11 is occupied"),
        adapter.SimulationResult("passed", "opentrons_simulate completed successfully."),
    ])

    async def completion(messages, schema, **kwargs):
        model_messages.append(messages)
        calls.append(kwargs)
        return StructuredResponse({"code": VALID_PROTOCOL if len(calls) == 1 else REVISED_PROTOCOL},
                                  {"model": "local-qwen"})

    def fake_simulate(path, **kwargs):
        assert path.read_text(encoding="utf-8") in {VALID_PROTOCOL, REVISED_PROTOCOL}
        return next(simulations)

    monkeypatch.setattr(adapter, "simulate_protocol", fake_simulate)
    result = await adapter.generate_ot2_protocol(
        "Transfer 100 uL from a reservoir to twelve wells.", tmp_path,
        completion=completion, repair_attempts=1,
        event_reader=lambda _: adapter.EventLog([], {}),
    )
    assert result.attempts == 2
    assert result.simulation.status == "passed"
    assert result.protocol_path.read_text(encoding="utf-8") == REVISED_PROTOCOL
    assert (tmp_path / "candidate_attempt_1.py").read_text(encoding="utf-8") == VALID_PROTOCOL
    assert not (tmp_path / "candidate_attempt_2.py").exists()
    assert "deck slot 11 is occupied" in model_messages[1][-1]["content"]
    assert calls[0]["schema_name"] == "ot2_protocol"
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "simulated"
    assert trace["static_validation_passed"] is True
    assert trace["event_validation_passed"] is True
    assert [attempt["simulation"] for attempt in trace["attempts"]] == ["failed", "passed"]
    assert trace["attempts"][0]["candidate_path"] == str(tmp_path / "candidate_attempt_1.py")
    assert "candidate_path" not in trace["attempts"][1]


@pytest.mark.asyncio
async def test_observable_scientific_failure_requests_source_grounded_full_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "simulate_protocol",
                        lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    monkeypatch.setattr(adapter, "audit_rubric_coverage",
                        lambda task, events, code, labware: _local_science_audit(
                            "supported" if "Revised tip sequence" in code else "failed",
                            evidence="SECRET BENCHMARK REFERENCE SHOULD NOT LEAK"))
    model_messages = []

    async def completion(messages, schema, **kwargs):
        model_messages.append(messages)
        return StructuredResponse({"code": VALID_PROTOCOL if len(model_messages) == 1 else REVISED_PROTOCOL},
                                  {"model": "local-qwen"})

    result = await adapter.generate_ot2_protocol(
        "Transfer 100 µL from reservoir A1 into each well A1 through A12.", tmp_path,
        rubric_task="a1-a12-100ul", completion=completion,
        event_reader=lambda _: adapter.EventLog([], {}), repair_attempts=1,
    )
    assert result.attempts == 2
    assert result.protocol_path.read_text() == REVISED_PROTOCOL
    second_prompt = model_messages[1][-1]["content"]
    assert "Transfer 100 µL from reservoir A1" in second_prompt
    assert "SECRET BENCHMARK" not in second_prompt
    assert "observable gaps" in second_prompt
    trace = json.loads(result.trace_path.read_text())
    assert [attempt["scientific_audit"] for attempt in trace["attempts"]] == ["failed", "passed"]
    assert trace["attempts"][0]["local_rubric_audit"]["official_score"] is None


@pytest.mark.asyncio
async def test_scientific_needs_review_does_not_block_simulated_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "simulate_protocol",
                        lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    monkeypatch.setattr(adapter, "audit_rubric_coverage",
                        lambda *args: _local_science_audit("needs_review", evidence="manual step"))

    async def completion(*args, **kwargs):
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    result = await adapter.generate_ot2_protocol(
        "Transfer 100 µL from reservoir A1 into each well A1 through A12.", tmp_path,
        rubric_task="a1-a12-100ul", completion=completion,
        event_reader=lambda _: adapter.EventLog([], {}), repair_attempts=0,
    )
    assert result.simulation.status == "passed"
    trace = json.loads(result.trace_path.read_text())
    assert trace["attempts"][0]["scientific_audit"] == "passed"


@pytest.mark.asyncio
async def test_simulated_direct_source_dose_must_match_accepted_local_plan(tmp_path, monkeypatch):
    plan = OT2Plan(
        (DeckSource("primer_plate", "primer pairs", "corning_96_wellplate_360ul_flat", ()),),
        (PlannedReaction("reaction", 0.5, None, (
            PlannedAddition(Addition("primer pairs", 0.5), "primer_plate", ()),
        ), ()),), (), (),
    )

    async def fake_plan(**kwargs):
        return PlanningResult(plan, ())

    async def completion(messages, schema, **kwargs):
        assert "stock-equivalent" in messages[0]["content"]
        return StructuredResponse({"code": SOURCE_DRIFT_PROTOCOL}, {"model": "local-qwen"})

    log = adapter.EventLog([
        {"kind": "pick", "instrument": "P20 Single", "channels": 1},
        {"kind": "aspirate", "instrument": "P20 Single", "channels": 1,
         "volume": 1.0, "well": "A1", "labware": "primer_plate on 4"},
        {"kind": "dispense", "instrument": "P20 Single", "channels": 1,
         "volume": 1.0, "well": "A1", "labware": "reaction_plate on 2"},
        {"kind": "drop", "instrument": "P20 Single", "channels": 1},
    ], {
        "primer_plate on 4": "corning_96_wellplate_360ul_flat",
        "reaction_plate on 2": "corning_96_wellplate_360ul_flat",
    })
    monkeypatch.setattr(adapter, "run_grounded_plan", fake_plan)
    monkeypatch.setattr(adapter, "labware_geometry_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(adapter, "simulate_protocol",
                        lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    with pytest.raises(adapter.GenerationError, match="simulated liquid actions differ"):
        await adapter.generate_ot2_protocol(
            "Transfer primer pairs to a reaction well.", tmp_path,
            completion=completion, event_reader=lambda _: log,
            evidence_planning=True, repair_attempts=0, patch_attempts=0,
        )
    trace = json.loads((tmp_path / "generation_trace.json").read_text())
    assert trace["status"] == "failed"
    assert trace["attempts"][0]["source_fidelity"][0]["code"] == "delivered_volume_differs_from_plan"
    assert not (tmp_path / "protocol.py").exists()


@pytest.mark.asyncio
async def test_scientific_failure_in_mechanically_valid_patch_needs_full_repair(tmp_path, monkeypatch):
    async def completion(messages, schema, **kwargs):
        code = VALID_PROTOCOL if not any("Your previous generated code failed" in m["content"]
                                          for m in messages) else REVISED_PROTOCOL
        return StructuredResponse({"code": code}, {"model": "local-qwen"})

    async def patch_completion(*args, **kwargs):
        assert kwargs["config"].max_tokens == 4096
        return StructuredResponse({"edits": [{"start_line": 1, "end_line": 0,
                                               "replacement": "# simulator repair"}]},
                                  {"model": "local-qwen"})

    def simulate(path, **kwargs):
        return adapter.SimulationResult("failed" if path.read_text() == VALID_PROTOCOL else "passed", "error")

    monkeypatch.setattr(adapter, "simulate_protocol", simulate)
    monkeypatch.setattr(adapter, "audit_rubric_coverage",
                        lambda task, events, code, labware: _local_science_audit(
                            "failed" if "# simulator repair" in code else "supported"))
    result = await adapter.generate_ot2_protocol(
        "Transfer 100 µL from reservoir A1 into each well A1 through A12.", tmp_path,
        rubric_task="a1-a12-100ul", completion=completion, patch_completion=patch_completion,
        event_reader=lambda _: adapter.EventLog([], {}), repair_attempts=1, patch_attempts=1,
        config=LocalLLMConfig(max_tokens=12_000),
    )
    assert result.attempts == 2
    assert result.protocol_path.read_text() == REVISED_PROTOCOL
    trace = json.loads(result.trace_path.read_text())
    assert trace["attempts"][0]["patches"][0]["status"] == "scientific_audit_rejected"
    assert trace["attempts"][1]["scientific_audit"] == "passed"


@pytest.mark.asyncio
async def test_generation_supplies_catalog_geometry_and_records_it(tmp_path, monkeypatch):
    geometry = {"sample_plate": {
        "columns": 12, "rows_per_column": 8, "well_count": 96,
        "first_column": [f"{row}1" for row in "ABCDEFGH"],
        "last_column": [f"{row}12" for row in "ABCDEFGH"],
        "is_tiprack": False,
    }}
    monkeypatch.setattr(adapter, "labware_geometry_context", lambda *args, **kwargs: geometry)
    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    messages_seen = []

    async def completion(messages, schema, **kwargs):
        messages_seen.extend(messages)
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    result = await adapter.generate_ot2_protocol(
        "Use `sample_plate`.", tmp_path, completion=completion,
        event_reader=lambda _: adapter.EventLog([], {}),
    )
    assert "rows_per_column" in messages_seen[-1]["content"]
    assert "experimental volumes" in messages_seen[-1]["content"]
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["labware_geometry"] == geometry


@pytest.mark.asyncio
async def test_failed_static_gate_never_simulates_and_never_leaves_stale_output(tmp_path, monkeypatch):
    (tmp_path / "protocol.py").write_text("stale", encoding="utf-8")

    async def completion(*args, **kwargs):
        return StructuredResponse({"code": "import os\n" + VALID_PROTOCOL}, {"model": "local-qwen"})

    def forbidden_simulate(*args, **kwargs):
        raise AssertionError("Unsafe candidate reached simulator")

    monkeypatch.setattr(adapter, "simulate_protocol", forbidden_simulate)
    with pytest.raises(adapter.GenerationError, match="No valid OT-2 protocol"):
        await adapter.generate_ot2_protocol("Do a transfer.", tmp_path, completion=completion, repair_attempts=0)
    assert not (tmp_path / "protocol.py").exists()
    trace = json.loads((tmp_path / "generation_trace.json").read_text(encoding="utf-8"))
    assert trace["status"] == "failed"
    assert trace["static_validation_passed"] is False
    assert trace["event_validation_passed"] is False
    assert trace["attempts"][0]["validation"] == "failed"
    rejected = tmp_path / "rejected_attempt_1.py.txt"
    assert rejected.read_text(encoding="utf-8").startswith("import os\n")
    assert trace["attempts"][0]["rejected_source_path"] == str(rejected)
    assert not list(tmp_path.glob("candidate_attempt_*.py"))


@pytest.mark.asyncio
async def test_missing_simulator_is_reported_as_unverified(tmp_path, monkeypatch):
    async def completion(*args, **kwargs):
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    monkeypatch.setattr(adapter.shutil, "which", lambda _: None)
    result = await adapter.generate_ot2_protocol("Do a transfer.", tmp_path, completion=completion)
    assert result.simulation.status == "unavailable"
    assert result.protocol_path.exists()
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "static_validated_only"
    assert trace["static_validation_passed"] is True
    assert trace["event_validation_passed"] is False


def _event(kind, *, labware=None, well=None, instrument="P300 right"):
    event = {"kind": kind, "instrument": instrument, "channels": 1}
    if labware is not None:
        event.update(labware=labware, well=well, volume=40.0)
    return event


def test_event_gate_allows_one_reservoir_to_feed_empty_destinations():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="reagent on 2", well="A1"),
        _event("dispense", labware="plate on 1", well="A1"),
        _event("aspirate", labware="reagent on 2", well="A1"),
        _event("dispense", labware="plate on 1", well="A2"),
        _event("drop"),
    ], {"reagent on 2": "nest_1_reservoir_195ml", "plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_contamination(log)
    assert result.status == "passed"


def test_event_gate_requires_fresh_tip_between_reagent_stocks():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="stocks on 2", well="A1"),
        _event("dispense", labware="plate on 1", well="A1"),
        _event("aspirate", labware="stocks on 2", well="A2"),
        _event("drop"),
    ], {"stocks on 2": "nest_12_reservoir_15ml", "plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_contamination(log)
    assert result.status == "failed"
    assert "A1 of stocks on 2" in result.detail
    assert "A2 of stocks on 2" in result.detail


def test_event_gate_allows_one_specimen_source_to_one_reaction_well_and_mix():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="plasmid on 1", well="A1"),
        _event("dispense", labware="cells on 7", well="A1"),
        _event("aspirate", labware="cells on 7", well="A1"),
        _event("dispense", labware="cells on 7", well="A1"),
        _event("drop"),
    ], {"plasmid on 1": "biorad_96_wellplate_200ul_pcr",
        "cells on 7": "biorad_96_wellplate_200ul_pcr"})
    assert adapter.validate_event_contamination(log).status == "passed"


def test_event_gate_rejects_second_specimen_destination_on_same_tip():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="plasmid on 1", well="A1"),
        _event("dispense", labware="cells on 7", well="A1"),
        _event("aspirate", labware="cells on 7", well="A1"),
        _event("dispense", labware="cells on 7", well="B1"),
        _event("drop"),
    ], {"plasmid on 1": "biorad_96_wellplate_200ul_pcr",
        "cells on 7": "biorad_96_wellplate_200ul_pcr"})
    result = adapter.validate_event_contamination(log)
    assert result.status == "failed"
    assert "A1 of cells on 7" in result.detail
    assert "B1 of cells on 7" in result.detail


@pytest.mark.asyncio
async def test_generation_uses_verbatim_methods_passage_with_source_digest(tmp_path, monkeypatch):
    import hashlib

    source = "Paper title\nMethods\nUse 40 µL beads.\nResults\n" + "irrelevant result " * 80
    monkeypatch.setattr(adapter, "labware_geometry_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    messages_seen = []

    async def completion(messages, schema, **kwargs):
        messages_seen.extend(messages)
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    result = await adapter.generate_ot2_protocol(
        "Clean up plate.", tmp_path, scientific_source=source, completion=completion,
        event_reader=lambda _: adapter.EventLog([], {}),
    )
    assert "Use 40 µL beads." in messages_seen[1]["content"]
    assert "irrelevant result" not in messages_seen[1]["content"]
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["scientific_source"]["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert trace["scientific_source"]["strategy"] == "verbatim_methods_section"


@pytest.mark.asyncio
async def test_optional_planning_uses_separate_bounded_paper_excerpt(tmp_path, monkeypatch):
    from pybravo.evals.text2wetlab.source_context import SourceContext

    paper = "Methods\nUse 40 µL beads.\nResults\n"
    excerpt = SourceContext("Methods\nUse 40 µL beads.", "bounded", "source-digest",
                            "excerpt-digest", None, None, ((1, 2),))
    monkeypatch.setattr(adapter, "prepare_planning_source", lambda source, **kwargs: excerpt)
    monkeypatch.setattr(adapter, "labware_geometry_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(adapter, "simulate_protocol",
                        lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))

    async def fake_plan(**kwargs):
        assert kwargs["scientific_source"] == excerpt.text
        return PlanningResult(None, ())

    async def completion(messages, schema, **kwargs):
        assert "Use 40 µL beads." in messages[1]["content"]
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    monkeypatch.setattr(adapter, "run_grounded_plan", fake_plan)
    result = await adapter.generate_ot2_protocol(
        "Clean up plate.", tmp_path, scientific_source=paper,
        evidence_planning=True, completion=completion,
        event_reader=lambda _: adapter.EventLog([], {}),
    )
    trace = json.loads(result.trace_path.read_text())
    assert trace["planning_scientific_source"]["excerpt_sha256"] == "excerpt-digest"
    assert trace["scientific_source"]["excerpt_sha256"] != "excerpt-digest"


def test_event_gate_rejects_reusing_tip_across_specimen_wells():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="reagent on 2", well="A1"),
        _event("dispense", labware="sample plate on 1", well="A1"),
        _event("aspirate", labware="sample plate on 1", well="A1"),
        _event("dispense", labware="sample plate on 1", well="A1"),
        _event("aspirate", labware="reagent on 2", well="A1"),
        _event("dispense", labware="sample plate on 1", well="B1"),
        _event("aspirate", labware="sample plate on 1", well="B1"),
        _event("drop"),
    ], {"reagent on 2": "nest_1_reservoir_195ml", "sample plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_contamination(log)
    assert result.status == "failed"
    assert "A1 of sample plate on 1" in result.detail
    assert "B1 of sample plate on 1" in result.detail


def test_event_gate_rejects_multiple_specimen_wells_even_without_return_to_reservoir():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="sample plate on 1", well="A1"),
        _event("dispense", labware="waste on 5", well="A1"),
        _event("aspirate", labware="sample plate on 1", well="B1"),
        _event("drop"),
    ], {"sample plate on 1": "corning_96_wellplate_360ul_flat", "waste on 5": "nest_1_reservoir_195ml"})
    result = adapter.validate_event_contamination(log)
    assert result.status == "failed"
    assert "A1 of sample plate on 1" in result.detail
    assert "B1 of sample plate on 1" in result.detail


def test_event_gate_allows_distinct_specimen_wells_after_tip_change():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="sample plate on 1", well="A1"),
        _event("drop"),
        _event("pick"),
        _event("aspirate", labware="sample plate on 1", well="B1"),
        _event("drop"),
    ], {"sample plate on 1": "corning_96_wellplate_360ul_flat"})
    assert adapter.validate_event_contamination(log).status == "passed"


def test_event_gate_requires_dropping_final_tip():
    log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="reagent on 2", well="A1"),
        _event("dispense", labware="plate on 1", well="A1"),
    ], {"reagent on 2": "nest_1_reservoir_195ml", "plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_contamination(log)
    assert result.status == "failed"
    assert "ended with an attached tip" in result.detail


def test_event_safety_rejects_a_below_range_p300_stroke_that_simulator_allows():
    log = adapter.EventLog([
        _event("pick"),
        {**_event("aspirate", labware="reagent on 2", well="A1"), "volume": 9.0},
        {**_event("dispense", labware="plate on 1", well="A1"), "volume": 9.0},
        _event("drop"),
    ], {"reagent on 2": "nest_1_reservoir_195ml", "plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_safety(log)
    assert result.status == "failed"
    assert "9.0 µL" in result.detail
    assert "20–300 µL" in result.detail


def test_event_safety_accepts_a_one_microliter_p20_stroke():
    log = adapter.EventLog([
        _event("pick", instrument="P20 left"),
        {**_event("aspirate", labware="source on 1", well="A1", instrument="P20 left"), "volume": 1.0},
        {**_event("dispense", labware="dest on 2", well="A1", instrument="P20 left"), "volume": 1.0},
        _event("drop", instrument="P20 left"),
    ], {"source on 1": "nest_1_reservoir_195ml", "dest on 2": "corning_96_wellplate_360ul_flat"})
    assert adapter.validate_event_safety(log).status == "passed"


def test_event_safety_rejects_p1000_below_documented_minimum():
    log = adapter.EventLog([
        _event("pick", instrument="P1000 left"),
        {**_event("aspirate", labware="reagent on 2", well="A1", instrument="P1000 left"), "volume": 40.0},
        {**_event("dispense", labware="plate on 1", well="A1", instrument="P1000 left"), "volume": 40.0},
        _event("drop", instrument="P1000 left"),
    ], {"reagent on 2": "nest_1_reservoir_195ml", "plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_safety(log)
    assert result.status == "failed"
    assert "100–1000 µL" in result.detail


def test_event_safety_rejects_dispense_larger_than_held_liquid():
    log = adapter.EventLog([
        _event("pick"),
        {**_event("aspirate", labware="reagent on 2", well="A1"), "volume": 40.0},
        {**_event("dispense", labware="plate on 1", well="A1"), "volume": 50.0},
        _event("drop"),
    ], {"reagent on 2": "nest_1_reservoir_195ml", "plate on 1": "corning_96_wellplate_360ul_flat"})
    result = adapter.validate_event_safety(log)
    assert result.status == "failed"
    assert "only 40 µL held" in result.detail


def test_heat_shock_gate_requires_transition_before_recovery_pipetting():
    pulse = {"kind": "thermocycler", "instrument": "", "text": (
        "Setting Thermocycler well block temperature to 42.0 °C "
        "with a hold time of 30 seconds"
    )}
    recovery = {"kind": "thermocycler", "instrument": "", "text": (
        "Setting Thermocycler well block temperature to 37.0 °C "
        "with a hold time of 60.0 minutes"
    )}
    liquid_events = [
        _event("pick"),
        {**_event("aspirate", labware="soc on 2", well="A1"), "volume": 50.0},
        {**_event("dispense", labware="cells on 1", well="A1"), "volume": 50.0},
        _event("drop"),
    ]
    labware = {"soc on 2": "nest_12_reservoir_15ml", "cells on 1": "biorad_96_wellplate_200ul_pcr"}
    instruction = "Perform heat-shock transformation on the thermocycler, then recovery with SOC."
    late = adapter.EventLog([pulse, *liquid_events, recovery], labware)
    early = adapter.EventLog([pulse, recovery, *liquid_events], labware)
    result = adapter.validate_event_safety(late, instruction=instruction)
    assert result.status == "failed"
    assert "block remains at the high temperature" in result.detail
    assert adapter.validate_event_safety(early, instruction=instruction).status == "passed"


@pytest.mark.asyncio
async def test_event_failure_enters_model_repair_loop(tmp_path, monkeypatch):
    prompts = []
    logs = iter([
        adapter.EventLog([
            _event("pick"),
            _event("aspirate", labware="plate on 1", well="A1"),
            _event("aspirate", labware="plate on 1", well="B1"),
            _event("drop"),
        ], {"plate on 1": "corning_96_wellplate_360ul_flat"}),
        adapter.EventLog([
            _event("pick"),
            _event("aspirate", labware="plate on 1", well="A1"),
            _event("drop"),
            _event("pick"),
            _event("aspirate", labware="plate on 1", well="B1"),
            _event("drop"),
        ], {"plate on 1": "corning_96_wellplate_360ul_flat"}),
    ])

    async def completion(messages, schema, **kwargs):
        prompts.append(messages)
        return StructuredResponse({"code": VALID_PROTOCOL if len(prompts) == 1 else REVISED_PROTOCOL},
                                  {"model": "local-qwen"})

    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    result = await adapter.generate_ot2_protocol(
        "Transfer two sample wells with clean tips.", tmp_path,
        completion=completion, event_reader=lambda _: next(logs), repair_attempts=1,
    )
    assert result.attempts == 2
    assert "A1 of plate on 1" in prompts[1][-1]["content"]
    assert "B1 of plate on 1" in prompts[1][-1]["content"]
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert [attempt["event_validation"] for attempt in trace["attempts"]] == ["failed", "passed"]
    assert trace["event_validation_passed"] is True


@pytest.mark.asyncio
async def test_event_failure_accepts_local_line_patch_and_keeps_original(tmp_path, monkeypatch):
    bad_log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="plate on 1", well="A1"),
        _event("aspirate", labware="plate on 1", well="B1"),
        _event("drop"),
    ], {"plate on 1": "corning_96_wellplate_360ul_flat"})
    good_log = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="plate on 1", well="A1"),
        _event("drop"),
        _event("pick"),
        _event("aspirate", labware="plate on 1", well="B1"),
        _event("drop"),
    ], bad_log.labware)
    logs = iter([bad_log, good_log])
    calls = []

    async def completion(messages, schema, **kwargs):
        calls.append("draft")
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    async def patch_completion(messages, schema, **kwargs):
        calls.append("patch")
        assert kwargs["schema_name"] == "ot2_line_repair"
        return StructuredResponse({"edits": [
            {"start_line": 1, "end_line": 0, "replacement": "# localized Qwen edit"},
        ]}, {"model": "local-qwen", "elapsed_s": 0.1})

    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    result = await adapter.generate_ot2_protocol(
        "Transfer two sample wells with clean tips.", tmp_path,
        completion=completion, patch_completion=patch_completion,
        event_reader=lambda _: next(logs), repair_attempts=0, patch_attempts=1,
    )
    assert calls == ["draft", "patch"]
    assert result.attempts == 1
    assert (tmp_path / "candidate_attempt_1.py").read_text() == VALID_PROTOCOL
    assert (tmp_path / "candidate_attempt_1_patch_1.py").is_file()
    assert (tmp_path / "patch_attempt_1_1.json").is_file()
    assert result.protocol_path.read_text().startswith("# localized Qwen edit\n")
    trace = json.loads(result.trace_path.read_text())
    assert trace["event_validation_passed"] is True
    assert trace["attempts"][0]["accepted_via_patch"] is True
    assert trace["attempts"][0]["patches"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_simulator_failure_accepts_bounded_line_patch_and_preserves_rejected_candidate(
    tmp_path, monkeypatch,
):
    calls = []

    async def completion(messages, schema, **kwargs):
        calls.append("draft")
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    async def patch_completion(messages, schema, **kwargs):
        calls.append("patch")
        assert "simulator rejected original" in messages[-1]["content"]
        return StructuredResponse({"edits": [
            {"start_line": 1, "end_line": 0, "replacement": "# simulator repair from local Qwen"},
        ]}, {"model": "local-qwen", "elapsed_s": 0.2})

    simulations = []

    def fake_simulate(path, **kwargs):
        simulations.append(path.name)
        if "# simulator repair" in path.read_text():
            return adapter.SimulationResult("passed", "simulator accepted repair")
        return adapter.SimulationResult("failed", "simulator rejected original")

    monkeypatch.setattr(adapter, "simulate_protocol", fake_simulate)
    result = await adapter.generate_ot2_protocol(
        "Transfer 100 uL from a reservoir to twelve wells.", tmp_path,
        completion=completion, patch_completion=patch_completion,
        event_reader=lambda _: adapter.EventLog([], {}), repair_attempts=0, patch_attempts=1,
    )
    assert calls == ["draft", "patch"]
    assert simulations == ["candidate_attempt_1.py", "candidate_attempt_1_patch_1.py"]
    assert result.simulation.status == "passed"
    assert (tmp_path / "candidate_attempt_1.py").read_text() == VALID_PROTOCOL
    assert (tmp_path / "candidate_attempt_1_patch_1.py").is_file()
    assert (tmp_path / "patch_attempt_1_1.json").is_file()
    assert result.protocol_path.read_text().startswith("# simulator repair from local Qwen\n")
    trace = json.loads(result.trace_path.read_text())
    assert trace["event_validation_passed"] is True
    assert trace["attempts"][0]["patches"][0]["failure_stage"] == "simulator"
    assert trace["attempts"][0]["patches"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_simulator_patch_cannot_silently_change_volume(tmp_path, monkeypatch):
    async def completion(messages, schema, **kwargs):
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    transfer_line = next(index for index, line in enumerate(VALID_PROTOCOL.splitlines(), 1)
                         if "pipette.transfer(100" in line)

    async def patch_completion(messages, schema, **kwargs):
        return StructuredResponse({"edits": [{
            "start_line": transfer_line,
            "end_line": transfer_line,
            "replacement": "        pipette.transfer(200, source['A1'], well, new_tip='always')",
        }]}, {"model": "local-qwen"})

    simulated = []

    def fake_simulate(path, **kwargs):
        simulated.append(path.name)
        return adapter.SimulationResult("failed", "simulator rejected original")

    monkeypatch.setattr(adapter, "simulate_protocol", fake_simulate)
    with pytest.raises(adapter.GenerationError):
        await adapter.generate_ot2_protocol(
            "Transfer 100 uL from a reservoir to twelve wells.", tmp_path,
            completion=completion, patch_completion=patch_completion,
            event_reader=lambda _: adapter.EventLog([], {}), repair_attempts=0, patch_attempts=1,
        )
    assert simulated == ["candidate_attempt_1.py"]
    assert (tmp_path / "candidate_attempt_1.py").read_text() == VALID_PROTOCOL
    assert not (tmp_path / "protocol.py").exists()
    trace = json.loads((tmp_path / "generation_trace.json").read_text())
    assert trace["attempts"][0]["patches"][0]["status"] == "task_facts_rejected"
    assert trace["event_validation_passed"] is False


@pytest.mark.asyncio
async def test_line_patch_cannot_change_existing_liquid_actions(tmp_path, monkeypatch):
    baseline = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="plate on 1", well="A1"),
        _event("aspirate", labware="plate on 1", well="B1"),
        _event("drop"),
    ], {"plate on 1": "corning_96_wellplate_360ul_flat"})
    changed = adapter.EventLog([
        _event("pick"),
        _event("aspirate", labware="plate on 1", well="A1"),
        _event("drop"),
    ], baseline.labware)
    logs = iter([baseline, changed])

    async def completion(messages, schema, **kwargs):
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    async def patch_completion(messages, schema, **kwargs):
        return StructuredResponse({"edits": [
            {"start_line": 1, "end_line": 0, "replacement": "# localized Qwen edit"},
        ]}, {"model": "local-qwen"})

    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    with pytest.raises(adapter.GenerationError):
        await adapter.generate_ot2_protocol(
            "Transfer two sample wells with clean tips.", tmp_path,
            completion=completion, patch_completion=patch_completion,
            event_reader=lambda _: next(logs), repair_attempts=0, patch_attempts=1,
        )
    trace = json.loads((tmp_path / "generation_trace.json").read_text())
    assert trace["event_validation_passed"] is False
    assert trace["attempts"][0]["patches"][0]["status"] == "task_facts_rejected"
    assert (tmp_path / "candidate_attempt_1.py").is_file()
    assert not (tmp_path / "protocol.py").exists()


@pytest.mark.asyncio
async def test_candidate_error_from_event_logger_is_repaired(tmp_path, monkeypatch):
    prompts = []
    calls = 0

    async def completion(messages, schema, **kwargs):
        prompts.append(messages)
        return StructuredResponse({"code": VALID_PROTOCOL if len(prompts) == 1 else REVISED_PROTOCOL},
                                  {"model": "local-qwen"})

    def event_reader(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise adapter.EventSimulationError("Structured Opentrons simulation failed: invalid well mapping")
        return adapter.EventLog([], {})

    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    result = await adapter.generate_ot2_protocol(
        "Transfer samples.", tmp_path, completion=completion, event_reader=event_reader,
        repair_attempts=1,
    )
    assert result.attempts == 2
    assert "invalid well mapping" in prompts[1][-1]["content"]
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["attempts"][0]["event_validation"] == "failed"
    assert "invalid well mapping" in trace["attempts"][0]["event_detail"]


@pytest.mark.asyncio
async def test_missing_structured_event_logger_fails_closed(tmp_path, monkeypatch):
    async def completion(messages, schema, **kwargs):
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    monkeypatch.setattr(adapter, "simulate_protocol", lambda *args, **kwargs: adapter.SimulationResult("passed", "ok"))
    with pytest.raises(adapter.GenerationError, match="structured event logger is required"):
        await adapter.generate_ot2_protocol("Transfer samples.", tmp_path, completion=completion)
    assert not (tmp_path / "protocol.py").exists()
    trace = json.loads((tmp_path / "generation_trace.json").read_text(encoding="utf-8"))
    assert trace["status"] == "failed"
    assert trace["static_validation_passed"] is False
    assert trace["event_validation_passed"] is False
    assert trace["attempts"][0]["simulation"] == "passed"
    assert trace["attempts"][0]["candidate_path"] == str(tmp_path / "candidate_attempt_1.py")
    assert (tmp_path / "candidate_attempt_1.py").read_text(encoding="utf-8") == VALID_PROTOCOL


@pytest.mark.asyncio
async def test_duplicate_rejected_code_skips_resimulation_and_demands_change(tmp_path, monkeypatch):
    prompts = []
    simulations = []
    error = "ProtocolCommandFailedError [line 9]: TipAttachedError: tip already attached"

    async def completion(messages, schema, **kwargs):
        prompts.append(messages)
        return StructuredResponse({"code": VALID_PROTOCOL}, {"model": "local-qwen"})

    def fake_simulate(path, **kwargs):
        simulations.append(path)
        return adapter.SimulationResult("failed", error)

    monkeypatch.setattr(adapter, "simulate_protocol", fake_simulate)
    with pytest.raises(adapter.GenerationError, match="byte-for-byte identical"):
        await adapter.generate_ot2_protocol(
            "Transfer samples.", tmp_path, completion=completion, repair_attempts=2,
        )
    assert len(prompts) == 3
    assert len(simulations) == 1
    assert "Specific correction" in prompts[1][-1]["content"]
    assert "`new_tip='never'`" in prompts[1][-1]["content"]
    assert "byte-for-byte identical" in prompts[2][-1]["content"]
    assert not (tmp_path / "protocol.py").exists()
    trace = json.loads((tmp_path / "generation_trace.json").read_text(encoding="utf-8"))
    assert [attempt["simulation"] for attempt in trace["attempts"]] == [
        "failed", "skipped_duplicate", "skipped_duplicate",
    ]
    for number, attempt in enumerate(trace["attempts"], start=1):
        candidate = tmp_path / f"candidate_attempt_{number}.py"
        assert candidate.read_text(encoding="utf-8") == VALID_PROTOCOL
        assert attempt["candidate_path"] == str(candidate)


def test_simulator_subprocess_does_not_receive_secrets_or_proxy_settings(tmp_path, monkeypatch):
    seen = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        assert command == ["/tmp/opentrons_simulate", str(tmp_path / "protocol.py")]
        return Completed()

    monkeypatch.setenv("OPENAI_API_KEY", "a secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("PYBRAVO_SAFE_VALUE", "still here")
    monkeypatch.setattr(adapter.shutil, "which", lambda _: "/tmp/opentrons_simulate")
    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    result = adapter.simulate_protocol(tmp_path / "protocol.py")
    assert result.status == "passed"
    assert "OPENAI_API_KEY" not in seen["env"]
    assert "HTTPS_PROXY" not in seen["env"]
    assert seen["env"]["PYBRAVO_SAFE_VALUE"] == "still here"


def test_custom_labware_directory_is_passed_to_both_simulators(tmp_path, monkeypatch):
    labware = tmp_path / "pinned_labware"
    labware.mkdir()
    logger = tmp_path / "official_runlog.py"
    logger.write_text("# test logger", encoding="utf-8")
    executable = tmp_path / "venv" / "bin" / "opentrons_simulate"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    executable.with_name("python").write_text("", encoding="utf-8")
    protocol = tmp_path / "candidate.py"
    protocol.write_text(VALID_PROTOCOL, encoding="utf-8")
    commands = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == str(executable.with_name("python")):
            (tmp_path / "seen_labware_dir.txt").write_text(command[-2], encoding="utf-8")
            from pathlib import Path

            Path(command[-1]).write_text(json.dumps({"ok": True, "events": [], "labware": {}}),
                                         encoding="utf-8")
        return Completed()

    monkeypatch.setattr(adapter.shutil, "which", lambda _: str(executable))
    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    simulation = adapter.simulate_protocol(protocol, simulator_command=executable, labware_dir=labware)
    event_log = adapter.record_simulation_events(
        protocol, event_logger_path=logger, simulator_command=executable, labware_dir=labware,
    )
    assert simulation.status == "passed"
    assert event_log.events == []
    assert commands[0] == [str(executable), "-L", str(labware), str(protocol)]
    assert (tmp_path / "seen_labware_dir.txt").read_text(encoding="utf-8") == str(labware)


@pytest.mark.asyncio
async def test_nonexistent_custom_labware_directory_fails_before_model_call(tmp_path):
    async def forbidden_completion(*args, **kwargs):
        raise AssertionError("Model called with invalid task setup")

    with pytest.raises(ValueError, match="Custom labware directory does not exist"):
        await adapter.generate_ot2_protocol(
            "Transfer samples.", tmp_path, labware_dir=tmp_path / "missing",
            completion=forbidden_completion,
        )
