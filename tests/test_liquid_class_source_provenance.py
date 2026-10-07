"""Imported VWorks evidence survives normal store reads and ordinary edits."""

from __future__ import annotations

import yaml

from pybravo import liquid_classes


def test_source_archive_survives_load_and_patch(monkeypatch, tmp_path):
    path = tmp_path / "liquid_classes.yaml"
    evidence = {
        "filename": "384 disposable tip 0.5 - 10ul.xml.roiZip",
        "sha256": "a" * 64,
        "roi_state": "IN_DEVELOPMENT",
        "entry_version": "2",
        "raw_coefficients": [0.0, 1.0],
    }
    origins = {"aspirate.w_velocity_ul_s": "imported_config", "tip_id": "reviewed"}
    path.write_text(yaml.safe_dump({
        "version": 1,
        "liquid_classes": [{
            "liquid_class_id": "vworks-class",
            "name": "384 disposable tip 0.5 - 10ul",
            "machine_id": "physical-bravo",
            "head_type": "HT_384_D_70",
            "tip_id": "st_10ul",
            "tip_capacity_ul": 10.0,
            "source_archive": evidence,
            "field_origins": origins,
            "aspirate": {"w_velocity_ul_s": 1.0},
            "dispense": {"w_velocity_ul_s": 1.0},
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("PYBRAVO_LIQUID_CLASS_STORE_PATH", str(path))

    loaded = liquid_classes.list_liquid_classes(machine_id="physical-bravo")
    assert loaded[0]["source_archive"] == evidence
    assert loaded[0]["field_origins"] == origins

    liquid_classes.patch_liquid_class("vworks-class", {"description": "reviewed example"})
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["liquid_classes"][0]
    assert saved["source_archive"] == evidence
    assert saved["field_origins"] == origins
