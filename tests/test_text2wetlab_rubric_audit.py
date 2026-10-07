"""Behavioral checks for the local, non-official Text2WetLab rubric audit."""

from __future__ import annotations

from pybravo.evals.text2wetlab.rubric_audit import RUBRIC_IDS, audit_rubric_coverage


def _transfer(events: list[dict], source: tuple[str, str], destination: tuple[str, str],
              volume: float, *, instrument: str = "P20 Single-Channel GEN2 on left mount",
              channels: int = 1) -> None:
    common = {"instrument": instrument, "channels": channels}
    events.extend([
        {"kind": "pick", **common},
        {"kind": "aspirate", "volume": volume, "labware": source[0], "well": source[1], **common},
        {"kind": "dispense", "volume": volume, "labware": destination[0], "well": destination[1], **common},
        {"kind": "drop", **common},
    ])


def _item(result: dict, rubric_id: str) -> dict:
    return next(item for item in result["items"] if item["id"] == rubric_id)


def test_every_pinned_task_has_five_audited_items_and_no_official_score() -> None:
    assert len(RUBRIC_IDS) == 7
    for task, ids in RUBRIC_IDS.items():
        assert len(ids) == 5
        result = audit_rubric_coverage(task, [], "metadata = {'apiLevel': '2.15'}\n")
        assert result["official_score"] is None
        assert result["metric"] == "local_rubric_coverage_audit"
        assert [item["id"] for item in result["items"]] == list(ids)


def test_colony_audit_catches_wrong_2x_dilution_even_with_complete_well_mapping() -> None:
    events: list[dict] = []
    colony = "colony_plate on 1"
    pcr = "pcr_plate on 2"
    mix = "master_mix_reservoir on 3"
    primers = "primer_plate on 4"
    for column in range(1, 13):
        for row in "ABCDEFGH":
            well = f"{row}{column}"
            _transfer(events, (mix, "A1"), (pcr, well), 5)
            _transfer(events, (primers, well), (pcr, well), 1)
            _transfer(events, (colony, well), (pcr, well), 1)
    source = """\
metadata = {'apiLevel': '2.15'}
def run(protocol):
    protocol.comment('Seal the PCR plate, thermocycle with 98 C initial denaturation, 30 cycles, final extension, hold 4 C.')
"""
    result = audit_rubric_coverage("colony-pcr-screening", events, source)
    assert _item(result, "reaction_setup")["status"] == "failed"
    assert _item(result, "sample_mapping")["status"] == "supported"
    assert _item(result, "tips_and_contamination")["status"] == "supported"
    assert _item(result, "thermocycling")["status"] == "needs_review"
    assert result["official_score"] is None


def test_golden_gate_distinguishes_25_ul_pcr_from_later_dpni_additions() -> None:
    events: list[dict] = []
    water = "tubes_50ml_1 on 1"
    reagents = "tubes_1_5ml_1 on 2"
    primers = "primer_plate on 3"
    templates = "template_plate on 4"
    pcr = "pcr_plate on 5"
    assembly = "assembly_plate on 6"
    fragment_specs = {
        "A1": ("A1", "A1", "A2"), "B1": ("A1", "B1", "B2"),
        "C1": ("B1", "C1", "C2"), "D1": ("C1", "D1", "D2"),
        "E1": ("A1", "E1", "E2"), "F1": ("A1", "F1", "F2"),
        "G1": ("D1", "G1", "G2"),
    }
    for well, (template, forward, reverse) in fragment_specs.items():
        _transfer(events, (reagents, "D1"), (pcr, well), 19)
        _transfer(events, (primers, forward), (pcr, well), 2.5)
        _transfer(events, (primers, reverse), (pcr, well), 2.5)
        _transfer(events, (templates, template), (pcr, well), 1)
    for well in fragment_specs:
        _transfer(events, (water, "A1"), (pcr, well), 19)
        _transfer(events, (reagents, "A2"), (pcr, well), 5)
        _transfer(events, (reagents, "B2"), (pcr, well), 1)
    designs = {
        "A1": {"B1": 3, "E1": 2, "F1": 3, "A1": 2},
        "B1": {"B1": 3, "E1": 2, "F1": 3, "C1": 2},
        "C1": {"B1": 3, "E1": 2, "F1": 3, "D1": 2},
        "D1": {"B1": 3, "E1": 2, "F1": 3, "G1": 2},
    }
    for well, fragments in designs.items():
        for fragment, volume in fragments.items():
            _transfer(events, (pcr, fragment), (assembly, well), volume)
        _transfer(events, (reagents, "C2"), (assembly, well), 2)
        _transfer(events, (reagents, "D2"), (assembly, well), 1)
        _transfer(events, (water, "A1"), (assembly, well), 7)
    for row in "ABCD":
        well = f"{row}1"
        _transfer(events, (assembly, well), ("cells_plate on 7", well), 5)
        _transfer(events, ("tubes_15ml_1 on 8", "A1"), ("cells_plate on 7", well), 250,
                  instrument="P300 Single-Channel GEN2 on right mount")
    source = """\
metadata = {'apiLevel': '2.15'}
def run(protocol):
    protocol.comment('Gradient PCR: 98 C 30 s, 35 cycles, then 72 C 5 min.')
    protocol.comment('DpnI digest at 37 C 30 min, then 65 C 20 min.')
    protocol.comment('Column clean-up of all seven fragments.')
    protocol.comment('Golden Gate program 37 C / 16 C, final heat and 4 C hold.')
    protocol.comment('TOP10 transformation: heat shock at 42 C.')
    protocol.comment('Plate on kanamycin LB agar.')
"""
    result = audit_rubric_coverage("golden-gate-assembly", events, source)
    assert _item(result, "pcr_setup")["checks"][0]["status"] == "supported"
    assert _item(result, "dpni_and_cleanup")["checks"][0]["status"] == "supported"
    assert _item(result, "assembly_mix")["status"] == "supported"
    assert _item(result, "cycling_and_transformation")["status"] == "needs_review"


def test_rna_expands_six_eight_channel_elutions_to_48_matched_wells() -> None:
    events: list[dict] = []
    extraction = "USA Scientific 96 Deep Well Plate on Magnetic Module GEN1 on 4"
    elution = "Plate_thermo_96_elutions on Temperature Module GEN1 on 6"
    for column in (1, 3, 5, 7, 9, 11):
        source_column = (column + 1) // 2
        for offset, row in enumerate("ABCDEFGH"):
            rack_slot = 10 if offset < 4 else 7
            rack_row = "ABCD"[offset % 4]
            rack = f"Opentrons 24 Tube Rack on {rack_slot}"
            _transfer(events, (rack, f"{rack_row}{source_column}"), (extraction, f"{row}{column}"), 250,
                      instrument="P1000 Single-Channel GEN2 on left mount")
    events.append({"kind": "temp", "instrument": "", "channels": 1, "celsius": 4.0})
    for column in (1, 3, 5, 7, 9, 11):
        _transfer(events, (extraction, f"A{column}"), (elution, f"A{column}"), 80,
                  instrument="P300 8-Channel GEN2 on right mount", channels=8)
    result = audit_rubric_coverage("opentrons-rna-extraction", events, "metadata = {'apiLevel': '2.15'}\n")
    assert _item(result, "sample_handling")["status"] == "supported"
    assert _item(result, "elution_recovery")["checks"][1]["status"] == "supported"
    assert _item(result, "elution_recovery")["status"] == "failed"  # no off-magnet 100 µL elution
    assert result["official_score"] is None
