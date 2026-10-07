"""Reaction checks derive math from supplied recipes, not benchmark answers."""

from __future__ import annotations

from pybravo.evals.text2wetlab.reaction import (
    Addition,
    PipetteRange,
    Reaction,
    Stroke,
    audit_reaction,
    audit_strokes,
)


def _codes(issues):
    return {issue.code for issue in issues}


def test_complete_two_x_to_one_x_reaction_balances_with_diluent():
    reaction = Reaction("PCR", 10, (
        Addition("2x mix", 5, stock_strength_x=2, target_strength_x=1),
        Addition("primer", 1),
        Addition("template", 1),
        Addition("water", 3, is_diluent=True),
    ), diluent_name="water")
    assert audit_reaction(reaction, available_components={"2x mix", "primer", "template", "water"}) == ()


def test_missing_diluent_exposes_both_final_volume_and_actual_strength():
    reaction = Reaction("PCR", 10, (
        Addition("2x mix", 5, stock_strength_x=2, target_strength_x=1),
        Addition("primer", 1),
        Addition("template", 1),
    ), diluent_name="water")
    issues = audit_reaction(reaction)
    assert _codes(issues) == {"final_volume_mismatch", "missing_diluent", "realized_strength_mismatch"}
    shortage = next(issue for issue in issues if issue.code == "missing_diluent")
    assert shortage.expected == 3
    concentration = next(issue for issue in issues if issue.code == "realized_strength_mismatch")
    assert round(concentration.observed, 3) == 1.429


def test_stock_dilution_math_catches_wrong_fraction_even_at_correct_total():
    reaction = Reaction("PCR", 10, (
        Addition("2x mix", 4, stock_strength_x=2, target_strength_x=1),
        Addition("primer", 1),
        Addition("template", 1),
        Addition("water", 4, is_diluent=True),
    ), diluent_name="water")
    issues = audit_reaction(reaction)
    assert _codes(issues) == {"stock_dilution_mismatch", "realized_strength_mismatch"}
    dilution = next(issue for issue in issues if issue.code == "stock_dilution_mismatch")
    assert dilution.expected == 5
    assert dilution.observed == 4


def test_reaction_can_distinguish_absent_on_deck_source_from_manual_addition():
    base = (
        Addition("2x mix", 5, stock_strength_x=2, target_strength_x=1),
        Addition("primer", 1),
        Addition("template", 1),
    )
    robot_reaction = Reaction("PCR", 10, base + (Addition("water", 3, is_diluent=True),), "water")
    manual_reaction = Reaction("PCR", 10, base + (Addition("water", 3, is_diluent=True,
                                                            delivery="manual"),), "water")
    available = {"2x mix", "primer", "template"}
    assert _codes(audit_reaction(robot_reaction, available_components=available)) == {"unavailable_component"}
    assert audit_reaction(manual_reaction, available_components=available) == ()


def test_other_reaction_sizes_and_stock_strengths_are_data_driven():
    assembly = Reaction("assembly", 20, (
        Addition("fragments", 10),
        Addition("10x buffer", 2, stock_strength_x=10, target_strength_x=1),
        Addition("enzyme", 1),
        Addition("water", 7, is_diluent=True),
    ), diluent_name="water")
    assert audit_reaction(assembly) == ()


def test_missing_reagent_volume_prevents_false_final_volume_claim():
    reaction = Reaction("unknown", 25, (Addition("buffer", None), Addition("water", 10)), "water")
    assert _codes(audit_reaction(reaction)) == {"missing_volume"}


def test_valid_capacity_bounded_tip_cycle():
    pipettes = [PipetteRange("p20", 1, 20)]
    strokes = [
        Stroke("p20", "pick_up_tip"),
        Stroke("p20", "aspirate", 20, "source A1"),
        Stroke("p20", "dispense", 10, "destination A1"),
        Stroke("p20", "dispense", 10, "destination A2"),
        Stroke("p20", "drop_tip"),
    ]
    assert audit_strokes(strokes, pipettes) == ()


def test_strokes_reject_out_of_range_and_overfilled_pipette():
    pipettes = [PipetteRange("p20", 1, 20), PipetteRange("p300", 20, 300)]
    strokes = [
        Stroke("p20", "pick_up_tip"),
        Stroke("p20", "aspirate", 25, "source A1"),
        Stroke("p20", "dispense", 30, "destination A1"),
        Stroke("p20", "drop_tip"),
        Stroke("p300", "pick_up_tip"),
        Stroke("p300", "aspirate", 4, "source B1"),
        Stroke("p300", "drop_tip"),
    ]
    assert {"above_pipette_maximum", "pipette_overfilled", "dispense_exceeds_held",
            "below_pipette_minimum"} <= _codes(audit_strokes(strokes, pipettes))


def test_mix_without_liquid_location_after_tip_pickup_is_rejected():
    strokes = [
        Stroke("p20", "pick_up_tip"),
        Stroke("p20", "mix", 15, repetitions=5),
        Stroke("p20", "drop_tip"),
    ]
    assert _codes(audit_strokes(strokes, [PipetteRange("p20", 1, 20)])) == {"missing_liquid_location"}


def test_tip_state_errors_are_detected_independently_per_pipette():
    strokes = [
        Stroke("p20", "pick_up_tip"),
        Stroke("p300", "pick_up_tip"),
        Stroke("p20", "pick_up_tip"),
        Stroke("p20", "aspirate", 5, "source A1"),
        Stroke("p20", "drop_tip"),
        Stroke("p300", "dispense", 25, "destination A1"),
    ]
    codes = _codes(audit_strokes(strokes, [PipetteRange("p20", 1, 20), PipetteRange("p300", 20, 300)]))
    assert {"tip_already_attached", "dispense_exceeds_held", "tip_left_attached"} <= codes


def test_mix_accounts_for_liquid_already_held():
    strokes = [
        Stroke("p20", "pick_up_tip"),
        Stroke("p20", "aspirate", 15, "source A1"),
        Stroke("p20", "mix", 10, "destination A1", repetitions=3),
        Stroke("p20", "drop_tip"),
    ]
    assert _codes(audit_strokes(strokes, [PipetteRange("p20", 0, 20)])) == {"mix_exceeds_capacity"}


def test_blow_out_clears_held_volume_before_next_aspiration():
    strokes = [
        Stroke("p20", "pick_up_tip"),
        Stroke("p20", "aspirate", 20, "source A1"),
        Stroke("p20", "blow_out", location="waste A1"),
        Stroke("p20", "aspirate", 20, "source A1"),
        Stroke("p20", "drop_tip"),
    ]
    assert audit_strokes(strokes, [PipetteRange("p20", 1, 20)]) == ()
