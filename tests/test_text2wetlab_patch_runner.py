"""Saved-candidate line repairs preserve original files and re-run every gate."""

from __future__ import annotations

import argparse

import pytest

from pybravo.evals.text2wetlab.adapter import EventLog, SimulationResult
from pybravo.workflow.protocols.llm import StructuredResponse
from scripts import experiment_text2wetlab_patch_repair as experiment

SOURCE = '''from opentrons import protocol_api
metadata = {"apiLevel": "2.15"}
def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_96_tiprack_20ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    pipette = protocol.load_instrument("p20_single_gen2", "left", tip_racks=[tips])
    pipette.pick_up_tip()
    pipette.aspirate(5, plate["A1"])
    pipette.dispense(5, plate["A2"])
    pipette.drop_tip()
'''


@pytest.mark.asyncio
async def test_saved_simulator_failure_uses_local_patch_and_preserves_original(tmp_path, monkeypatch):
    source = tmp_path / "candidate_attempt_1.py"
    source.write_text(SOURCE)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Move 5 uL from A1 to A2.")
    output = tmp_path / "experiment"
    args = argparse.Namespace(
        source=source, instruction=instruction, paper=None, event_logger=tmp_path / "runlog.py",
        simulator=tmp_path / "opentrons_simulate", labware_dir=None,
        output_dir=output, max_attempts=1, model_timeout=30,
    )

    def simulate(path, **kwargs):
        return (SimulationResult("passed", "ok") if "# Qwen simulator fix" in path.read_text()
                else SimulationResult("failed", "original simulator error"))

    async def local_patch(messages, schema, **kwargs):
        assert "original simulator error" in messages[-1]["content"]
        return StructuredResponse({"edits": [
            {"start_line": 1, "end_line": 0, "replacement": "# Qwen simulator fix"},
        ]}, {"model": "local-qwen", "elapsed_s": 0.1})

    monkeypatch.setattr(experiment, "simulate_protocol", simulate)
    monkeypatch.setattr(experiment, "record_simulation_events", lambda *a, **k: EventLog([], {}))
    monkeypatch.setattr(experiment, "structured_json", local_patch)
    result = await experiment.run_experiment(args)
    assert result["baseline_failure"]["stage"] == "simulator"
    assert result["status"] == "passed_local_gates"
    assert (output / "original_candidate.py").read_text() == SOURCE
    assert source.read_text() == SOURCE
    assert (output / "patch_attempt_1.json").is_file()
    assert (output / "candidate_patch_1.py").read_text().startswith("# Qwen simulator fix\n")
    assert result["official_score"] is None


@pytest.mark.asyncio
async def test_saved_candidate_with_new_static_error_is_repaired_before_simulation(tmp_path, monkeypatch):
    invalid = SOURCE.replace('    pipette.drop_tip()', '    pipette.mix(2, 5)\n    pipette.drop_tip()')
    source = tmp_path / "candidate_attempt_1.py"
    source.write_text(invalid)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Move 5 uL from A1 to A2, then mix there.")
    output = tmp_path / "experiment"
    args = argparse.Namespace(
        source=source, instruction=instruction, paper=None, event_logger=tmp_path / "runlog.py",
        simulator=tmp_path / "opentrons_simulate", labware_dir=None,
        output_dir=output, max_attempts=1, model_timeout=30,
    )
    mix_line = next(index for index, line in enumerate(invalid.splitlines(), 1)
                    if "pipette.mix" in line)
    simulated = []

    async def local_patch(messages, schema, **kwargs):
        assert "mix() must name" in messages[-1]["content"]
        return StructuredResponse({"edits": [{
            "start_line": mix_line,
            "end_line": mix_line,
            "replacement": '    pipette.mix(2, 5, plate["A2"])',
        }]}, {"model": "local-qwen"})

    def simulate(path, **kwargs):
        simulated.append(path)
        return SimulationResult("passed", "ok")

    monkeypatch.setattr(experiment, "simulate_protocol", simulate)
    monkeypatch.setattr(experiment, "record_simulation_events", lambda *a, **k: EventLog([], {}))
    monkeypatch.setattr(experiment, "structured_json", local_patch)
    result = await experiment.run_experiment(args)
    assert result["baseline_failure"]["stage"] == "static"
    assert result["status"] == "passed_local_gates"
    assert simulated == [output / "candidate_patch_1.py"]
    assert (output / "original_candidate.py").read_text() == invalid


@pytest.mark.asyncio
async def test_patch_runner_uses_expanded_local_output_budget(tmp_path, monkeypatch):
    source = tmp_path / "candidate.py"
    source.write_text(SOURCE)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Move 5 uL from A1 to A2.")
    args = argparse.Namespace(
        source=source, instruction=instruction, paper=None,
        event_logger=tmp_path / "runlog.py", simulator=tmp_path / "opentrons_simulate",
        labware_dir=None, output_dir=tmp_path / "experiment", max_attempts=1,
        model_timeout=30, model_max_tokens=4096,
    )

    async def local_patch(messages, schema, *, config, **kwargs):
        assert config.max_tokens == 4096
        return StructuredResponse({"edits": [
            {"start_line": 1, "end_line": 0, "replacement": "# Qwen simulator fix"},
        ]}, {"model": "local-qwen"})

    monkeypatch.setattr(experiment, "simulate_protocol", lambda path, **kwargs: (
        SimulationResult("passed", "ok") if "Qwen simulator fix" in path.read_text()
        else SimulationResult("failed", "baseline error")
    ))
    monkeypatch.setattr(experiment, "record_simulation_events", lambda *a, **k: EventLog([], {}))
    monkeypatch.setattr(experiment, "structured_json", local_patch)
    result = await experiment.run_experiment(args)
    assert result["status"] == "passed_local_gates"
