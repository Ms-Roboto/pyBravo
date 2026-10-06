"""Cross-profile liquid classes are reviewable evidence, never run-ready methods."""

from __future__ import annotations

from copy import deepcopy

import httpx
import pytest
import yaml

from pybravo.web import server
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols.liquid_class_proposals import (
    LiquidClassProposalQuery,
    propose_liquid_classes,
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


def _class(class_id: str = "physical-st10") -> dict:
    return {
        "liquid_class_id": class_id,
        "name": "Locally configured ST10 class",
        "machine_id": "physical-bravo",
        "head_type": "HT_384_D_70",
        "tip_id": "st_10ul",
        "tip_capacity_ul": 10.0,
        "aspirate": deepcopy(MOTION),
        "dispense": deepcopy(MOTION),
        "equation": {"control_points": [
            {"desired_ul": 0.0, "commanded_ul": 0.0},
            {"desired_ul": 10.0, "commanded_ul": 10.0},
        ]},
    }


def _context(**updates) -> dict:
    context = {
        "controller_type": "simulation",
        "machine_id": "SIMULATED",
        "head_type": "HT_384_D_70",
        "tip_definitions": [{
            "tip_id": "st_10ul", "capacity_ul": 10.0,
            "compatible_heads": ["HT_384_D_70"],
        }],
        "liquid_classes": [],
    }
    context.update(updates)
    return context


def _query(**updates) -> LiquidClassProposalQuery:
    payload = {
        "tip_id": "st_10ul", "volume_ul": 5.0, "reagent_family": "DMSO",
        "source_labware_id": "labcyte-pp", "destination_labware_id": "labcyte-ldv",
    }
    payload.update(updates)
    return LiquidClassProposalQuery.model_validate(payload)


def _write_classes(monkeypatch, tmp_path, rows):
    path = tmp_path / "liquid_classes.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "liquid_classes": rows}), encoding="utf-8")
    monkeypatch.setenv("PYBRAVO_LIQUID_CLASS_STORE_PATH", str(path))
    methods = tmp_path / "protocol_methods.yaml"
    methods.write_text("version: 1\nmethods: []\n", encoding="utf-8")
    monkeypatch.setenv("PYBRAVO_METHOD_STORE_PATH", str(methods))
    return path


def test_cross_profile_proposal_is_explicitly_unverified_and_pinned_to_raw_source(monkeypatch, tmp_path):
    valid = _class()
    inferred_tip = _class("inferred-tip")
    inferred_tip["tip_id"] = ""
    missing_motion = _class("missing-motion")
    del missing_motion["dispense"]["z_out_velocity_mm_s"]
    wrong_head = _class("wrong-head")
    wrong_head["head_type"] = "HT_96_D_70"
    source = _write_classes(monkeypatch, tmp_path, [valid, inferred_tip, missing_motion, wrong_head])

    result = propose_liquid_classes(_context(), _query())
    assert [row["liquid_class_id"] for row in result["candidates"]] == ["physical-st10"]
    candidate = result["candidates"][0]
    assert result["planning_liquid_class_candidates"] == result["candidates"]
    assert candidate["machine_id"] == "physical-bravo"
    assert candidate["tip_id"] == "st_10ul"
    assert candidate["status"] == "imported_unverified"
    assert candidate["execution_ready"] is False
    assert candidate["can_apply_to_plan"] is False
    assert "method_ref" not in candidate
    assert candidate["aspirate"]["w_velocity_ul_s"] == 1.0
    assert candidate["dispense"]["post_delay_ms"] == 0
    assert candidate["provenance"]["source_digest"]
    assert candidate["source_field_origins"]["aspirate.w_velocity_ul_s"] == "imported_config"
    assert any("DMSO" in note and "unverified" in note for note in candidate["caveats"])
    assert "local_method_review" in candidate["missing_fields"]
    assert source.read_text(encoding="utf-8")  # discovery never mutates the store


def test_no_cross_profile_proposal_on_physical_controller_or_without_exact_tip(monkeypatch, tmp_path):
    _write_classes(monkeypatch, tmp_path, [_class()])
    assert propose_liquid_classes(_context(controller_type="agile"), _query())["candidates"] == []
    assert propose_liquid_classes(_context(), _query(tip_id="st_70ul"))["candidates"] == []
    assert propose_liquid_classes(_context(), _query(volume_ul=11.0))["candidates"] == []
    assert propose_liquid_classes(_context(), _query(volume_ul=10.5))["candidates"] == []


def test_related_publication_is_context_only_not_numeric_class_provenance(monkeypatch, tmp_path):
    _write_classes(monkeypatch, tmp_path, [_class()])
    method_path = tmp_path / "protocol_methods.yaml"
    method_path.write_text(yaml.safe_dump({
        "version": 1,
        "methods": [{
            "method_id": "reference:dmso-st10",
            "version": "1.0.0",
            "status": "reference_candidate",
            "title": "Vendor DMSO ST10 reference",
            "applicability": {
                "head_types": ["HT_384_D_70"],
                "tip_ids": ["st_10ul"],
                "reagent_families": ["DMSO"],
            },
            "evidence": [{
                "source_type": "publication",
                "source_url": "https://example.test/dmso-study",
                "note": "Reservoir to polystyrene, not PP to LDV.",
            }],
        }],
    }), encoding="utf-8")
    result = propose_liquid_classes(_context(), _query())
    assert len(result["references"]) == 1
    assert result["references"][0]["qualifies_numeric_settings"] is False
    assert result["references"][0]["relation"] == "context_only"
    assert result["candidates"][0]["provenance"]["source_type"] == "local_config"
    assert "source_url" not in result["candidates"][0]["provenance"]
    assert propose_liquid_classes(_context(), _query(reagent_family="aqueous"))["references"] == []


@pytest.mark.asyncio
async def test_liquid_class_proposal_endpoint_is_read_only_and_typed(monkeypatch, tmp_path):
    source = _write_classes(monkeypatch, tmp_path, [_class()])
    before = source.read_bytes()
    monkeypatch.setattr(api, "machine_context", lambda _bravo: _context())
    monkeypatch.setattr(api, "_bravo", lambda: object())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        result = await client.post("/api/protocols/liquid-class-proposals", json=_query().model_dump())
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["candidates"][0]["liquid_class_id"] == "physical-st10"
        assert body["candidates"][0]["execution_ready"] is False
        invalid = await client.post("/api/protocols/liquid-class-proposals", json={
            **_query().model_dump(), "volume_ul": -1,
        })
        assert invalid.status_code == 422
    assert source.read_bytes() == before
