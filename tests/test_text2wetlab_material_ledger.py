"""Synthetic accounting cases; no benchmark protocol content is authored here."""

from __future__ import annotations

import pytest

from pybravo.evals.text2wetlab.material_ledger import (
    InitialWell,
    StageBoundary,
    WellLimit,
    WellRef,
    audit_material_flow,
)


def _stroke(kind: str, labware: str, well: str, volume: float, *, instrument: str = "pipette",
            channels: int = 1) -> dict:
    return {"kind": kind, "instrument": instrument, "channels": channels,
            "labware": labware, "well": well, "volume": volume}


def _tip(kind: str, *, instrument: str = "pipette", channels: int = 1) -> dict:
    return {"kind": kind, "instrument": instrument, "channels": channels}


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_one_aspiration_can_distribute_to_two_wells_with_source_provenance() -> None:
    source = WellRef("stock rack on 1", "A1")
    first = WellRef("plate on 2", "A1")
    second = WellRef("plate on 2", "A2")
    events = [
        _tip("pick"),
        _stroke("aspirate", source.labware, source.well, 10),
        _stroke("dispense", first.labware, first.well, 4),
        _stroke("dispense", second.labware, second.well, 6),
        _tip("drop"),
    ]
    result = audit_material_flow(events, initial_wells={
        source: InitialWell(25, material_id="buffer"),
        first: InitialWell(0),
        second: InitialWell(0),
    })
    assert not result.has_errors
    assert result.final_wells[source].volume_ul == pytest.approx(15)
    assert result.final_wells[first].components_ul == {"buffer": 4}
    assert result.final_wells[second].components_ul == {"buffer": 6}
    assert result.final_wells[first].observed_net_change_ul == pytest.approx(4)
    assert result.snapshots[-1].name == "final"
    assert result.snapshots[-1].tips == {}


def test_intermediate_composition_and_ordered_snapshots_follow_explicit_mix() -> None:
    water = WellRef("stocks on 1", "A1")
    reagent = WellRef("stocks on 1", "A2")
    premix = WellRef("tube rack on 2", "B1")
    reaction_a = WellRef("plate on 3", "A1")
    reaction_b = WellRef("plate on 3", "A2")
    events = [
        _tip("pick"),
        _stroke("aspirate", water.labware, water.well, 20),
        _stroke("dispense", premix.labware, premix.well, 20),
        _tip("drop"),
        _tip("pick"),
        _stroke("aspirate", reagent.labware, reagent.well, 10),
        _stroke("dispense", premix.labware, premix.well, 10),
        {"kind": "mix", "instrument": "pipette", "labware": premix.labware, "well": premix.well},
        _tip("drop"),
        _tip("pick"),
        _stroke("aspirate", premix.labware, premix.well, 15),
        _stroke("dispense", reaction_a.labware, reaction_a.well, 5),
        _stroke("dispense", reaction_b.labware, reaction_b.well, 10),
        _tip("drop"),
    ]
    result = audit_material_flow(
        events,
        initial_wells={water: InitialWell(50, "water"), reagent: InitialWell(20, "reagent"),
                       premix: InitialWell(0), reaction_a: InitialWell(0), reaction_b: InitialWell(0)},
        stages=(StageBoundary("premix_ready", 8), StageBoundary("first_reaction", 11)),
        stage_targets_ul={"premix_ready": {premix: 30}},
    )
    assert not result.has_errors
    assert [snapshot.name for snapshot in result.snapshots] == ["premix_ready", "first_reaction", "final"]
    assert result.snapshots[0].wells[premix].components_ul == {"water": 20, "reagent": 10}
    assert result.snapshots[0].wells[premix].volume_ul == pytest.approx(30)
    assert result.snapshots[1].wells[reaction_a].volume_ul == pytest.approx(5)
    assert result.final_wells[premix].volume_ul == pytest.approx(15)
    assert result.final_wells[reaction_a].components_ul == pytest.approx({"water": 10 / 3, "reagent": 5 / 3})
    assert result.final_wells[reaction_b].components_ul == pytest.approx({"water": 20 / 3, "reagent": 10 / 3})


def test_sequential_tip_aspiration_preserves_full_mass_but_partial_layered_dispense_is_uncertain() -> None:
    a = WellRef("stocks on 1", "A1")
    b = WellRef("stocks on 1", "A2")
    first = WellRef("plate on 2", "A1")
    second = WellRef("plate on 2", "A2")
    events = [
        _tip("pick"),
        _stroke("aspirate", a.labware, a.well, 5),
        _stroke("aspirate", b.labware, b.well, 5),
        _stroke("dispense", first.labware, first.well, 5),
        _stroke("dispense", second.labware, second.well, 5),
        _tip("drop"),
    ]
    inventory = {a: InitialWell(20, "a"), b: InitialWell(20, "b"),
                 first: InitialWell(0), second: InitialWell(0)}
    result = audit_material_flow(events, initial_wells=inventory)
    assert not result.has_errors
    assert result.final_wells[first].volume_ul == pytest.approx(5)
    assert result.final_wells[first].components_ul is None
    assert result.final_wells[second].components_ul is None
    assert result.final_wells[first].possible_source_ids == ("a", "b")

    full = audit_material_flow(events[:3] + [
        _stroke("dispense", first.labware, first.well, 10), _tip("drop")
    ], initial_wells=inventory)
    assert full.final_wells[first].components_ul == {"a": 5, "b": 5}


def test_repeated_preparation_exceeds_supplied_stage_target_even_if_later_consumed() -> None:
    a = WellRef("stocks on 1", "A1")
    b = WellRef("stocks on 1", "A2")
    mix = WellRef("tube rack on 2", "A1")
    destination = WellRef("plate on 3", "A1")
    preparation = [
        _tip("pick"), _stroke("aspirate", a.labware, a.well, 10),
        _stroke("dispense", mix.labware, mix.well, 10), _tip("drop"),
        _tip("pick"), _stroke("aspirate", b.labware, b.well, 5),
        _stroke("dispense", mix.labware, mix.well, 5), _tip("drop"),
    ]
    events = preparation + preparation + [
        _tip("pick"), _stroke("aspirate", mix.labware, mix.well, 30),
        _stroke("dispense", destination.labware, destination.well, 30), _tip("drop"),
    ]
    result = audit_material_flow(
        events,
        initial_wells={a: InitialWell(100, "a"), b: InitialWell(100, "b"),
                       mix: InitialWell(0), destination: InitialWell(0)},
        stages=(StageBoundary("first_preparation", 7), StageBoundary("prepared", 15)),
        stage_targets_ul={"first_preparation": {mix: 15}, "prepared": {mix: 15}},
    )
    assert result.snapshots[0].wells[mix].volume_ul == pytest.approx(15)
    assert result.snapshots[1].wells[mix].volume_ul == pytest.approx(30)
    assert result.final_wells[mix].volume_ul == pytest.approx(0)
    assert "stage_volume_over_target" in _codes(result)
    assert result.has_errors


def test_final_reaction_target_and_physical_capacity_are_independent_constraints() -> None:
    stock = WellRef("stock on 1", "A1")
    reaction = WellRef("plate on 2", "A1")
    events = [
        _tip("pick"), _stroke("aspirate", stock.labware, stock.well, 15),
        _stroke("dispense", reaction.labware, reaction.well, 15), _tip("drop"),
        _tip("pick"), _stroke("aspirate", stock.labware, stock.well, 15),
        _stroke("dispense", reaction.labware, reaction.well, 15), _tip("drop"),
    ]
    result = audit_material_flow(
        events,
        initial_wells={stock: InitialWell(100), reaction: InitialWell(0)},
        well_limits={reaction: WellLimit(max_volume_ul=25, target_final_volume_ul=20)},
    )
    assert result.final_wells[reaction].volume_ul == pytest.approx(30)
    assert {"well_capacity_exceeded", "target_volume_overfill"} <= _codes(result)


def test_unmeasured_quantities_do_not_become_exact_final_volumes() -> None:
    stock = WellRef("stock on 1", "A1")
    target = WellRef("possibly prefilled on 2", "A1")
    result = audit_material_flow([
        _tip("pick"), _stroke("aspirate", stock.labware, stock.well, 5),
        _stroke("dispense", target.labware, target.well, 5), _tip("drop"),
    ], well_limits={target: WellLimit(target_final_volume_ul=5)})
    assert result.final_wells[stock].volume_ul is None
    assert result.final_wells[target].volume_ul is None
    assert result.final_wells[target].observed_net_change_ul == pytest.approx(5)
    assert {"unmeasured_initial_source", "unmeasured_initial_destination",
            "unverifiable_final_volume"} <= _codes(result)
    assert not result.has_errors


def test_underdraw_and_overdispense_are_reported_without_creating_liquid() -> None:
    source = WellRef("stock on 1", "A1")
    target = WellRef("plate on 2", "A1")
    result = audit_material_flow([
        _tip("pick"), _stroke("aspirate", source.labware, source.well, 7),
        _stroke("dispense", target.labware, target.well, 3), _tip("drop"),
        _tip("pick"), _stroke("aspirate", source.labware, source.well, 4),
        _stroke("dispense", target.labware, target.well, 5), _tip("drop"),
    ], initial_wells={source: InitialWell(10), target: InitialWell(0)})
    assert {"source_volume_exhausted", "dispense_exceeds_held"} <= _codes(result)
    assert result.final_wells[source].volume_ul == pytest.approx(3)
    assert result.final_wells[target].volume_ul == pytest.approx(3)


def test_multichannel_flow_requires_explicit_geometry_and_accounts_each_channel() -> None:
    reservoir = WellRef("reservoir on 1", "A1")
    plate_a = WellRef("plate on 2", "A1")
    plate_b = WellRef("plate on 2", "B1")
    events = [
        _tip("pick", channels=2),
        _stroke("aspirate", reservoir.labware, reservoir.well, 3, channels=2),
        _stroke("dispense", plate_a.labware, plate_a.well, 3, channels=2),
        _tip("drop", channels=2),
    ]
    inventory = {reservoir: InitialWell(10, "reagent"), plate_a: InitialWell(0), plate_b: InitialWell(0)}
    unresolved = audit_material_flow(events, initial_wells=inventory)
    assert "unresolved_wells" in _codes(unresolved)
    assert unresolved.final_wells[reservoir].volume_ul == pytest.approx(10)

    def wells(event: dict, count: int) -> tuple[WellRef, ...]:
        assert count == 2
        if event["labware"] == reservoir.labware:
            return (reservoir, reservoir)
        return (plate_a, plate_b)

    resolved = audit_material_flow(events, initial_wells=inventory, well_resolver=wells)
    assert not resolved.has_errors
    assert resolved.final_wells[reservoir].volume_ul == pytest.approx(4)
    assert resolved.final_wells[plate_a].components_ul == {"reagent": 3}
    assert resolved.final_wells[plate_b].components_ul == {"reagent": 3}


def test_invalid_inventory_and_stage_metadata_fail_closed() -> None:
    well = WellRef("plate", "A1")
    with pytest.raises(ValueError, match="component amounts"):
        audit_material_flow([], initial_wells={well: InitialWell(5, components_ul={"a": 4})})
    with pytest.raises(ValueError, match="stage target"):
        audit_material_flow([], stage_targets_ul={"missing": {well: 5}})
    with pytest.raises(ValueError, match="increasing"):
        audit_material_flow([{}], stages=(StageBoundary("later", 0), StageBoundary("earlier", -1)))


def test_stage_boundary_is_retained_when_an_event_is_invalid() -> None:
    result = audit_material_flow(
        [_stroke("aspirate", "stocks", "A1", 5)],
        stages=(StageBoundary("attempted_aspiration", 0),),
    )
    assert [item.name for item in result.snapshots] == ["attempted_aspiration", "final"]
    assert "no_tip" in _codes(result)
