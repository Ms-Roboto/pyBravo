"""Localized model repairs must apply only to the rejected source coordinates."""

import hashlib

import pytest

from pybravo.evals.text2wetlab.patch_repair import (
    PatchError,
    apply_line_patch,
    line_patch_messages,
    numbered_source,
    preserve_simulator_repair_facts,
    task_allows_tip_refill,
)


def test_numbered_source_and_prompt_keep_task_and_candidate_distinct():
    source = "first()\nsecond()\n"
    messages = line_patch_messages(source, instruction="Move 5 µL into A1.",
                                   diagnostic="Line 2 failed.")
    assert numbered_source(source) == "0001| first()\n0002| second()"
    assert "Move 5 µL" in messages[1]["content"]
    assert hashlib.sha256(source.encode()).hexdigest() in messages[2]["content"]
    assert "0002| second()" in messages[2]["content"]
    assert "keep the existing aspirate" in messages[0]["content"].lower()
    assert "Do not return used tips" in messages[0]["content"]
    assert "Do not reset tip racks" in messages[0]["content"]


def test_tip_refill_requires_explicit_source_permission():
    instruction = "Tips are unlimited: call pipette.reset_tipracks() when you have used a rack."
    assert task_allows_tip_refill(instruction)
    assert not task_allows_tip_refill("Do not call pipette.reset_tipracks().")
    messages = line_patch_messages("x()", instruction=instruction,
                                   diagnostic="OutOfTipsError")
    assert "add reset_tipracks() only after" in messages[0]["content"]


def test_single_line_replacement_keeps_every_other_line():
    source = "before()\nwrong()\nafter()\n"
    patched = apply_line_patch(source, {
        "edits": [{"start_line": 2, "end_line": 2, "replacement": "right()"}],
    })
    assert patched == "before()\nright()\nafter()\n"


def test_insertion_and_append_use_original_line_coordinates():
    source = "one()\ntwo()\n"
    patched = apply_line_patch(source, {"edits": [
        {"start_line": 3, "end_line": 2, "replacement": "three()"},
        {"start_line": 1, "end_line": 0, "replacement": "zero()"},
    ]})
    assert patched == "zero()\none()\ntwo()\nthree()\n"


@pytest.mark.parametrize("edits", [
    [],
    [{"start_line": 0, "end_line": 0, "replacement": "x"}],
    [{"start_line": 4, "end_line": 3, "replacement": "x"}],
    [{"start_line": 1, "end_line": 3, "replacement": "x"}],
    [{"start_line": 1, "end_line": 1, "replacement": "x"},
     {"start_line": 2, "end_line": 1, "replacement": "y"}],
    [{"start_line": 2, "end_line": 1, "replacement": "x"},
     {"start_line": 2, "end_line": 1, "replacement": "y"}],
    [{"start_line": True, "end_line": 0, "replacement": "x"}],
    [{"start_line": 1, "end_line": 1, "replacement": "x", "extra": 1}],
])
def test_invalid_or_ambiguous_patches_fail_closed(edits):
    with pytest.raises(PatchError):
        apply_line_patch("one()\ntwo()\n", {"edits": edits})


def test_unmodified_protocol_is_not_a_successful_repair():
    with pytest.raises(PatchError):
        apply_line_patch("one()\n", {"edits": [
            {"start_line": 1, "end_line": 1, "replacement": "one()"},
        ]})


PROGRAM = '''from opentrons import protocol_api
metadata = {"apiLevel": "2.15"}
def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_96_tiprack_20ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    pipette = protocol.load_instrument("p20_single_gen2", "left", tip_racks=[tips])
    volume = 5
    for i in range(2):
        pipette.pick_up_tip()
        pipette.aspirate(volume, plate["A1"])
        pipette.dispense(volume, plate.wells()[i])
        pipette.mix(2, volume)
        pipette.drop_tip()
    protocol.delay(minutes=5)
'''


def test_simulator_repair_can_name_implicit_mix_well_without_changing_liquid_facts():
    fixed = PROGRAM.replace("pipette.mix(2, volume)", "pipette.mix(2, volume, plate.wells()[i])")
    assert preserve_simulator_repair_facts(PROGRAM, fixed) is None


def test_simulator_repair_may_correct_only_a_computed_well_index():
    bad = PROGRAM.replace("pipette.dispense(volume, plate.wells()[i])",
                          "target = plate.wells()[i * 12]\n        pipette.dispense(volume, target)")
    fixed = bad.replace("plate.wells()[i * 12]", "plate.wells()[i * 8]")
    assert preserve_simulator_repair_facts(bad, fixed) is None


def test_simulator_repair_may_add_fresh_tip_cycle_without_removing_work():
    fixed = PROGRAM.replace("        pipette.mix(2, volume)",
                            "        pipette.drop_tip()\n"
                            "        pipette.pick_up_tip()\n"
                            "        pipette.mix(2, volume)")
    assert preserve_simulator_repair_facts(PROGRAM, fixed) is None


def test_explicitly_authorized_tip_refill_can_add_only_isolated_reset_guard():
    fixed = PROGRAM.replace("        pipette.pick_up_tip()",
                            "        if i == 1:\n"
                            "            pipette.reset_tipracks()\n"
                            "        pipette.pick_up_tip()")
    assert preserve_simulator_repair_facts(PROGRAM, fixed) is not None
    assert preserve_simulator_repair_facts(PROGRAM, fixed,
                                            allow_tip_refill=True) is None
    unsafe = fixed.replace("pipette.reset_tipracks()",
                           "pipette.aspirate(volume, plate['A1'])\n"
                           "            pipette.reset_tipracks()")
    assert preserve_simulator_repair_facts(PROGRAM, unsafe,
                                            allow_tip_refill=True) is not None


@pytest.mark.parametrize("source, reason", [
    (PROGRAM.replace("volume = 5", "volume = 6"), "declared volumes"),
    (PROGRAM.replace("pipette.dispense(volume", "pipette.dispense(6"), "declared volumes"),
    (PROGRAM.replace("        pipette.dispense(volume, plate.wells()[i])\n", ""), "liquid actions"),
    (PROGRAM.replace("range(2)", "range(1)"), "control-flow"),
    (PROGRAM.replace("plate[\"A1\"]", "plate[\"A2\"]"), "liquid source"),
    (PROGRAM.replace("protocol.delay(minutes=5)", "protocol.delay(minutes=1)"), "incubation"),
    (PROGRAM.replace("pipette.drop_tip()", "pipette.return_tip()"), "tip change"),
    (PROGRAM.replace("protocol.delay(minutes=5)", "protocol.delay(minutes=5)\n    helper(7)"),
     "numeric setting"),
    (PROGRAM.replace("load_labware(\"corning_96_wellplate_360ul_flat\", 2)",
                     "load_labware(\"corning_96_wellplate_360ul_flat\", 3)"), "deck bindings"),
])
def test_simulator_repair_rejects_changed_task_facts(source, reason):
    assert reason in preserve_simulator_repair_facts(PROGRAM, source)
