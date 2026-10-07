"""The scientific line-repair experiment preserves fixed work and honest scoring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from pybravo.workflow.protocols.llm import StructuredResponse
from scripts import experiment_text2wetlab_science_patch as experiment

SOURCE = '''from opentrons import protocol_api
metadata = {"apiLevel": "2.15"}
def run(protocol: protocol_api.ProtocolContext):
    stock = protocol.load_labware("stock", 1, label="stock")
    plate = protocol.load_labware("plate", 2, label="plate")
    pipette = protocol.load_instrument("p20_single_gen2", "left")
    pipette.transfer(20, stock["A1"], plate["A1"])
'''


def _events(volume: int, *, destination: str = "A1") -> list[dict]:
    return [
        {"kind": "pick", "instrument": "p20"},
        {"kind": "aspirate", "instrument": "p20", "labware": "stock on 1",
         "well": "A1", "volume": volume},
        {"kind": "dispense", "instrument": "p20", "labware": "plate on 2",
         "well": destination, "volume": volume},
        {"kind": "drop", "instrument": "p20"},
    ]


def _audit(status: str) -> dict:
    return {"status": status, "official_score": None,
            "items": [{"checks": [{"name": "reaction total", "status": status,
                                   "evidence": "reagent total must match the source"}]}]}


def test_science_prompt_allows_grounded_volume_correction_without_exposing_rubric():
    messages = experiment.science_patch_messages(
        SOURCE, instruction="Prepare a 19 uL reaction on the fixed deck.",
        scientific_source="Methods: reaction volume is 19 uL.",
        diagnostic="Recheck the supplied method's reaction total.",
    )
    assert "Correct liquid quantities" in messages[0]["content"]
    assert "Preserve the task's fixed labware" in messages[0]["content"]
    assert "0007|" in messages[-1]["content"]
    assert "rubric id" not in json.dumps(messages).lower()


def test_fixed_facts_accept_source_supported_volume_but_reject_deck_or_mapping_change():
    revised = SOURCE.replace("transfer(20,", "transfer(19,")
    deck_change = revised.replace("label=\"plate\"", "label=\"other\"")
    baseline, candidate = _events(20), _events(19)
    labware = {"stock on 1": "stock", "plate on 2": "plate"}
    assert experiment._preserve_fixed_facts("golden-gate-assembly", SOURCE, baseline,
                                             revised, candidate, labware, labware) is None
    assert "fixed labware" in experiment._preserve_fixed_facts(
        "golden-gate-assembly", SOURCE, baseline, deck_change, candidate,
        labware, labware,
    )
    assert "mapping" in experiment._preserve_fixed_facts(
        "golden-gate-assembly", SOURCE, baseline, revised,
        _events(19, destination="B1"), labware, labware,
    )


@pytest.mark.asyncio
async def test_needs_review_candidate_is_saved_without_automatic_acceptance(tmp_path, monkeypatch):
    source_path = tmp_path / "input.py"
    source_path.write_text(SOURCE)
    output = tmp_path / "output"
    monkeypatch.setattr(experiment, "_pinned_sources",
                        lambda task, root, override: ("Prepare a 19 uL reaction.", None))

    def fake_check(task, source, path, task_dir, simulator, dataset_root, labware_dir, instruction):
        task_dir.mkdir(exist_ok=True)
        events_path = task_dir / "official_events.json"
        before = path == source_path
        events_path.write_text(json.dumps(_events(20 if before else 19)))
        labware = {"stock on 1": "stock", "plate on 2": "plate"}
        audit = _audit("failed" if before else "needs_review")
        return {"status": "mechanical_gates_passed", "local_rubric_audit": audit,
                "pinned_runlog": {"events_path": str(events_path), "labware": labware}}

    monkeypatch.setattr(experiment, "_check_candidate", fake_check)
    monkeypatch.setattr("pybravo.evals.text2wetlab.adapter._scientific_audit",
                        lambda *args: (None, "Recheck the supplied task's reaction total."))
    async def fake_model(messages, schema, **kwargs):
        assert kwargs["config"].base_url == "http://sparky.local:8000/v1"
        return StructuredResponse({"edits": [{"start_line": 7, "end_line": 7,
                                                "replacement": '    pipette.transfer(19, stock["A1"], plate["A1"])'}]},
                                  {"model": "qwen", "elapsed_s": 1, "usage": {}})

    monkeypatch.setattr(experiment, "structured_json", fake_model)
    args = argparse.Namespace(task="golden-gate-assembly", source=source_path,
                              simulator=tmp_path / "simulator", output_dir=output,
                              dataset_root=None, paper_override=None, max_attempts=1,
                              model_timeout=10, model_max_tokens=1024)
    trace = await experiment.run_experiment(args)
    assert trace["official_score"] is None
    assert trace["status"] == "improved_candidate_needs_review"
    assert trace["best_candidate"] == str(output / "attempt_1" / "candidate.py")
    assert "transfer(19" in (output / "attempt_1" / "candidate.py").read_text()
    assert trace["attempts"][0]["status"] == "scientist_review_required"


@pytest.mark.asyncio
async def test_unfailed_baseline_never_calls_model(tmp_path, monkeypatch):
    source_path = tmp_path / "input.py"
    source_path.write_text(SOURCE)
    monkeypatch.setattr(experiment, "_pinned_sources", lambda *args: ("Task", None))
    monkeypatch.setattr(experiment, "_check_candidate", lambda *args: {
        "status": "mechanical_gates_passed", "local_rubric_audit": _audit("needs_review")})
    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Qwen should not be called")
    monkeypatch.setattr(experiment, "structured_json", forbidden_model)
    args = argparse.Namespace(task="golden-gate-assembly", source=source_path,
                              simulator=tmp_path / "simulator", output_dir=tmp_path / "output",
                              dataset_root=None, paper_override=None, max_attempts=1,
                              model_timeout=10, model_max_tokens=1024)
    trace = await experiment.run_experiment(args)
    assert trace["status"] == "baseline_needs_review"
    assert trace["attempts"] == []


@pytest.mark.asyncio
async def test_repository_output_is_rejected_before_any_write(tmp_path):
    source_path = tmp_path / "input.py"
    source_path.write_text(SOURCE)
    output = experiment.__file__
    args = argparse.Namespace(task="golden-gate-assembly", source=source_path,
                              simulator=tmp_path / "simulator", output_dir=Path(output).parent,
                              dataset_root=None, paper_override=None, max_attempts=1,
                              model_timeout=10, model_max_tokens=1024)
    with pytest.raises(ValueError, match="outside the pyBravo repository"):
        await experiment.run_experiment(args)
