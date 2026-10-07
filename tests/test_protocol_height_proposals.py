"""The geometry helper offers estimates without silently certifying motion."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pybravo.deck.labware import LabwareDefinition, _read_labware_snapshot

from pybravo.workflow.protocols.height_proposals import (
    propose_aspiration_height,
    propose_noncontact_dispense_height,
    propose_transfer_heights,
)


SOURCE = {
    "id": "01KD4PYY7N4EG0E22P3QP1RHB9", "name": "384 Labcyte PP0200 PP sq flt",
    "well_depth_mm": 11.4, "well_diameter_mm": 3.5, "well_volume_ul": 130.0,
    "well_geometry": "square",
}
DESTINATION = {
    "id": "lw-3918306f45b8", "name": "1536 Labcyte LP-0400 LDV",
    "well_depth_mm": 5.2, "well_diameter_mm": 1.7, "well_volume_ul": 5.5,
    "well_geometry": "square",
}
CLASS = {
    "liquid_class_id": "vendor-384-st10",
    "aspirate": {
        "z_in_velocity_mm_s": 40.0, "z_in_acceleration_mm_s2": 100.0,
        "z_out_velocity_mm_s": 10.0, "z_out_acceleration_mm_s2": 2.0,
    },
    "dispense": {
        "z_in_velocity_mm_s": 40.0, "z_in_acceleration_mm_s2": 100.0,
        "z_out_velocity_mm_s": 5.0, "z_out_acceleration_mm_s2": 5.0,
    },
    "source_field_origins": {
        "aspirate.z_in_velocity_mm_s": "vendor_archive",
        "dispense.z_in_velocity_mm_s": "vendor_archive",
    },
}


def test_four_source_transfer_source_has_heuristic_height_but_ldv_is_unresolved():
    originals = deepcopy((SOURCE, DESTINATION, CLASS))
    result = propose_transfer_heights(
        SOURCE, DESTINATION,
        source_initial_volume_ul=45.0,
        total_source_withdrawal_ul=10.0,
        source_dead_volume_ul=6.5,
        destination_initial_volume_ul=0.0,
        dispense_volume_ul=5.0,
        liquid_class=CLASS,
    )
    source = result["aspirate"]
    assert source["status"] == "proposed_for_review"
    assert 1 < source["distance_from_bottom_mm"] < 2
    assert source["residual_volume_ul"] == 35.0
    assert source["requires_method_review"] is True
    assert source["motion"]["z_in_velocity_mm_s"] == 40.0
    assert source["motion"]["field_origins"]["z_in_velocity_mm_s"] == "vendor_archive"

    destination = result["dispense"]
    assert destination["status"] == "unresolved"
    assert destination["distance_from_bottom_mm"] is None
    assert destination["capacity_fraction"] == 0.9091
    assert destination["nominal_headspace_mm"] < 0.5
    assert len(destination["reasons"]) == 2
    assert destination["motion"]["z_in_velocity_mm_s"] == 40.0
    assert destination["motion"]["geometry_derived"] is False
    assert result["complete_height_proposal"] is False
    assert result["physical_safety_established"] is False
    assert (SOURCE, DESTINATION, CLASS) == originals


def test_flat_well_with_generous_headspace_receives_reviewable_dispense_height():
    well = {"well_depth_mm": 10.0, "well_diameter_mm": 3.0, "well_volume_ul": 90.0,
            "well_geometry": "square"}
    result = propose_noncontact_dispense_height(
        well, initial_volume_ul=0.0, dispense_volume_ul=18.0)
    assert result["status"] == "proposed_for_review"
    assert result["distance_from_bottom_mm"] == 3.0
    assert result["motion"]["status"] == "unresolved"
    assert result["requires_method_review"] is True


def test_near_full_even_uniform_well_has_no_noncontact_height():
    well = {"well_depth_mm": 10.0, "well_diameter_mm": 3.0, "well_volume_ul": 90.0,
            "well_geometry": "square"}
    result = propose_noncontact_dispense_height(
        well, initial_volume_ul=0.0, dispense_volume_ul=85.0, liquid_class=CLASS)
    assert result["status"] == "unresolved"
    assert result["distance_from_bottom_mm"] is None
    assert any("headspace" in reason for reason in result["reasons"])
    assert result["motion"]["z_in_velocity_mm_s"] == 40.0


def test_source_below_dead_volume_and_missing_motion_remain_unresolved():
    result = propose_aspiration_height(
        SOURCE, initial_volume_ul=10.0, total_withdrawal_ul=5.0,
        dead_volume_ul=6.5)
    assert result["status"] == "unresolved"
    assert result["motion"]["status"] == "unresolved"
    assert "dead volume" in result["reasons"][0]


def test_bad_geometry_or_implicit_class_motion_is_not_filled_by_defaults():
    bad = {"well_depth_mm": 5.2, "well_volume_ul": 5.5}
    result = propose_noncontact_dispense_height(
        bad, initial_volume_ul=0.0, dispense_volume_ul=5.0,
        liquid_class={"liquid_class_id": "incomplete", "dispense": {"z_in_velocity_mm_s": 40}})
    assert result["status"] == "unresolved"
    assert result["geometry"] is None
    assert result["motion"]["status"] == "unresolved"


def test_unknown_shape_does_not_silently_use_square_area():
    well = {"well_depth_mm": 10.0, "well_diameter_mm": 3.0, "well_volume_ul": 90.0}
    result = propose_noncontact_dispense_height(
        well, initial_volume_ul=0.0, dispense_volume_ul=18.0)
    assert result["status"] == "unresolved"
    assert result["distance_from_bottom_mm"] is None
    assert "known well shape" in result["reasons"][0]


def test_editor_shape_codes_normalize_to_summary_and_round_area():
    plate = LabwareDefinition.from_mongo({
        "labware_type_id": "round-test", "name": "Round test plate", "base_class": "microplate",
        "wells": 96, "well_dimensions_mm": {
            "depth_mm": 10.0, "diameter_mm": 3.0, "volume_ul": 70.6858,
            "well_geometry": 1,
        },
    })
    summary = plate.to_summary()
    assert summary["well_geometry"] == "round"
    proposal = propose_noncontact_dispense_height(
        summary, initial_volume_ul=0.0, dispense_volume_ul=14.0)
    assert proposal["status"] == "proposed_for_review"
    assert proposal["geometry"]["modeled_max_section_mm2"] == pytest.approx(7.06858, rel=1e-5)
    assert LabwareDefinition(id="square", name="Square", kind="sbs_plate", well_geometry=2).well_geometry == "square"
    assert LabwareDefinition(id="unknown", name="Unknown", kind="sbs_plate").well_geometry is None


def test_local_snapshot_preserves_recorded_labcyte_shapes_for_active_context():
    snapshot = Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml"
    rows = {row.id: row.to_summary() for row in _read_labware_snapshot(snapshot)}
    assert rows[SOURCE["id"]]["well_geometry"] == "square"
    assert rows[DESTINATION["id"]]["well_geometry"] == "square"
    proposal = propose_transfer_heights(
        rows[SOURCE["id"]], rows[DESTINATION["id"]],
        source_initial_volume_ul=45.0, total_source_withdrawal_ul=10.0,
        source_dead_volume_ul=6.5, destination_initial_volume_ul=0.0,
        dispense_volume_ul=5.0,
    )
    assert proposal["aspirate"]["distance_from_bottom_mm"] == pytest.approx(1.694)
    assert proposal["dispense"]["status"] == "unresolved"
