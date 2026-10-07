"""The OT-2 benchmark adapter must reject unsafe code before simulation."""

from __future__ import annotations

import json

import pytest

from pybravo.evals.text2wetlab import adapter
from pybravo.workflow.protocols.llm import StructuredResponse

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
