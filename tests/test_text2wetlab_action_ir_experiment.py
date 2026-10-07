"""Offline tests for the local-model ActionPlan experiment boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pybravo.evals.text2wetlab.trusted_catalog import (
    catalog_names,
    facts_from_definitions,
    load_trusted_module_catalog,
    module_deck_requests,
)
from scripts import experiment_text2wetlab_action_ir as experiment


def _definition(names: list[str], *, tiprack: bool, volume: int) -> dict:
    return {
        "ordering": [names],
        "wells": {name: {"x": 0, "y": 90 - 9 * index,
                         "totalLiquidVolume": volume}
                  for index, name in enumerate(names)},
        "parameters": {"isTiprack": tiprack},
    }


def test_catalog_comes_from_definitions_and_preserves_exact_wells():
    definitions = {
        "small_source": _definition(["A1", "B1"], tiprack=False, volume=100),
        "small_tiprack": _definition(["A1", "B1"], tiprack=True, volume=20),
        "eight_tiprack": _definition([f"{row}1" for row in "ABCDEFGH"],
                                     tiprack=True, volume=300),
    }
    labware, pipettes = facts_from_definitions(
        definitions,
        {"small_pipette": {"minVolume": 1, "maxVolume": 20, "channels": 1}},
    )
    assert catalog_names("Use `small_source`, `small_tiprack`, and `A1`.") == (
        "small_source", "small_tiprack",
    )
    assert labware["small_source"].wells == {"A1", "B1"}
    assert labware["small_tiprack"].tip_capacity_ul == 20
    assert labware["eight_tiprack"].multichannel_anchor_wells == {"A1"}
    assert pipettes["small_pipette"].min_volume_ul == 1


def test_long_trough_requires_centering_quirk_and_tip_clearance():
    tiprack = _definition([f"{row}1" for row in "ABCDEFGH"],
                          tiprack=True, volume=200)
    for details in tiprack["wells"].values():
        details["diameter"] = 5.23
    trough = _definition(["A1", "A2"], tiprack=False, volume=15000)
    trough["parameters"].update({"format": "trough",
                                 "quirks": ["centerMultichannelOnWells"]})
    for details in trough["wells"].values():
        details.update(shape="rectangular", xDimension=8.2, yDimension=71.2)
    labware, _ = facts_from_definitions(
        {"tips": tiprack, "long_trough": trough}, {},
    )
    assert labware["long_trough"].multichannel_anchor_wells == {"A1", "A2"}
    trough["parameters"]["quirks"] = []
    labware, _ = facts_from_definitions({"tips": tiprack, "long_trough": trough}, {})
    assert not labware["long_trough"].multichannel_compatible
    trough["parameters"]["quirks"] = ["centerMultichannelOnWells"]
    trough["wells"]["A1"]["yDimension"] = 65
    labware, _ = facts_from_definitions({"tips": tiprack, "long_trough": trough}, {})
    assert labware["long_trough"].multichannel_anchor_wells == {"A2"}


def test_module_requests_restrict_alias_slots_and_labware_to_fixed_deck():
    instruction = """| 4 | Magnetic Module GEN1 (`'magnetic module'`) holding sample plate | `deep_plate` |
| 6 | Temperature Module GEN1 (`'tempdeck'`) holding elution plate | `cold_plate` |
| 7, 8, 10, 11 | Thermocycler Module GEN1 (`'thermocycler'`) holding `pcr_plate` | label |
The paper also mentions a magnetic module without a deck row.
"""
    assert module_deck_requests(
        instruction, known_labware={"deep_plate", "cold_plate", "pcr_plate"},
    ) == [
        {"alias": "magnetic module", "expected_slots": [4],
         "labware_load_names": ["deep_plate"]},
        {"alias": "tempdeck", "expected_slots": [6],
         "labware_load_names": ["cold_plate"]},
        {"alias": "thermocycler", "expected_slots": [7, 8, 10, 11],
         "labware_load_names": ["pcr_plate"]},
    ]


def test_module_preflight_accepts_only_exact_reported_footprint(tmp_path, monkeypatch):
    simulator = tmp_path / "opentrons_simulate"
    simulator.touch()
    (tmp_path / "python").touch()
    source = "| 4 | Magnetic Module GEN1 (`'magnetic module'`) | `deep_plate` |\n"
    calls: list[dict] = []

    def report(*args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "magnetic module": {
                "kind": "magnetic", "physical_model": "magneticModuleV1",
                "occupied_slots": [4], "slot": 4,
                "compatible_labware": ["deep_plate"],
                "temperature_range_c": None, "lid_temperature_range_c": None,
                "magnet_height_range_mm": [0, 20],
            },
        }))

    from pybravo.evals.text2wetlab import trusted_catalog

    monkeypatch.setattr(trusted_catalog.subprocess, "run", report)
    facts = load_trusted_module_catalog(
        source, simulator=simulator,
        labware={"deep_plate": object()},
    )
    assert calls[0]["requests"][0]["expected_slots"] == [4]
    assert facts["magnetic module"].allowed_slots == {4}
    assert facts["magnetic module"].magnet_height_range_mm == (0, 20)
    assert facts["magnetic module"].compatible_labware_load_names == {"deep_plate"}

    def wrong_footprint(*args, **kwargs):
        result = report(*args, **kwargs)
        output = json.loads(result.stdout)
        output["magnetic module"]["occupied_slots"] = [5]
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(output))

    monkeypatch.setattr(trusted_catalog.subprocess, "run", wrong_footprint)
    assert load_trusted_module_catalog(
        source, simulator=simulator, labware={"deep_plate": object()},
    ) == {}


def test_exact_source_line_ids_reject_forged_evidence():
    source = "Use source A1.\n\nTransfer 5 uL to target B1.\n"
    spans, display = experiment._line_spans(source, None)
    assert set(spans) == {"task.L1", "task.L2"}
    assert source[spans["task.L2"].start:spans["task.L2"].end] == (
        "Transfer 5 uL to target B1."
    )
    assert "[task.L2] Transfer 5 uL" in display


def test_deck_slot_canonicalization_is_narrow_and_recorded():
    original = {"labware": [{"slot": "1"}, {"slot": "08"}, {"slot": "12"}],
                "modules": [{"slot": "11"}], "actions": [{"volume_ul": "5"}]}
    canonical, changes = experiment._canonicalize_deck_slots(original)
    assert original["labware"][0]["slot"] == "1"
    assert [entry["slot"] for entry in canonical["labware"]] == [1, "08", "12"]
    assert canonical["modules"][0]["slot"] == 11
    assert canonical["actions"][0]["volume_ul"] == "5"
    assert changes == ["/labware/0/slot: '1' -> 1", "/modules/0/slot: '11' -> 11"]


def test_synthetic_action_plan_compiles_before_existing_checks(tmp_path, monkeypatch):
    labware, pipettes = facts_from_definitions(
        {
            "synthetic_source": _definition(["A1"], tiprack=False, volume=100),
            "synthetic_target": _definition(["B1"], tiprack=False, volume=100),
            "synthetic_tiprack": _definition(["A1"], tiprack=True, volume=20),
        },
        {"synthetic_p20": {"minVolume": 1, "maxVolume": 20, "channels": 1}},
    )
    instruction = "Synthetic instruction: transfer 5 uL from source A1 to target B1."
    spans, _ = experiment._line_spans(instruction, None)
    payload = {
        "labware": [
            {"id": "tips", "load_name": "synthetic_tiprack", "slot": 1,
             "label": "tips"},
            {"id": "source", "load_name": "synthetic_source", "slot": 2,
             "label": "source"},
            {"id": "target", "load_name": "synthetic_target", "slot": 3,
             "label": "target"},
        ],
        "pipettes": [{"id": "pip", "model": "synthetic_p20", "mount": "left",
                      "tip_rack_ids": ["tips"]}],
        "actions": [
            {"kind": "pickup", "pipette": "pip", "evidence_refs": ["task.L1"]},
            {"kind": "aspirate", "pipette": "pip", "labware": "source",
             "well": "A1", "volume_ul": 5, "evidence_refs": ["task.L1"]},
            {"kind": "dispense", "pipette": "pip", "labware": "target",
             "well": "B1", "volume_ul": 5, "evidence_refs": ["task.L1"]},
            {"kind": "drop", "pipette": "pip", "evidence_refs": ["task.L1"]},
        ],
    }
    called: list[str] = []
    monkeypatch.setattr(experiment, "labware_geometry_context", lambda *a, **k: {})

    def fake_gate(task, code, path, directory, simulator, root, labware_dir, source):
        called.append(code)
        assert Path(path).is_file()
        assert "label='source'" in code
        assert "label='target'" in code
        return {"status": "mechanical_gates_passed",
                "local_rubric_audit": {"checks": []}}

    monkeypatch.setattr(experiment, "_check_candidate", fake_gate)
    result = experiment._check_compiled_plan(
        payload, instruction=instruction, paper=None, spans=spans,
        labware=labware, pipettes=pipettes, simulator=tmp_path / "simulator",
        task="synthetic", task_dir=tmp_path, dataset_root=None,
        labware_dir=None, refill_authorized=False,
    )
    assert result["status"] == "mechanical_gates_passed"
    assert len(called) == 1
    assert result["official_score"] is None

    payload["actions"][1]["volume_ul"] = 21
    result = experiment._check_compiled_plan(
        payload, instruction=instruction, paper=None, spans=spans,
        labware=labware, pipettes=pipettes, simulator=tmp_path / "simulator",
        task="synthetic", task_dir=tmp_path, dataset_root=None,
        labware_dir=None, refill_authorized=False,
    )
    assert result["status"] == "action_plan_rejected"
    assert len(called) == 1

    payload["actions"][1]["volume_ul"] = 5
    monkeypatch.setattr(experiment, "_check_candidate", lambda *a, **k: {
        "status": "mechanical_gates_passed",
        "local_rubric_audit": {"status": "failed", "items": [
            {"id": "volume", "checks": [{"name": "wrong amount", "status": "failed"}]},
        ]},
    })
    result = experiment._check_compiled_plan(
        payload, instruction=instruction, paper=None, spans=spans,
        labware=labware, pipettes=pipettes, simulator=tmp_path / "simulator",
        task="synthetic", task_dir=tmp_path, dataset_root=None,
        labware_dir=None, refill_authorized=False,
    )
    assert result["status"] == "local_science_failed"
    assert result["failed_observable_science_checks"] == [
        {"rubric_item": "volume", "check": "wrong amount"},
    ]
