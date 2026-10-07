"""Source-fidelity checks compare model-authored plans with simulated actions."""

from __future__ import annotations

from dataclasses import replace

from pybravo.evals.text2wetlab.planning import (
    DeckSource,
    Evidence,
    OT2Plan,
    PlannedAddition,
    PlannedReaction,
    Stage,
    TipBudget,
    TipDemand,
    audit_plan,
)
from pybravo.evals.text2wetlab.reaction import Addition
from pybravo.evals.text2wetlab.source_fidelity import (
    SOURCE_FIDELITY_GUIDANCE,
    audit_direct_source_delivery,
    audit_inventory_strength_claims,
    audit_manual_addition_stages,
)


def _stage(name: str, kind: str = "pipette") -> Stage:
    return Stage(name, kind, None, None, None, None, None, None, None, None, ())


def _plan(*, mix_ul: float = 5, primer_ul: float = 0.5,
          water_delivery: str = "manual", water_stage: str | None = "Add water by hand") -> OT2Plan:
    sources = (
        DeckSource("mix_reservoir", "2x master mix", "nest_1_reservoir_195ml", ()),
        DeckSource("primer_plate", "primer pairs", "corning_96_wellplate_360ul_flat", ()),
        DeckSource("template_plate", "colony template", "corning_96_wellplate_360ul_flat", ()),
    )
    reaction = PlannedReaction("PCR", 10, "water", (
        PlannedAddition(Addition("2x master mix", mix_ul, 2, 1), "mix_reservoir", (),
                        stage_name="Add mix"),
        PlannedAddition(Addition("primer pairs", primer_ul), "primer_plate", (),
                        stage_name="Add primers"),
        PlannedAddition(Addition("water", 10 - mix_ul - primer_ul - 1, is_diluent=True,
                                 delivery=water_delivery), None, (), stage_name=water_stage),
        PlannedAddition(Addition("colony template", 1), "template_plate", (),
                        stage_name="Add template"),
    ), ())
    stages = (_stage("Add mix"), _stage("Add primers"), _stage("Add template"),
              _stage("Add water by hand", "manual"))
    budget = TipBudget("p20_single_gen2", 1, 1, 96, 0, True, (
        TipDemand("Add mix", 96, 1, 0, (), (mix_ul,)),
        TipDemand("Add primers", 96, 1, 0, (), (primer_ul,)),
        TipDemand("Add template", 96, 1, 0, (), (1,)),
    ), (), "opentrons_96_tiprack_20ul")
    return OT2Plan(sources, (reaction,), (budget,), stages)


def _events(*, mix_ul: float = 5, primer_ul: float = 0.5) -> list[dict]:
    return [
        {"kind": "pick", "instrument": "P20 Single"},
        {"kind": "aspirate", "instrument": "P20 Single", "labware": "mix_reservoir on 3",
         "well": "A1", "volume": mix_ul},
        {"kind": "dispense", "instrument": "P20 Single", "labware": "pcr_plate on 2",
         "well": "A1", "volume": mix_ul},
        {"kind": "drop", "instrument": "P20 Single"},
        {"kind": "pick", "instrument": "P20 Single"},
        {"kind": "aspirate", "instrument": "P20 Single", "labware": "primer_plate on 4",
         "well": "A1", "volume": primer_ul},
        {"kind": "dispense", "instrument": "P20 Single", "labware": "pcr_plate on 2",
         "well": "A1", "volume": primer_ul},
        {"kind": "drop", "instrument": "P20 Single"},
    ]


def _codes(findings) -> set[str]:
    return {issue.code for issue in findings}


def test_arithmetic_inventory_range_and_tip_checks_reject_unfaithful_plan():
    plan = _plan(mix_ul=4.5, water_delivery="robot", water_stage=None)
    issues = audit_plan(plan)
    assert {"stock_dilution_mismatch", "realized_strength_mismatch",
            "missing_deck_source", "pipette_range_mismatch", "insufficient_tips"} <= _codes(issues)


def test_manual_addition_needs_named_handoff_stage():
    assert audit_manual_addition_stages(_plan()) == ()
    issues = audit_manual_addition_stages(_plan(water_stage=None))
    assert _codes(issues) == {"manual_addition_stage_missing"}


def test_stock_multiplier_claim_cannot_disagree_with_loaded_inventory():
    plan = _plan()
    cited_source = replace(plan.deck_sources[0], component="1x master mix", evidence=(
        Evidence("instruction", "mix_reservoir: 2x master mix, plenty"),
    ))
    reaction = plan.reactions[0]
    wrong_addition = replace(reaction.additions[0], addition=replace(
        reaction.additions[0].addition, stock_strength_x=1,
    ))
    plan = replace(plan, deck_sources=(cited_source, *plan.deck_sources[1:]),
                   reactions=(replace(reaction, additions=(wrong_addition, *reaction.additions[1:])),))
    assert _codes(audit_inventory_strength_claims(plan)) == {
        "source_strength_conflicts_with_inventory",
        "addition_stock_strength_conflicts_with_inventory",
    }


def test_simulated_volume_must_match_cited_plan_not_rounded_pipette_minimum():
    plan = _plan()
    issues = audit_direct_source_delivery(plan, _events(primer_ul=1), labware={
        "mix_reservoir on 3": "nest_1_reservoir_195ml",
        "primer_plate on 4": "corning_96_wellplate_360ul_flat",
    })
    assert _codes(issues) == {"delivered_volume_differs_from_plan"}
    assert issues[0].path.endswith("primer_plate/pcrplate/A1")
    assert "0.5 µL" in issues[0].message


def test_simulated_stock_volume_must_match_cited_dilution():
    issues = audit_direct_source_delivery(_plan(), _events(mix_ul=4.5, primer_ul=0.5))
    assert _codes(issues) == {"delivered_volume_differs_from_plan"}
    assert "mix_reservoir" in issues[0].path


def test_two_planned_additions_from_one_source_compare_their_per_well_sum():
    plan = _plan()
    reaction = plan.reactions[0]
    second_mix = PlannedAddition(Addition("2x master mix", 1), "mix_reservoir", (),
                                 stage_name="Add mix")
    plan = replace(plan, reactions=(replace(
        reaction, additions=(*reaction.additions, second_mix),
    ),))
    assert audit_direct_source_delivery(plan, _events(mix_ul=6)) == ()


def test_missing_planned_deck_source_is_observable_when_labware_was_loaded():
    events = [event for event in _events() if "primer_plate" not in event.get("labware", "")]
    issues = audit_direct_source_delivery(_plan(), events, labware={
        "mix_reservoir on 3": "nest_1_reservoir_195ml",
        "primer_plate on 4": "corning_96_wellplate_360ul_flat",
    })
    assert "planned_source_not_aspirated" in _codes(issues)


def test_mixed_tip_lineage_is_left_for_review_instead_of_false_attribution():
    events = _events()[:3]
    events.insert(2, {"kind": "aspirate", "instrument": "P20 Single",
                      "labware": "unknown_reagent on 6", "well": "A1", "volume": 1})
    assert audit_direct_source_delivery(_plan(), events) == ()


def test_unmatched_simulator_label_does_not_assert_a_source_volume():
    events = [dict(event) for event in _events(primer_ul=1)]
    for event in events:
        if event.get("labware") == "primer_plate on 4":
            event["labware"] = "operator_label on 4"
    assert audit_direct_source_delivery(_plan(), events, labware={
        "operator_label on 4": "corning_96_wellplate_360ul_flat",
    }) == ()


def test_guidance_forbids_unsourced_predilution_to_hide_subminimum_transfer():
    assert "do not invent a diluted stock" in SOURCE_FIDELITY_GUIDANCE.lower()
    assert "newly prepared dilution" in SOURCE_FIDELITY_GUIDANCE
    assert "stock-equivalent" in SOURCE_FIDELITY_GUIDANCE
    assert "manual operator-handoff stage" in " ".join(SOURCE_FIDELITY_GUIDANCE.split())
