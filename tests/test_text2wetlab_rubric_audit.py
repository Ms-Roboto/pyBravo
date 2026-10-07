"""Behavioral checks for the local, non-official Text2WetLab rubric audit."""

from __future__ import annotations

from pybravo.evals.text2wetlab.rubric_audit import (
    RUBRIC_IDS,
    Transfer,
    _source_facts,
    _tip_isolation,
    audit_rubric_coverage,
)


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


def _check(result: dict, rubric_id: str, name: str) -> dict:
    return next(check for check in _item(result, rubric_id)["checks"] if check["name"] == name)


def test_every_pinned_task_has_five_audited_items_and_no_official_score() -> None:
    assert len(RUBRIC_IDS) == 7
    for task, ids in RUBRIC_IDS.items():
        assert len(ids) == 5
        result = audit_rubric_coverage(task, [], "metadata = {'apiLevel': '2.15'}\n")
        assert result["official_score"] is None
        assert result["metric"] == "local_rubric_coverage_audit"
        assert [item["id"] for item in result["items"]] == list(ids)


def test_tip_isolation_distinguishes_same_well_name_on_two_sample_racks() -> None:
    transfers = [
        Transfer(1, "Opentrons 24 Tube Rack on 7", "A1", "Deep well plate on 4",
                 "A1", 250, "P1000 Single on left mount", 1),
        Transfer(2, "Opentrons 24 Tube Rack on 10", "A1", "Deep well plate on 4",
                 "B1", 250, "P1000 Single on left mount", 1),
    ]
    assert not _tip_isolation("opentrons-rna-extraction", transfers, {"samples"})


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
    assert _check(result, "sample_handling", "48 one-to-one 250 µL samples")["status"] == "supported"
    assert _check(result, "elution_recovery", "matched recovery into 4 °C plate")["status"] == "supported"
    assert _item(result, "elution_recovery")["status"] == "failed"  # no off-magnet 100 µL elution
    assert result["official_score"] is None


def test_source_facts_resolve_string_slots_and_module_labware() -> None:
    source = """\
def run(protocol):
    protocol.load_labware('nest_12_reservoir_15ml', '5')
    mag = protocol.load_module('magnetic module', '4')
    mag.load_labware('usascientific_96_wellplate_2.4ml_deep')
    temp = protocol.load_module('tempdeck', '6')
    temp.load_labware('thermo_96_wellplate_200ul')
"""
    loads, _, _, error = _source_facts(source)
    assert error is None
    assert {("nest_12_reservoir_15ml", 5), ("module:magnetic module", 4),
            ("usascientific_96_wellplate_2.4ml_deep", 4), ("module:tempdeck", 6),
            ("thermo_96_wellplate_200ul", 6)} <= {(name, slot) for name, slot, _ in loads}


def test_rna_fixed_deck_check_detects_missing_rack_without_relying_on_labels() -> None:
    source = """\
def run(protocol):
    protocol.load_labware('usascientific_96_wellplate_2.4ml_deep', '1')
    protocol.load_labware('opentrons_96_filtertiprack_200ul', '2')
    protocol.load_labware('opentrons_96_filtertiprack_200ul', '3')
    mag = protocol.load_module('magnetic module', '4')
    mag.load_labware('usascientific_96_wellplate_2.4ml_deep')
    protocol.load_labware('nest_12_reservoir_15ml', '5')
    temp = protocol.load_module('tempdeck', '6')
    temp.load_labware('thermo_96_wellplate_200ul')
    protocol.load_labware('opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap', '7')
    protocol.load_labware('opentrons_96_filtertiprack_200ul', '9')
    protocol.load_labware('opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap', '10')
    protocol.load_labware('opentrons_96_filtertiprack_1000ul', '11')
"""
    task = "opentrons-rna-extraction"
    assert _check(audit_rubric_coverage(task, [], source),
                  "sample_handling", "fixed deck and modules")["status"] == "supported"
    missing = source.replace("protocol.load_labware('opentrons_96_filtertiprack_200ul', '9')", "")
    assert _check(audit_rubric_coverage(task, [], missing),
                  "sample_handling", "fixed deck and modules")["status"] == "failed"


def test_rna_binding_and_settling_delays_must_bracket_sample_and_removal() -> None:
    source = "metadata = {'apiLevel': '2.15'}\n"
    sample = ("Opentrons 24 Tube Rack on 10", "A1")
    extraction = ("USA Scientific 96 Deep Well Plate on Magnetic Module GEN1 on 4", "A1")
    waste = ("USA Scientific 96 Deep Well Plate on 1", "A1")
    sample_events: list[dict] = []
    _transfer(sample_events, sample, extraction, 250,
              instrument="P1000 Single-Channel GEN2 on left mount")
    removal_events: list[dict] = []
    _transfer(removal_events, extraction, waste, 500,
              instrument="P1000 Single-Channel GEN2 on left mount")
    wrong = ([{"kind": "delay", "seconds": 300}, *sample_events,
              {"kind": "engage"}, *removal_events, {"kind": "delay", "seconds": 240}])
    right = ([*sample_events, {"kind": "delay", "seconds": 300},
              {"kind": "engage"}, {"kind": "delay", "seconds": 240}, *removal_events])
    name = "binding/settling waits and magnetic removal"
    assert _check(audit_rubric_coverage("opentrons-rna-extraction", wrong, source),
                  "binding_and_separation", name)["status"] == "failed"
    assert _check(audit_rubric_coverage("opentrons-rna-extraction", right, source),
                  "binding_and_separation", name)["status"] == "supported"


def test_rna_requires_initial_supernatant_removal_before_first_wash() -> None:
    source = "metadata = {'apiLevel': '2.15'}\n"
    extraction = "USA Scientific 96 Deep Well Plate on Magnetic Module GEN1 on 4"
    waste = "USA Scientific 96 Deep Well Plate on 1"
    reservoir = "NEST 12 Well Reservoir on 5"
    multi = "P300 8-Channel GEN2 on right mount"
    events: list[dict] = [{"kind": "engage"}]
    for column in (1, 3, 5, 7, 9, 11):
        well = f"A{column}"
        _transfer(events, (extraction, well), (waste, well), 200,
                  instrument=multi, channels=8)
    _transfer(events, (reservoir, "A9"), (extraction, "A1"), 200,
              instrument=multi, channels=8)
    name = "initial supernatant removed before washes"
    task = "opentrons-rna-extraction"
    good = audit_rubric_coverage(task, events, source)
    assert _check(good, "binding_and_separation", name)["status"] == "supported"
    missing_well = audit_rubric_coverage(task, events[:1] + events[5:], source)
    assert _check(missing_well, "binding_and_separation", name)["status"] == "failed"


def _rna_elution_events(*, wait_after_magnet: bool = True,
                        wrong_recovery_temperature: bool = False) -> list[dict]:
    events: list[dict] = [{"kind": "temp", "celsius": 4}, {"kind": "disengage"}]
    extraction = "USA Scientific 96 Deep Well Plate on Magnetic Module GEN1 on 4"
    elution = "Plate_thermo_96_elutions on Temperature Module GEN1 on 6"
    reservoir = "NEST 12 Well Reservoir on 5"
    multi = "P300 8-Channel GEN2 on right mount"
    for column in (1, 3, 5, 7, 9, 11):
        well = f"A{column}"
        _transfer(events, (reservoir, "A4"), (extraction, well), 100,
                  instrument=multi, channels=8)
        events.extend([
            {"kind": "pick", "instrument": multi, "channels": 8},
            {"kind": "aspirate", "volume": 50, "labware": extraction,
             "well": well, "instrument": multi, "channels": 8},
            {"kind": "dispense", "volume": 50, "labware": extraction,
             "well": well, "instrument": multi, "channels": 8},
            {"kind": "drop", "instrument": multi, "channels": 8},
        ])
    events.append({"kind": "delay", "seconds": 30})
    if not wait_after_magnet:
        events.append({"kind": "delay", "seconds": 90})
    events.append({"kind": "engage"})
    if wait_after_magnet:
        events.append({"kind": "delay", "seconds": 90})
    if wrong_recovery_temperature:
        events.append({"kind": "temp", "celsius": 25})
    for column in (1, 3, 5, 7, 9, 11):
        well = f"A{column}"
        _transfer(events, (extraction, well), (elution, well), 80,
                  instrument=multi, channels=8)
    return events


def test_rna_elution_clearing_wait_must_follow_magnet_engagement() -> None:
    source = "metadata = {'apiLevel': '2.15'}\n"
    name = "30 s incubation, 90 s magnetic clearing and mixing"
    wrong = audit_rubric_coverage("opentrons-rna-extraction",
                                  _rna_elution_events(wait_after_magnet=False), source)
    right = audit_rubric_coverage("opentrons-rna-extraction", _rna_elution_events(), source)
    assert _check(wrong, "elution_recovery", name)["status"] == "failed"
    assert _check(right, "elution_recovery", name)["status"] == "supported"


def _rna_sequential_elution_events(*, omit_second_column_wait: bool = False) -> list[dict]:
    events: list[dict] = [{"kind": "temp", "celsius": 4}, {"kind": "disengage"}]
    extraction = "USA Scientific 96 Deep Well Plate on Magnetic Module GEN1 on 4"
    elution = "Plate_thermo_96_elutions on Temperature Module GEN1 on 6"
    reservoir = "NEST 12 Well Reservoir on 5"
    multi = "P300 8-Channel GEN2 on right mount"
    for column in (1, 3, 5, 7, 9, 11):
        well = f"A{column}"
        _transfer(events, (reservoir, "A4"), (extraction, well), 100,
                  instrument=multi, channels=8)
    for column in (1, 3, 5, 7, 9, 11):
        well = f"A{column}"
        events.extend([
            {"kind": "pick", "instrument": multi, "channels": 8},
            {"kind": "aspirate", "volume": 50, "labware": extraction,
             "well": well, "instrument": multi, "channels": 8},
            {"kind": "dispense", "volume": 50, "labware": extraction,
             "well": well, "instrument": multi, "channels": 8},
            {"kind": "drop", "instrument": multi, "channels": 8},
        ])
        if not (omit_second_column_wait and column == 3):
            events.append({"kind": "delay", "seconds": 30})
        events.extend([{"kind": "engage"}, {"kind": "delay", "seconds": 90}])
        _transfer(events, (extraction, well), (elution, well), 80,
                  instrument=multi, channels=8)
        events.append({"kind": "disengage"})
    return events


def test_rna_elution_allows_sequential_column_magnet_cycles() -> None:
    source = "metadata = {'apiLevel': '2.15'}\n"
    name = "30 s incubation, 90 s magnetic clearing and mixing"
    right = audit_rubric_coverage("opentrons-rna-extraction",
                                  _rna_sequential_elution_events(), source)
    wrong = audit_rubric_coverage("opentrons-rna-extraction",
                                  _rna_sequential_elution_events(omit_second_column_wait=True),
                                  source)
    assert _check(right, "elution_recovery", name)["status"] == "supported"
    assert _check(wrong, "elution_recovery", name)["status"] == "failed"


def test_rna_recovery_checks_current_temperature_not_any_earlier_four_degrees() -> None:
    source = "metadata = {'apiLevel': '2.15'}\n"
    result = audit_rubric_coverage("opentrons-rna-extraction",
                                   _rna_elution_events(wrong_recovery_temperature=True), source)
    assert _check(result, "elution_recovery", "matched recovery into 4 °C plate")["status"] == "failed"
