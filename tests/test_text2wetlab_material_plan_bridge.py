"""Synthetic, source-cited plan targets for the standalone material ledger."""

from __future__ import annotations

from pybravo.evals.text2wetlab.material_ledger import InitialWell, WellRef, audit_material_flow
from pybravo.evals.text2wetlab.material_plan_bridge import TargetBinding, targets_from_accepted_plan
from pybravo.evals.text2wetlab.planning import DeckSource, Evidence, OT2Plan, PlannedReaction, Stage
from pybravo.evals.text2wetlab.planning_runtime import PlanningResult

INSTRUCTION = (
    "Prepare master mix 15 µL in tube rack well B1. "
    "Prepare final reaction 25 µL in PCR plate well A1."
)
MIX_QUOTE = "Prepare master mix 15 µL in tube rack well B1"
REACTION_QUOTE = "Prepare final reaction 25 µL in PCR plate well A1"


def _stage() -> Stage:
    return Stage(
        "Prepare mix", "pipette", None, None, None, None, None,
        None, None, None, (Evidence("instruction", MIX_QUOTE),),
    )


def _result(status: str = "accepted") -> PlanningResult:
    plan = OT2Plan(
        (DeckSource("mix", "master mix", "tube rack", (Evidence("instruction", MIX_QUOTE),),
                    "Prepare mix"),),
        (PlannedReaction("master mix batch", 15, None, (), (Evidence("instruction", MIX_QUOTE),)),
         PlannedReaction("final reaction", 25, None, (), (Evidence("instruction", REACTION_QUOTE),))),
        (), (_stage(),),
    )
    return PlanningResult(plan, ({"status": status},))


def _intermediate(*, quote: str = MIX_QUOTE) -> TargetBinding:
    return TargetBinding(
        "master mix batch", WellRef("tube rack on 2", "B1"), "prepared_intermediate",
        Evidence("instruction", quote), Evidence("instruction", MIX_QUOTE), "mix",
    )


def _final(*, volume_quote: str = REACTION_QUOTE) -> TargetBinding:
    return TargetBinding(
        "final reaction", WellRef("PCR plate on 3", "A1"), "final_reaction",
        Evidence("instruction", REACTION_QUOTE), Evidence("instruction", volume_quote),
    )


def _codes(targets) -> set[str]:
    return {gap.code for gap in targets.review_gaps}


def test_exact_cited_well_and_volume_create_final_and_preparation_targets() -> None:
    targets = targets_from_accepted_plan(
        _result(), (_intermediate(), _final()), instruction=INSTRUCTION,
        stage_event_indices={"Prepare mix": 7},
    )
    assert not targets.review_gaps
    assert targets.well_limits[WellRef("PCR plate on 3", "A1")].target_final_volume_ul == 25
    assert targets.stage_targets_ul["Prepare mix"][WellRef("tube rack on 2", "B1")] == 15
    assert [(stage.name, stage.after_event_index) for stage in targets.stages] == [("Prepare mix", 7)]


def test_bridge_targets_can_flag_duplicated_intermediate_in_observed_events() -> None:
    targets = targets_from_accepted_plan(
        _result(), (_intermediate(),), instruction=INSTRUCTION,
        stage_event_indices={"Prepare mix": 7},
    )
    stock = WellRef("stock on 1", "A1")
    intermediate = WellRef("tube rack on 2", "B1")
    first = [
        {"kind": "pick", "instrument": "p20"},
        {"kind": "aspirate", "instrument": "p20", "labware": stock.labware,
         "well": stock.well, "volume": 15},
        {"kind": "dispense", "instrument": "p20", "labware": intermediate.labware,
         "well": intermediate.well, "volume": 15},
        {"kind": "drop", "instrument": "p20"},
    ]
    ledger = audit_material_flow(
        first + first,
        initial_wells={stock: InitialWell(40, "stock"), intermediate: InitialWell(0)},
        stages=targets.stages,
        stage_targets_ul=targets.stage_targets_ul,
    )
    assert "stage_volume_over_target" in {issue.code for issue in ledger.issues}


def test_nonaccepted_plan_or_unresolved_vessel_cannot_supply_a_target() -> None:
    rejected = targets_from_accepted_plan(_result("audit_failed"), (_final(),), instruction=INSTRUCTION)
    assert "plan_not_accepted" in _codes(rejected)
    assert not rejected.well_limits

    wrong_well = TargetBinding(
        "final reaction", WellRef("PCR plate on 3", "B1"), "final_reaction",
        Evidence("instruction", REACTION_QUOTE), Evidence("instruction", REACTION_QUOTE),
    )
    missing = targets_from_accepted_plan(_result(), (wrong_well,), instruction=INSTRUCTION)
    assert "vessel_identity_unresolved" in _codes(missing)
    assert not missing.well_limits


def test_volume_quote_must_state_the_plan_quantity_with_units() -> None:
    wrong_volume = "Prepare master mix 15 µL in tube rack well B1"
    targets = targets_from_accepted_plan(_result(), (_final(volume_quote=wrong_volume),), instruction=INSTRUCTION)
    assert "volume_identity_unresolved" in _codes(targets)
    assert not targets.well_limits


def test_same_number_in_an_unrelated_passage_does_not_bind_a_vessel() -> None:
    unrelated = "Add 25 µL buffer to wash tube rack well C1"
    targets = targets_from_accepted_plan(
        _result(), (_final(volume_quote=unrelated),),
        instruction=INSTRUCTION + " " + unrelated + ".",
    )
    assert "volume_vessel_link_unresolved" in _codes(targets)
    assert not targets.well_limits


def test_separate_passages_can_link_by_a_distinct_reaction_name() -> None:
    vessel_quote = "PCR plate well A1 is the destination"
    volume_quote = "The PCR reaction is 25 µL"
    original = _result().plan
    assert original is not None
    reaction = PlannedReaction("PCR reaction", 25, None, (), (Evidence("instruction", volume_quote),))
    accepted = PlanningResult(
        OT2Plan(original.deck_sources, (reaction,), (), original.stages),
        ({"status": "accepted"},),
    )
    binding = TargetBinding("PCR reaction", WellRef("PCR plate on 3", "A1"), "final_reaction",
                            Evidence("instruction", vessel_quote), Evidence("instruction", volume_quote))
    targets = targets_from_accepted_plan(
        accepted, (binding,), instruction=vessel_quote + ". " + volume_quote + ".",
    )
    assert not targets.review_gaps
    assert targets.well_limits[binding.well].target_final_volume_ul == 25


def test_intermediate_requires_grounded_generated_source_and_event_boundary() -> None:
    no_boundary = targets_from_accepted_plan(_result(), (_intermediate(),), instruction=INSTRUCTION)
    assert "stage_boundary_unresolved" in _codes(no_boundary)
    assert not no_boundary.stage_targets_ul

    wrong_source = TargetBinding(
        "master mix batch", WellRef("tube rack on 2", "B1"), "prepared_intermediate",
        Evidence("instruction", MIX_QUOTE), Evidence("instruction", MIX_QUOTE), "absent",
    )
    unlinked = targets_from_accepted_plan(
        _result(), (wrong_source,), instruction=INSTRUCTION,
        stage_event_indices={"Prepare mix": 7},
    )
    assert "intermediate_source_unresolved" in _codes(unlinked)

    out_of_range = targets_from_accepted_plan(
        _result(), (_intermediate(),), instruction=INSTRUCTION,
        stage_event_indices={"Prepare mix": 7}, event_count=7,
    )
    assert "stage_boundary_invalid" in _codes(out_of_range)


def test_conflicting_or_ambiguous_bindings_leave_review_gaps() -> None:
    duplicate = targets_from_accepted_plan(_result(), (_final(), _final()), instruction=INSTRUCTION)
    assert "duplicate_target_well" in _codes(duplicate)
    assert not duplicate.well_limits

    plan = _result().plan
    assert plan is not None
    repeated_reaction = PlanningResult(
        OT2Plan(plan.deck_sources, plan.reactions + (plan.reactions[1],), plan.tip_budgets, plan.stages),
        ({"status": "accepted"},),
    )
    ambiguous = targets_from_accepted_plan(repeated_reaction, (_final(),), instruction=INSTRUCTION)
    assert "ambiguous_reaction" in _codes(ambiguous)


def test_stage_boundaries_must_follow_ordered_plan_stages() -> None:
    plan = _result().plan
    assert plan is not None
    second_quote = "Prepare second master mix 15 µL in tube rack well B2"
    second_stage = Stage(
        "Prepare second mix", "pipette", None, None, None, None, None,
        None, None, None, (Evidence("instruction", second_quote),),
    )
    second_source = DeckSource("mix2", "master mix", "tube rack", (Evidence("instruction", second_quote),),
                               "Prepare second mix")
    second_reaction = PlannedReaction("second mix batch", 15, None, (),
                                      (Evidence("instruction", second_quote),))
    expanded = PlanningResult(OT2Plan(plan.deck_sources + (second_source,),
                                      plan.reactions + (second_reaction,), (),
                                      plan.stages + (second_stage,)), ({"status": "accepted"},))
    second_binding = TargetBinding("second mix batch", WellRef("tube rack on 2", "B2"),
                                   "prepared_intermediate", Evidence("instruction", second_quote),
                                   Evidence("instruction", second_quote), "mix2")
    targets = targets_from_accepted_plan(
        expanded, (_intermediate(), second_binding), instruction=INSTRUCTION + " " + second_quote + ".",
        stage_event_indices={"Prepare mix": 9, "Prepare second mix": 4},
    )
    assert "stage_boundary_invalid" in _codes(targets)
    assert set(targets.stage_targets_ul) == {"Prepare mix"}
