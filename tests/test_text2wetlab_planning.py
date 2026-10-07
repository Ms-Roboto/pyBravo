"""Typed OT-2 planning checks use cited task facts, never reference protocols."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pybravo.evals.text2wetlab.planning import (
    PLAN_SCHEMA,
    DeckSource,
    Evidence,
    OT2Plan,
    PlannedAddition,
    PlannedReaction,
    PlanParseError,
    Stage,
    TipBudget,
    TipDemand,
    audit_plan,
    parse_plan,
    plan_to_prompt,
)
from pybravo.evals.text2wetlab.reaction import Addition

INSTRUCTION = """# Example PCR task
Load 1 rack of 96 tips for p20_single_gen2: opentrons_96_tiprack_20ul.
Tips are unlimited: call reset_tipracks after a used rack.
## What is in the labware at the start
- `mix_reservoir`: Q5 master mix 2x, plenty.
- `primer_plate`: primer pairs, plenty.
- `template_plate`: colony template, plenty.
- `pcr_plate`: empty destination plate.
## Tools
Prepare 96 separate reactions on the pcr_plate.
"""
PAPER = """The final PCR reaction is 10 µL. Add 5 µL Q5 master mix,
1 µL primer pair, 1 µL colony template and water to fill the volume.
"""


def _quote(text: str, source: str = "instruction") -> list[dict[str, str]]:
    return [{"source": source, "quote": text}]


def _empty_payload() -> dict:
    return {"deck_sources": [], "reactions": [], "tip_budgets": [], "stages": []}


def _source(source_id: str, component: str, labware: str, quote: str) -> dict:
    return {"id": source_id, "component": component, "labware": labware,
            "evidence": _quote(quote)}


def _addition(component: str, source_id: str | None, volume: float, quote: str,
              *, stock: float | None = None, target: float | None = None,
              is_diluent: bool = False, delivery: str = "robot") -> dict:
    return {"component": component, "source_id": source_id, "volume_ul": volume,
            "stock_strength_x": stock, "target_strength_x": target,
            "is_diluent": is_diluent, "delivery": delivery,
            "evidence": _quote(quote, "paper")}


def _reaction() -> dict:
    return {"name": "colony PCR", "final_volume_ul": 10, "diluent_name": "water",
            "evidence": _quote("final PCR reaction is 10 µL", "paper"),
            "additions": [
                _addition("Q5 master mix", "mix", 5, "5 µL Q5 master mix", stock=2, target=1),
                _addition("primer pair", "primer", 1, "1 µL primer pair"),
                _addition("colony template", "template", 1, "1 µL colony template"),
            ]}


def _stage(name: str, kind: str, quote: str, *, module: str | None = None,
           module_type: str | None = None, temp: float | None = None,
           duration: float | None = None,
           lid: str | None = None, magnet: str | None = None,
           requires_temp: float | None = None, requires_lid: str | None = None,
           requires_magnet: str | None = None) -> dict:
    return {"name": name, "kind": kind, "module_id": module,
            "module_type": module_type, "temperature_c": temp, "lid_state": lid,
            "magnet_state": magnet, "required_temperature_c": requires_temp,
            "required_lid_state": requires_lid, "required_magnet_state": requires_magnet,
            "duration_s": duration,
            "evidence": _quote(quote)}


def _sources() -> list[dict]:
    return [
        _source("mix", "Q5 master mix", "mix_reservoir", "`mix_reservoir`: Q5 master mix 2x"),
        _source("primer", "primer pair", "primer_plate", "`primer_plate`: primer pairs"),
        _source("template", "colony template", "template_plate",
                "`template_plate`: colony template"),
    ]


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_schema_has_only_plan_data_and_no_protocol_code_field():
    assert set(PLAN_SCHEMA["properties"]) == {
        "deck_sources", "reactions", "tip_budgets", "stages",
    }
    assert PLAN_SCHEMA["additionalProperties"] is False
    source_schema = PLAN_SCHEMA["properties"]["deck_sources"]["items"]
    addition_schema = PLAN_SCHEMA["properties"]["reactions"]["items"]["properties"]["additions"]["items"]
    assert "produced_by_stage" in source_schema["required"]
    assert "stage_name" in addition_schema["required"]
    assert "output_source_id" in PLAN_SCHEMA["properties"]["reactions"]["items"]["required"]
    assert "after_stage" in PLAN_SCHEMA["properties"]["stages"]["items"]["required"]
    assert "well" in PLAN_SCHEMA["properties"]["deck_sources"]["items"]["required"]


def test_generated_intermediate_has_one_preparation_and_ordered_manual_stage():
    produced = DeckSource("batch", "prepared mixture", "microtube", (),
                          produced_by_stage="Prepare mixture")
    stock = DeckSource("stock", "stock reagent", "source plate", ())
    one = PlannedReaction("prepare batch", 10, None, (
        PlannedAddition(Addition("stock reagent", 10), "stock", (),
                        stage_name="Prepare mixture"),
    ), (), output_source_id="batch")
    stages = (
        Stage("Prepare mixture", "pipette", None, None, None, None, None,
              None, None, None, ()),
        Stage("Manual hold", "manual", None, None, None, None, None,
              None, None, None, (), duration_s=60, after_stage="Prepare mixture"),
    )
    plan = OT2Plan((stock, produced), (one,), (), stages)
    assert "duplicate_intermediate_preparation" not in _codes(audit_plan(plan))
    assert "duplicate_intermediate_preparation" in _codes(audit_plan(replace(
        plan, reactions=(one, replace(one, name="corrected batch")),
    )))
    assert "reaction_output_is_starting_stock" in _codes(audit_plan(replace(
        plan, reactions=(replace(one, output_source_id="stock"),),
    )))
    assert "stage_predecessor_not_earlier" in _codes(audit_plan(replace(
        plan, stages=(stages[1], stages[0]),
    )))
    assert "unknown_stage_predecessor" in _codes(audit_plan(replace(
        plan, stages=(stages[0], replace(stages[1], after_stage="Missing")),
    )))
    rendered = json.loads(plan_to_prompt(plan))
    assert rendered["reactions"][0]["output_source_id"] == "batch"
    assert rendered["stages"][1]["after_stage"] == "Prepare mixture"


def test_parser_accepts_whitespace_normalized_verbatim_evidence():
    payload = _empty_payload()
    payload["deck_sources"] = _sources()
    payload["reactions"] = [_reaction()]
    plan = parse_plan(payload, instruction=INSTRUCTION, scientific_source=PAPER)
    assert len(plan.deck_sources) == 3
    assert plan.reactions[0].additions[0].addition.stock_strength_x == 2


def test_parser_accepts_markup_and_bounded_elision_but_not_reordered_or_fake_words():
    payload = _empty_payload()
    payload["deck_sources"] = [_source("mix", "Q5 master mix", "mix_reservoir",
                                       "mix_reservoir: Q5 master mix 2x, plenty")]
    assert parse_plan(payload, instruction=INSTRUCTION).deck_sources[0].id == "mix"
    payload["deck_sources"][0]["evidence"] = _quote("`mix_reservoir` ... Q5 master mix 2x")
    assert parse_plan(payload, instruction=INSTRUCTION).deck_sources[0].id == "mix"
    payload["deck_sources"][0]["evidence"] = _quote("Q5 master mix 2x ... mix_reservoir")
    with pytest.raises(PlanParseError, match="absent"):
        parse_plan(payload, instruction=INSTRUCTION)
    payload["deck_sources"][0]["evidence"] = _quote("mix_reservoir: imaginary water")
    with pytest.raises(PlanParseError, match="absent"):
        parse_plan(payload, instruction=INSTRUCTION)


def test_parser_rejects_uncited_and_paper_only_on_deck_sources():
    payload = _empty_payload()
    payload["deck_sources"] = [_source("water", "water", "water_reservoir", "water to fill the volume")]
    payload["deck_sources"][0]["evidence"] = _quote("water to fill the volume", "paper")
    with pytest.raises(PlanParseError, match="on-deck inventory"):
        parse_plan(payload, instruction=INSTRUCTION, scientific_source=PAPER)
    payload["deck_sources"] = [_source("water", "water", "water_reservoir", "water source in slot 1")]
    with pytest.raises(PlanParseError, match="absent"):
        parse_plan(payload, instruction=INSTRUCTION, scientific_source=PAPER)
    payload["deck_sources"] = [_source("water", "water", "mix_reservoir",
                                       "`mix_reservoir`: Q5 master mix 2x")]
    with pytest.raises(PlanParseError, match="claimed component"):
        parse_plan(payload, instruction=INSTRUCTION, scientific_source=PAPER)


def test_reaction_audit_catches_missing_diluent_and_unlisted_deck_source():
    payload = _empty_payload()
    payload["deck_sources"] = _sources()
    payload["reactions"] = [_reaction()]
    plan = parse_plan(payload, instruction=INSTRUCTION, scientific_source=PAPER)
    assert {"final_volume_mismatch", "missing_diluent", "realized_strength_mismatch"} <= _codes(
        audit_plan(plan)
    )

    complete = PlannedReaction("PCR", 10, "water", (
        PlannedAddition(Addition("Q5 master mix", 5, 2, 1), "mix", (Evidence("paper", "mix"),)),
        PlannedAddition(Addition("primer pair", 1), "primer", (Evidence("paper", "primer"),)),
        PlannedAddition(Addition("colony template", 1), "template", (Evidence("paper", "template"),)),
        PlannedAddition(Addition("water", 3, is_diluent=True), "water", (Evidence("paper", "water"),)),
    ), (Evidence("paper", "reaction"),))
    plan = OT2Plan(plan.deck_sources, (complete,), (), ())
    assert "missing_deck_source" in _codes(audit_plan(plan))


def test_robot_reagents_need_named_pipetting_stages_and_tip_demand():
    reaction = PlannedReaction("PCR", 10, None, (
        PlannedAddition(Addition("Q5 master mix", 5), "mix", ()),
        PlannedAddition(Addition("primer pair", 1), "primer", ()),
    ), ())
    sources = (DeckSource("mix", "Q5 master mix", "mix_reservoir", ()),
               DeckSource("primer", "primer pair", "primer_plate", ()))
    stages = (
        Stage("Dispense Q5 master mix", "manual", None, None, None, None, None,
              None, None, None, ()),
        Stage("Add primer pair", "pipette", None, None, None, None, None,
              None, None, None, ()),
    )
    issues = audit_plan(OT2Plan(sources, (reaction,), (), stages))
    assert "robot_addition_stage_missing" in _codes(issues)
    assert "missing_stage_tip_demand" in _codes(issues)


def test_empty_initial_vessel_cannot_be_claimed_as_supplied_reagent():
    instruction = "tubes_1_5ml_1 well D1: empty at the start (PCR master mix for 8 reactions)."
    payload = {
        "deck_sources": [{
            "id": "master_mix", "component": "PCR master mix",
            "labware": "tubes_1_5ml_1",
            "evidence": [{"source": "instruction", "quote": instruction}],
        }],
        "reactions": [], "tip_budgets": [], "stages": [],
    }
    with pytest.raises(PlanParseError, match="initially empty vessel"):
        parse_plan(payload, instruction=instruction)


def test_generated_intermediate_requires_an_earlier_named_producer_and_consumer():
    instruction = """Use p20_single_gen2 with opentrons_96_tiprack_20ul.
## What is in the labware at the start
- mix_tube well A1: empty at the start (master mix preparation).
Prepare master mix in mix_tube well A1, then add 10 µL master mix to each PCR well.
"""
    quote = "mix_tube well A1: empty at the start (master mix preparation)"
    payload = _empty_payload()
    payload["deck_sources"] = [{
        "id": "mix", "component": "master mix", "labware": "mix_tube",
        "produced_by_stage": "Prepare master mix", "well": "A1",
        "evidence": _quote(quote),
    }]
    payload["reactions"] = [{
        "name": "PCR well", "final_volume_ul": 10, "diluent_name": None,
        "evidence": _quote("add 10 µL master mix to each PCR well"),
        "additions": [{
            "component": "master mix", "source_id": "mix", "volume_ul": 10,
            "stock_strength_x": None, "target_strength_x": None,
            "is_diluent": False, "delivery": "robot",
            "stage_name": "Dispense master mix",
            "evidence": _quote("add 10 µL master mix to each PCR well"),
        }],
    }]
    payload["stages"] = [
        _stage("Prepare master mix", "pipette", "Prepare master mix in mix_tube well A1"),
        _stage("Dispense master mix", "pipette", "add 10 µL master mix to each PCR well"),
    ]
    payload["reactions"][0]["output_source_id"] = None
    payload["stages"][1]["after_stage"] = "Prepare master mix"
    payload["tip_budgets"] = [{
        "pipette": "p20_single_gen2", "channels": 1,
        "tiprack_load_name": "opentrons_96_tiprack_20ul",
        "rack_count": 1, "tips_per_rack": 96, "planned_resets": 0,
        "refill_allowed": False,
        "evidence": _quote("Use p20_single_gen2 with opentrons_96_tiprack_20ul"),
        "demands": [{
            "stage": stage, "visits": 1, "fresh_tips_per_visit": 1,
            "shared_pickups": 0, "stroke_volumes_ul": [10],
            "evidence": _quote("add 10 µL master mix to each PCR well"),
        } for stage in ("Prepare master mix", "Dispense master mix")],
    }]
    plan = parse_plan(payload, instruction=instruction)
    assert audit_plan(plan) == ()
    assert "source_well_not_in_labware" in _codes(audit_plan(
        plan, geometry={"mix_tube": {"valid_wells": ["B1"]}},
    ))
    rendered = json.loads(plan_to_prompt(plan))
    assert rendered["deck_sources"][0]["produced_by_stage"] == "Prepare master mix"
    assert rendered["deck_sources"][0]["well"] == "A1"
    assert rendered["reactions"][0]["additions"][0]["stage_name"] == "Dispense master mix"
    assert rendered["reactions"][0]["output_source_id"] is None
    assert rendered["stages"][1]["after_stage"] == "Prepare master mix"
    payload["deck_sources"][0]["well"] = "B1"
    with pytest.raises(PlanParseError, match="absent from its inventory evidence"):
        parse_plan(payload, instruction=instruction)
    payload["deck_sources"][0]["well"] = "A1"

    payload["stages"].reverse()
    assert "intermediate_used_before_production" in _codes(
        audit_plan(parse_plan(payload, instruction=instruction)))
    payload["stages"].reverse()
    payload["reactions"][0]["additions"][0]["stage_name"] = None
    assert "intermediate_use_stage_missing" in _codes(
        audit_plan(parse_plan(payload, instruction=instruction)))
    payload["reactions"][0]["additions"][0]["stage_name"] = "Dispense master mix"
    payload["deck_sources"][0]["produced_by_stage"] = "Missing producer"
    assert "unknown_producer_stage" in _codes(
        audit_plan(parse_plan(payload, instruction=instruction)))


def test_off_deck_manual_handoff_can_produce_an_intermediate():
    source = DeckSource("cleaned", "cleaned fragment", "plate", (), "Column cleanup handoff")
    stages = (
        Stage("Column cleanup handoff", "manual", None, None, None, None, None,
              None, None, None, (Evidence("instruction", "off-deck column cleanup"),)),
        Stage("Use cleaned fragment", "pipette", None, None, None, None, None,
              None, None, None, ()),
    )
    reaction = PlannedReaction("assembly", 2, None, (
        PlannedAddition(Addition("cleaned fragment", 2), "cleaned", (), "Use cleaned fragment"),
    ), ())
    budget = TipBudget("p20_single_gen2", 1, 1, 96, 0, False, (
        TipDemand("Use cleaned fragment", 1, 1, 0, (), (2,)),
    ), ())
    assert audit_plan(OT2Plan((source,), (reaction,), (budget,), stages)) == ()


def test_plural_template_category_must_still_match_inventory_content():
    instruction = """## What is in the labware at the start
- template_plate wells A1:B1: linearized plasmid templates, 20 µL each.
"""
    payload = _empty_payload()
    payload["deck_sources"] = [_source("templates", "Templates", "template_plate",
                                       "template_plate wells A1:B1: linearized plasmid templates")]
    assert parse_plan(payload, instruction=instruction).deck_sources[0].component == "Templates"
    payload["deck_sources"][0]["component"] = "unlisted enzyme"
    with pytest.raises(PlanParseError, match="claimed component"):
        parse_plan(payload, instruction=instruction)


def test_reaction_audit_catches_wrong_two_x_fraction_even_when_total_balances():
    reaction = PlannedReaction("PCR", 10, "water", (
        PlannedAddition(Addition("2x mix", 4, 2, 1), "mix", ()),
        PlannedAddition(Addition("water", 6, is_diluent=True), "water", ()),
    ), ())
    sources = (DeckSource("mix", "2x mix", "mix_reservoir", ()),
               DeckSource("water", "water", "water_reservoir", ()))
    assert {"stock_dilution_mismatch", "realized_strength_mismatch"} <= _codes(
        audit_plan(OT2Plan(sources, (reaction,), (), ()))
    )


def test_manual_reaction_addition_is_visible_as_an_operator_step_warning():
    reaction = PlannedReaction("PCR", 10, "water", (
        PlannedAddition(Addition("water", 10, is_diluent=True, delivery="manual"), None, ()),
    ), ())
    issues = audit_plan(OT2Plan((), (reaction,), (), ()))
    warning = next(issue for issue in issues if issue.code == "manual_addition_required")
    assert warning.severity == "warning"


def test_audit_distinguishes_named_master_mixes_even_if_generic_words_overlap():
    reaction = PlannedReaction("PCR", 10, None, (
        PlannedAddition(Addition("Phire master mix", 5), "mix", ()),
        PlannedAddition(Addition("water", 5), "water", ()),
    ), ())
    sources = (DeckSource("mix", "Q5 master mix", "mix_reservoir", ()),
               DeckSource("water", "water", "water_reservoir", ()))
    assert "source_component_mismatch" in _codes(audit_plan(
        OT2Plan(sources, (reaction,), (), ()),
    ))


def test_tip_budget_counts_full_rack_loads_and_reset_permission():
    demands = (TipDemand("sample addition", 96, 1, 0, (), (5,)),
               TipDemand("sample mixing", 96, 1, 0, (), (5,)),
               TipDemand("template", 96, 1, 0, (), (1,)))
    stages = tuple(Stage(name, "pipette", None, None, None, None, None,
                         None, None, None, ()) for name in (
                             "sample addition", "sample mixing", "template"))
    budget = TipBudget("p20_single_gen2", 1, 1, 96, 0, True, demands, ())
    assert budget.pickups == 288
    assert budget.pickups_per_load == 96
    assert "insufficient_tips" in _codes(audit_plan(OT2Plan((), (), (budget,), stages)))
    replenished = TipBudget("p20_single_gen2", 1, 1, 96, 2, True, demands, ())
    assert audit_plan(OT2Plan((), (), (replenished,), stages)) == ()
    unauthorized = TipBudget("p20_single_gen2", 1, 1, 96, 2, False, demands, ())
    assert "unapproved_tip_refill" in _codes(audit_plan(OT2Plan((), (), (unauthorized,), stages)))


def test_multichannel_budget_uses_eight_positions_per_pickup():
    demands = (TipDemand("wash", 48, 1, 0, (), (200,)),)
    stages = (Stage("wash", "pipette", None, None, None, None, None, None, None, None, ()),)
    budget = TipBudget("p300_multi_gen2", 8, 3, 96, 0, False, demands, ())
    assert budget.pickups_per_load == 36
    assert "insufficient_tips" in _codes(audit_plan(OT2Plan((), (), (budget,), stages)))


def test_module_audit_requires_temperature_transition_before_recovery_pipetting():
    stages = (
        Stage("pre-chill", "set_temperature", "tc", "thermocycler", 4, None, None,
              None, None, None, ()),
        Stage("add DNA", "pipette", "tc", "thermocycler", None, None, None,
              4, "open", None, ()),
        Stage("heat pulse", "set_temperature", "tc", "thermocycler", 42, None, None,
              None, None, None, ()),
        Stage("add recovery medium", "pipette", "tc", "thermocycler", None, None, None,
              37, "open", None, ()),
    )
    budget = TipBudget("p20_single_gen2", 1, 1, 96, 0, False, (
        TipDemand("add DNA", 1, 1, 0, (), (1,)),
        TipDemand("add recovery medium", 1, 1, 0, (), (10,)),
    ), ())
    assert _codes(audit_plan(OT2Plan((), (), (budget,), stages))) == {"module_state_mismatch"}
    correct = stages[:3] + (
        Stage("cool", "set_temperature", "tc", "thermocycler", 37, None, None,
              None, None, None, ()),
    ) + stages[3:]
    assert audit_plan(OT2Plan((), (), (budget,), correct)) == ()


def test_module_audit_rejects_pipetting_under_closed_lid_and_wrong_magnet_state():
    stages = (
        Stage("close", "set_lid", "tc", "thermocycler", None, "closed", None,
              None, None, None, ()),
        Stage("add", "pipette", "tc", "thermocycler", None, None, None,
              None, None, None, ()),
        Stage("engage", "set_magnet", "mag", "magnetic", None, None, "engaged",
              None, None, None, ()),
        Stage("resuspend", "pipette", "mag", "magnetic", None, None, None,
              None, None, "disengaged", ()),
    )
    assert {"closed_lid_pipetting", "module_state_mismatch"} <= _codes(
        audit_plan(OT2Plan((), (), (), stages))
    )


def test_rendered_plan_is_compact_and_contains_no_paper_quotes():
    payload = _empty_payload()
    payload["deck_sources"] = _sources()
    payload["reactions"] = [_reaction()]
    plan = parse_plan(payload, instruction=INSTRUCTION, scientific_source=PAPER)
    rendered = plan_to_prompt(plan)
    content = json.loads(rendered)
    assert content["reactions"][0]["final_volume_ul"] == 10
    assert "evidence" not in rendered
    assert "final PCR reaction is 10 µL" not in rendered


def test_timed_module_stage_is_parsed_and_rendered_without_losing_duration():
    payload = _empty_payload()
    payload["stages"] = [_stage("incubate", "set_temperature", "Prepare 96 separate reactions",
                                module="tc", module_type="thermocycler", temp=37, duration=120)]
    plan = parse_plan(payload, instruction=INSTRUCTION)
    assert plan.stages[0].duration_s == 120
    assert json.loads(plan_to_prompt(plan))["stages"][0]["duration_s"] == 120


def test_parser_rejects_negative_budget_and_unrecognized_fields():
    payload = _empty_payload()
    payload["tip_budgets"] = [{
        "pipette": "p20_single_gen2", "channels": 1, "rack_count": -1,
        "tiprack_load_name": "opentrons_96_tiprack_20ul", "tips_per_rack": 96,
        "planned_resets": 0, "refill_allowed": False,
        "demands": [], "evidence": _quote("1 rack of 96 tips for p20_single_gen2"),
    }]
    with pytest.raises(PlanParseError, match="nonnegative"):
        parse_plan(payload, instruction=INSTRUCTION)
    payload = _empty_payload()
    payload["code"] = "print('not a plan')"
    with pytest.raises(PlanParseError, match="exactly"):
        parse_plan(payload, instruction=INSTRUCTION)


def test_empty_plan_is_rejected_by_semantic_audit():
    payload = _empty_payload()
    plan = parse_plan(payload, instruction=INSTRUCTION)
    assert _codes(audit_plan(plan)) == {"empty_stage_plan"}


def test_tip_refill_needs_cited_task_authorization():
    payload = _empty_payload()
    payload["tip_budgets"] = [{
        "pipette": "p20_single_gen2", "channels": 1, "rack_count": 1,
        "tiprack_load_name": "opentrons_96_tiprack_20ul", "tips_per_rack": 96,
        "planned_resets": 1, "refill_allowed": True,
        "demands": [], "evidence": _quote("1 rack of 96 tips for p20_single_gen2"),
    }]
    with pytest.raises(PlanParseError, match="refills without"):
        parse_plan(payload, instruction=INSTRUCTION)
    payload["tip_budgets"][0]["evidence"] = _quote("Tips are unlimited: call reset_tipracks")
    assert parse_plan(payload, instruction=INSTRUCTION).tip_budgets[0].refill_allowed


def test_tip_geometry_can_refute_claimed_rack_capacity():
    stage = Stage("wash", "pipette", None, None, None, None, None, None, None, None, ())
    budget = TipBudget("p300_multi_gen2", 8, 1, 96, 0, False,
                       (TipDemand("wash", 1, 1, 0, (), (100,)),), (), "opentrons_96_filtertiprack_200ul")
    geometry = {"opentrons_96_filtertiprack_200ul": {"well_count": 24, "is_tiprack": True}}
    assert "tiprack_geometry_mismatch" in _codes(audit_plan(
        OT2Plan((), (), (budget,), (stage,)), geometry=geometry,
    ))


def test_planned_stroke_volume_is_checked_against_pipette_and_tip_capacity():
    stage = Stage("dispense", "pipette", None, None, None, None, None, None, None, None, ())
    budget = TipBudget("p300_single_gen2", 1, 1, 96, 0, False,
                       (TipDemand("dispense", 1, 1, 0, (), (9, 250)),), (),
                       "opentrons_96_tiprack_200ul")
    issues = audit_plan(OT2Plan((), (), (budget,), (stage,)))
    assert [issue.code for issue in issues].count("pipette_range_mismatch") == 2
