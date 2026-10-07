"""Focused ROI archive parsing and reconciliation tests."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from scripts.import_bravo_liquid_class_archives import (
    MOTION_FIELDS,
    load_archives,
    main,
    parse_archive,
    reconcile,
)


def _archive(path: Path, *, name: str, coefficients: list[float] | None = None) -> Path:
    coefficients = coefficients or [0.1026, 1.0567]
    root = ET.Element("Velocity11", {"file": "Liquid Entry", "extrainfo": name, "version": "1.0"})
    values = ET.SubElement(root, "values")
    ET.SubElement(values, "value", {"name": "Note", "value": ""})
    for dotted, vendor_key in MOTION_FIELDS.items():
        value = "0" if "delay" in dotted else "5"
        ET.SubElement(values, "value", {"name": vendor_key, "value": value})
    ET.SubElement(values, "value", {"name": "VERSION", "value": "2"})
    subkey = ET.SubElement(ET.SubElement(root, "subkeys"), "subkey", {"name": "Coefficients"})
    coefficient_values = ET.SubElement(subkey, "values")
    ET.SubElement(coefficient_values, "value", {"name": "Number of Coefficients", "value": str(len(coefficients))})
    for i, coefficient in enumerate(coefficients):
        ET.SubElement(coefficient_values, "value", {"name": str(i), "value": str(coefficient)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as z:
        member = path.name.removesuffix(".roiZip")
        z.writestr(member, ET.tostring(root, encoding="utf-8") + b"\x00")
        # The actual supplied Agilent archives use this mismatched declaration.
        z.writestr(member + ".otherMetadata", b'<?xml version="1.0" encoding="utf-16"?><ROIMetadata><ROIState>IN_DEVELOPMENT</ROIState></ROIMetadata>')
        z.writestr(member + ".auditTrail", b"")
        z.writestr(member + ".md5", b"0" * 32)
    return path


def _store(existing: dict | None = None) -> dict:
    return {
        "version": 1,
        "liquid_classes": [existing] if existing else [],
        "pipette_techniques": [{"name": "keep this technique"}],
    }


def test_parses_vendor_fields_polynomial_and_source_evidence(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "class.xml.roiZip", name="384 disposable tip 0.5 - 10ul",
        coefficients=[0.4512, 0.8427, 0.0277, -0.0011],
    )
    entry = parse_archive(archive)

    assert entry["name"] == "384 disposable tip 0.5 - 10ul"
    assert entry["motion"]["aspirate"]["w_velocity_ul_s"] == 5
    assert entry["motion"]["aspirate"]["post_delay_ms"] == 0
    assert entry["coefficients"] == [0.4512, 0.8427, 0.0277, -0.0011]
    assert entry["source_archive"]["raw_coefficients"] == entry["coefficients"]
    assert entry["source_archive"]["roi_state"] == "IN_DEVELOPMENT"
    assert entry["source_archive"]["entry_version"] == "2"
    assert entry["source_archive"]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_exact_match_preserves_values_and_id_and_reports_differences(tmp_path: Path) -> None:
    _archive(tmp_path / "class.xml.roiZip", name="existing class")
    existing = {
        "liquid_class_id": "liq_preserve",
        "name": "existing class",
        "machine_id": "MACHINE-1",
        "head_type": "HT_384_D_70",
        "tip_id": "",
        "tip_capacity_ul": 10.0,
        "aspirate": {"w_velocity_ul_s": 99.0},
        "dispense": {},
        "equation": {"control_points": [{"desired_ul": 0, "commanded_ul": 0}]},
        "reviewer_note": "retain the hand-tuned class",
    }
    original = _store(existing)
    updated, report = reconcile(original, load_archives(tmp_path), machine_id="MACHINE-1")

    row = updated["liquid_classes"][0]
    assert row["liquid_class_id"] == "liq_preserve"
    assert row["aspirate"] == existing["aspirate"]
    assert row["equation"] == existing["equation"]
    assert row["reviewer_note"] == existing["reviewer_note"]
    assert row["tip_id"] == ""
    assert "source_archive" not in row
    assert "field_origins" not in row
    assert report["archives"][0]["status"] == "matched_values_differ"
    assert any(d["field"] == "aspirate.w_velocity_ul_s" for d in report["archives"][0]["differences"])
    assert original == _store(existing)  # pure reconciliation


def test_exact_values_gain_archive_provenance_without_changing_settings(tmp_path: Path) -> None:
    _archive(tmp_path / "class.xml.roiZip", name="matching class")
    motion = {"w_velocity_ul_s": 5.0, "w_acceleration_ul_s2": 5.0, "post_delay_ms": 0,
              "z_in_velocity_mm_s": 5.0, "z_in_acceleration_mm_s2": 5.0,
              "z_out_velocity_mm_s": 5.0, "z_out_acceleration_mm_s2": 5.0}
    existing = {
        "liquid_class_id": "liq_matching", "name": "matching class", "machine_id": "MACHINE-1",
        "head_type": "HT_384_D_70", "tip_id": "", "tip_capacity_ul": 10.0,
        "aspirate": motion.copy(), "dispense": motion.copy(),
        "equation": {"control_points": [
            {"desired_ul": 0.0, "commanded_ul": 0.1026},
            {"desired_ul": 10.0, "commanded_ul": 10.6696},
        ]},
    }
    updated, report = reconcile(_store(existing), load_archives(tmp_path), machine_id="MACHINE-1")
    row = updated["liquid_classes"][0]
    for key in ("liquid_class_id", "name", "machine_id", "head_type", "tip_id", "tip_capacity_ul", "aspirate", "dispense", "equation"):
        assert row[key] == existing[key]
    assert row["source_archive"]["filename"] == "class.xml.roiZip"
    assert row["field_origins"]["aspirate.w_velocity_ul_s"] == "imported_config"
    assert row["field_origins"]["equation.control_points"] == "imported_config"
    assert report["archives"][0]["status"] == "matched_preserved"


def test_unmapped_archive_stays_out_of_active_catalog(tmp_path: Path) -> None:
    _archive(tmp_path / "class.xml.roiZip", name="unknown AM class")
    updated, index = reconcile(_store(), load_archives(tmp_path), machine_id="MACHINE-1")

    assert updated["liquid_classes"] == []
    assert index["archives"][0]["status"] == "unmapped"
    assert index["archives"][0]["source_archive"]["raw_coefficients"] == [0.1026, 1.0567]


def test_explicit_mapping_requires_compatible_head_and_tip(tmp_path: Path) -> None:
    _archive(tmp_path / "class.xml.roiZip", name="new class")
    entries = load_archives(tmp_path)
    with pytest.raises(ValueError, match="Unsupported head/tip pair"):
        reconcile(_store(), entries, machine_id="MACHINE-1", mappings={
            "new class": {"head_type": "HT_384_D_70", "tip_id": "lt_250ul", "tip_capacity_ul": 250},
        })
    updated, index = reconcile(_store(), entries, machine_id="MACHINE-1", mappings={
        "new class": {"head_type": "HT_384_D_70", "tip_id": "st_10ul", "tip_capacity_ul": 10},
    })
    row = updated["liquid_classes"][0]
    assert row["tip_id"] == "st_10ul"
    assert row["source_archive"]["roi_state"] == "IN_DEVELOPMENT"
    assert row["field_origins"]["aspirate.w_velocity_ul_s"] == "imported_config"
    assert index["archives"][0]["status"] == "imported_explicit_mapping"
    assert "reagent_family" not in row


def test_cli_apply_and_index_are_idempotent(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    _archive(archives / "existing.xml.roiZip", name="existing class")
    _archive(archives / "unmapped.xml.roiZip", name="unmapped class")
    catalog = tmp_path / "catalog.yaml"
    index = tmp_path / "liquid_class_archives.yaml"
    catalog.write_text(yaml.safe_dump(_store({
        "liquid_class_id": "liq_existing", "name": "existing class", "machine_id": "MACHINE-1",
        "head_type": "HT_384_D_70", "tip_id": "", "tip_capacity_ul": 10.0,
        "aspirate": {}, "dispense": {}, "equation": {"control_points": []},
    })), encoding="utf-8")
    args = [str(archives), "--machine-id", "MACHINE-1", "--catalog", str(catalog), "--index", str(index), "--apply"]
    assert main(args) == 0
    first_catalog = catalog.read_bytes()
    first_index = index.read_bytes()
    assert main(args) == 0
    assert catalog.read_bytes() == first_catalog
    assert index.read_bytes() == first_index
    rows = yaml.safe_load(catalog.read_text())["liquid_classes"]
    report = yaml.safe_load(index.read_text())
    assert len(rows) == 1 and rows[0]["liquid_class_id"] == "liq_existing"
    assert report["summary"]["archive_count"] == 2
    assert {a["status"] for a in report["archives"]} == {"matched_values_differ", "unmapped"}
