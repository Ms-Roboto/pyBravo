"""Method lookup must never turn a plausible legacy class into a qualified run."""

from __future__ import annotations

import pytest
import yaml

from pybravo.workflow.protocols.methods import (
    MethodConflictError,
    MethodValidationError,
    _digest,
    get_method,
    lookup_methods,
    method_registry,
    save_reviewed_method,
)

MOTION = {
    "w_velocity_ul_s": 1.0,
    "w_acceleration_ul_s2": 2.0,
    "post_delay_ms": 0,
    "z_in_velocity_mm_s": 30.0,
    "z_in_acceleration_mm_s2": 50.0,
    "z_out_velocity_mm_s": 20.0,
    "z_out_acceleration_mm_s2": 40.0,
}


def _context(liquid_class):
    return {
        "machine_id": "machine-1",
        "head_type": "HT_384_D_70",
        "liquid_classes": [liquid_class],
        "tip_definitions": [
            {"id": "st_10ul", "tip_id": "st_10ul", "capacity_ul": 10.0, "compatible_heads": ["HT_384_D_70"]},
            {"id": "st_70ul", "tip_id": "st_70ul", "capacity_ul": 70.0, "compatible_heads": ["HT_384_D_70"]},
        ],
        "tipbox_choices": [
            {"labware_id": "rack-10", "tip_definition_id": "st_10ul", "execution_ready": True},
            {"labware_id": "rack-70", "tip_definition_id": "st_70ul", "execution_ready": True},
        ],
        "labware": [
            {
                "id": "source-384",
                "base_class": "microplate",
                "rows": 16,
                "cols": 24,
                "wells": 384,
                "spacing_x_mm": 4.5,
                "spacing_y_mm": 4.5,
                "well_depth_mm": 10.0,
                "well_volume_ul": 100.0,
            },
            {
                "id": "dest-1536",
                "base_class": "microplate",
                "rows": 32,
                "cols": 48,
                "wells": 1536,
                "spacing_x_mm": 2.25,
                "spacing_y_mm": 2.25,
                "well_depth_mm": 6.0,
                "well_volume_ul": 10.0,
            },
        ],
    }


def _class():
    return {
        "liquid_class_id": "class-10",
        "name": "Reviewed class",
        "machine_id": "machine-1",
        "head_type": "HT_384_D_70",
        "tip_id": "st_10ul",
        "tip_capacity_ul": 10.0,
        "aspirate": dict(MOTION),
        "dispense": dict(MOTION),
        "equation": {
            "control_points": [
                {"desired_ul": 0.0, "commanded_ul": 0.0},
                {"desired_ul": 10.0, "commanded_ul": 10.0},
            ]
        },
    }


def _query(**changes):
    value = {
        "tip_id": "st_10ul",
        "tipbox_id": "rack-10",
        "source_labware_id": "source-384",
        "destination_labware_id": "dest-1536",
        "reagent_family": "aqueous",
        "volume_ul": 5.0,
    }
    value.update(changes)
    return value


def _reviewed_method(liquid_class):
    ref = {"id": liquid_class["liquid_class_id"], "name": liquid_class["name"], "digest": _digest(liquid_class)}
    return {
        "method_id": "aqueous-st10",
        "version": "1.0.0",
        "status": "reviewed",
        "title": "Reviewed aqueous ST10 transfer",
        "applicability": {
            "machine_ids": ["machine-1"],
            "head_types": ["HT_384_D_70"],
            "tip_ids": ["st_10ul"],
            "tipbox_ids": ["rack-10"],
            "source_labware_ids": ["source-384"],
            "destination_labware_ids": ["dest-1536"],
            "reagent_families": ["aqueous"],
            "operations": ["transfer", "distribute"],
            "min_volume_ul": 1.0,
            "max_volume_ul": 10.0,
        },
        "aspirate": {
            "liquid_class_ref": ref,
            "distance_from_bottom_mm": 1.0,
            "pre_air_ul": 0.0,
            "post_air_ul": 0.0,
            "dynamic_tip_extension": 0.0,
            "tip_touch": False,
            "well_offset": {"x_fraction_of_well_radius": 0.0, "y_fraction_of_well_radius": 0.0},
        },
        "dispense": {
            "liquid_class_ref": ref,
            "distance_from_bottom_mm": 1.0,
            "blowout_ul": 0.0,
            "dynamic_tip_retraction": 0.0,
            "tip_touch": False,
            "well_offset": {"x_fraction_of_well_radius": 0.0, "y_fraction_of_well_radius": 0.0},
        },
        "tip_policy": "fresh_per_source",
        "evidence": [
            {
                "source_type": "local_review",
                "reviewer": "test scientist",
                "reviewed_at": "2026-10-05",
                "note": "Test fixture, not physical qualification.",
            }
        ],
    }


def _stores(monkeypatch, tmp_path, liquid_class, methods):
    class_path = tmp_path / "liquid_classes.yaml"
    class_path.write_text(yaml.safe_dump({"version": 1, "liquid_classes": [liquid_class]}))
    method_path = tmp_path / "protocol_methods.yaml"
    method_path.write_text(yaml.safe_dump({"version": 1, "methods": methods}))
    monkeypatch.setenv("PYBRAVO_LIQUID_CLASS_STORE_PATH", str(class_path))
    monkeypatch.setenv("PYBRAVO_METHOD_STORE_PATH", str(method_path))


def test_imported_class_with_inferred_tip_and_missing_motion_stays_incomplete(monkeypatch, tmp_path):
    normalized = _class()
    raw = _class()
    raw["tip_id"] = ""
    del raw["aspirate"]["z_out_velocity_mm_s"]
    _stores(monkeypatch, tmp_path, raw, [])
    record = get_method(_context(normalized), "imported:class-10")
    assert record["execution_ready"] is False
    assert record["applicability"]["tip_ids"] == []
    assert record["aspirate"]["liquid_class_ref"]["field_origins"]["tip_id"] == "inferred_or_unknown"
    assert (
        record["aspirate"]["liquid_class_ref"]["field_origins"]["aspirate.z_out_velocity_mm_s"]
        == "normalizer_default_or_unknown"
    )


def test_exact_reviewed_method_pins_revision_and_soft_volume_mismatch(monkeypatch, tmp_path):
    liquid_class = _class()
    _stores(monkeypatch, tmp_path, liquid_class, [_reviewed_method(liquid_class)])
    context = _context(liquid_class)
    registry = method_registry(context)
    reviewed = get_method(context, "aqueous-st10")
    assert reviewed["execution_ready"] is True
    assert get_method(context, "aqueous-st10", reviewed["revision"]) == reviewed
    assert get_method(context, "aqueous-st10", "stale-revision") is None
    assert len(registry["digest"]) == 64
    exact = lookup_methods(context, _query())["candidates"][0]
    assert exact["method_ref"] == {"method_id": "aqueous-st10", "revision": reviewed["revision"]}
    assert exact["match_kind"] == "exact"
    near = lookup_methods(context, _query(volume_ul=0.5))["candidates"][0]
    assert near["match_kind"] == "closest"
    assert any(item["field"] == "volume_ul" for item in near["mismatches"])


def test_hard_tip_and_plate_constraints_precede_ranking(monkeypatch, tmp_path):
    liquid_class = _class()
    _stores(monkeypatch, tmp_path, liquid_class, [_reviewed_method(liquid_class)])
    context = _context(liquid_class)
    over_capacity = lookup_methods(context, _query(volume_ul=11.0))
    assert not over_capacity["candidates"]
    assert any(issue["field"] == "volume_ul" for issue in over_capacity["issues"])
    incompatible = lookup_methods(context, _query(tip_id="st_70ul", tipbox_id="rack-70"))
    assert not incompatible["candidates"]
    assert any(issue["field"] == "destination_labware_id" for issue in incompatible["issues"])
    wrong_box = lookup_methods(context, _query(tipbox_id="rack-70"))
    assert any(issue["field"] == "tipbox_id" for issue in wrong_box["issues"])
    context["head_max_volume_ul"] = 4.0
    head_limited = lookup_methods(context, _query())
    assert any(issue["field"] == "volume_ul" for issue in head_limited["issues"])
    context.pop("head_max_volume_ul")
    context["labware"][1]["well_volume_ul"] = 4.0
    plate_limited = lookup_methods(context, _query())
    assert any(issue["field"] == "destination_labware_id" for issue in plate_limited["issues"])


def test_distribute_compares_each_aliquot_to_destination_capacity(monkeypatch, tmp_path):
    liquid_class = _class()
    _stores(monkeypatch, tmp_path, liquid_class, [_reviewed_method(liquid_class)])
    context = _context(liquid_class)
    context["labware"][1]["well_volume_ul"] = 6.0
    okay = lookup_methods(context, _query(operation="distribute", volume_ul=10.0, dispense_volumes_ul=[5.0, 5.0]))
    assert not okay["issues"]
    overflow = lookup_methods(context, _query(operation="distribute", volume_ul=10.0, dispense_volumes_ul=[7.0, 3.0]))
    assert any(item["field"] == "destination_labware_id" for item in overflow["issues"])
    wrong_total = lookup_methods(
        context, _query(operation="distribute", volume_ul=10.0, dispense_volumes_ul=[5.0, 4.0])
    )
    assert any(item["field"] == "dispense_volumes_ul" for item in wrong_total["issues"])
    paired = lookup_methods(context, _query(operation="distribute", volume_ul=12.0, dispense_volumes_ul=[6.0, 6.0]))
    assert not paired["issues"]
    assert paired["shared_aspiration_feasible"] is False
    assert paired["candidates"]
    assert any(item["field"] == "shared_aspiration_capacity" for item in paired["candidates"][0]["mismatches"])
    assert not any(item["field"] == "volume_ul" for item in paired["candidates"][0]["mismatches"])


def test_mix_lookup_does_not_require_a_destination_plate(monkeypatch, tmp_path):
    liquid_class = _class()
    method = _reviewed_method(liquid_class)
    method["applicability"]["operations"].append("mix")
    _stores(monkeypatch, tmp_path, liquid_class, [method])
    result = lookup_methods(
        _context(liquid_class),
        _query(operation="mix", destination_labware_id=None),
    )
    assert not result["issues"]
    candidate = next(row for row in result["candidates"] if row["method_id"] == "aqueous-st10")
    assert candidate["match_kind"] == "exact"
    assert candidate["mismatches"] == []


def test_reference_publications_cannot_become_executable(monkeypatch, tmp_path):
    liquid_class = _class()
    _stores(
        monkeypatch,
        tmp_path,
        liquid_class,
        [
            {
                "method_id": "reference:test",
                "version": "1.0.0",
                "status": "reference_candidate",
                "title": "Publication only",
                "applicability": {"head_types": ["HT_384_D_70"]},
                "evidence": [{"source_type": "publication", "source_url": "https://example.org/paper"}],
            }
        ],
    )
    candidate = next(
        row
        for row in lookup_methods(_context(liquid_class), _query())["candidates"]
        if row["method_id"] == "reference:test"
    )
    assert candidate["status"] == "reference_candidate"
    assert candidate["execution_ready"] is False
    assert "status.review_required" in candidate["missing_fields"]


def test_measured_origin_is_preserved_but_missing_raw_value_stays_unknown(monkeypatch, tmp_path):
    liquid_class = _class()
    raw = _class()
    raw["field_origins"] = {"aspirate.w_velocity_ul_s": "measured"}
    _stores(monkeypatch, tmp_path, raw, [_reviewed_method(liquid_class)])
    context = _context(liquid_class)
    imported = get_method(context, "imported:class-10")
    assert imported["aspirate"]["liquid_class_ref"]["field_origins"]["aspirate.w_velocity_ul_s"] == "measured"
    assert get_method(context, "aqueous-st10")["execution_ready"] is True

    del raw["aspirate"]["w_velocity_ul_s"]
    _stores(monkeypatch, tmp_path, raw, [_reviewed_method(liquid_class)])
    reviewed = get_method(context, "aqueous-st10")
    assert reviewed["execution_ready"] is False
    assert "aspirate.liquid_class_unverified_fields" in reviewed["missing_fields"]


def test_normalizer_replacement_and_missing_calibration_do_not_claim_provenance(monkeypatch, tmp_path):
    liquid_class = _class()
    raw = _class()
    raw["aspirate"]["w_velocity_ul_s"] = 0.0  # legacy normalizer would replace this with 100
    raw["equation"] = {}
    _stores(monkeypatch, tmp_path, raw, [_reviewed_method(liquid_class)])
    context = _context(liquid_class)
    imported = get_method(context, "imported:class-10")
    origins = imported["aspirate"]["liquid_class_ref"]["field_origins"]
    assert origins["aspirate.w_velocity_ul_s"] == "normalizer_default_or_unknown"
    assert origins["equation.control_points"] == "normalizer_default_or_unknown"
    reviewed = get_method(context, "aqueous-st10")
    assert "aspirate.liquid_class_unverified_fields" in reviewed["missing_fields"]


def test_scientist_curation_is_atomic_and_versioned(monkeypatch, tmp_path):
    liquid_class = _class()
    _stores(monkeypatch, tmp_path, liquid_class, [])
    context = _context(liquid_class)
    source = get_method(context, "imported:class-10")
    payload = _reviewed_method(liquid_class)
    payload["derived_from"] = {"method_id": source["method_id"], "revision": source["revision"]}
    before = method_registry(context)["digest"]
    saved = save_reviewed_method(context, payload, expected_registry_digest=before)
    assert saved["execution_ready"] is True
    assert saved["derived_from"] == payload["derived_from"]
    assert get_method(context, payload["method_id"], saved["revision"]) == saved
    stored = yaml.safe_load((tmp_path / "protocol_methods.yaml").read_text())
    assert [row["version"] for row in stored["methods"]] == ["1.0.0"]

    with pytest.raises(MethodConflictError, match="changed"):
        save_reviewed_method(context, payload, expected_registry_digest=before)
    new_payload = _reviewed_method(liquid_class)
    new_payload["version"] = "1.0.1"
    with pytest.raises(MethodConflictError, match="revision"):
        save_reviewed_method(context, new_payload, expected_registry_digest=method_registry(context)["digest"])
    updated = save_reviewed_method(
        context,
        new_payload,
        expected_registry_digest=method_registry(context)["digest"],
        expected_method_revision=saved["revision"],
    )
    assert get_method(context, payload["method_id"])["revision"] == updated["revision"]
    assert [row["version"] for row in yaml.safe_load((tmp_path / "protocol_methods.yaml").read_text())["methods"]] == [
        "1.0.0",
        "1.0.1",
    ]


def test_curation_requires_complete_values_and_explicit_default_attestations(monkeypatch, tmp_path):
    liquid_class = _class()
    raw = _class()
    raw["tip_id"] = ""
    del raw["aspirate"]["w_velocity_ul_s"]
    _stores(monkeypatch, tmp_path, raw, [])
    context = _context(liquid_class)
    payload = _reviewed_method(liquid_class)
    digest = method_registry(context)["digest"]
    with pytest.raises(MethodValidationError) as error:
        save_reviewed_method(context, payload, expected_registry_digest=digest)
    assert "aspirate.liquid_class_unverified_fields" in error.value.missing_fields
    assert yaml.safe_load((tmp_path / "protocol_methods.yaml").read_text())["methods"] == []

    imported = get_method(context, "imported:class-10")
    origins = imported["aspirate"]["liquid_class_ref"]["source_field_origins"]
    attestations = {
        key: "reviewed_inferred" if key == "tip_id" else "reviewed_default"
        for key, origin in origins.items()
        if origin in {"normalizer_default_or_unknown", "inferred_or_unknown"}
    }
    for phase in ("aspirate", "dispense"):
        payload[phase]["liquid_class_ref"]["field_origins"] = attestations
    saved = save_reviewed_method(context, payload, expected_registry_digest=digest)
    assert saved["execution_ready"] is True
    ref = saved["aspirate"]["liquid_class_ref"]
    assert ref["field_origins"]["aspirate.w_velocity_ul_s"] == "reviewed_default"
    assert ref["source_field_origins"]["aspirate.w_velocity_ul_s"] == "normalizer_default_or_unknown"


def test_scientist_review_requires_a_source_or_rationale_note(monkeypatch, tmp_path):
    liquid_class = _class()
    _stores(monkeypatch, tmp_path, liquid_class, [])
    payload = _reviewed_method(liquid_class)
    payload["evidence"][0].pop("note")
    with pytest.raises(MethodValidationError) as error:
        save_reviewed_method(
            _context(liquid_class),
            payload,
            expected_registry_digest=method_registry(_context(liquid_class))["digest"],
        )
    assert "evidence.local_review" in error.value.missing_fields
