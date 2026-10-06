"""Reviewed plate residuals can populate drafts without inventing run state."""

from __future__ import annotations

from pybravo.workflow.protocols.dead_volume import seed_reviewed_source_dead_volumes
from pybravo.workflow.protocols.models import ProtocolPlan


def _plan() -> ProtocolPlan:
    return ProtocolPlan.model_validate({
        "name": "Source to destination",
        "materials": [
            {"id": "source", "name": "Source", "labware_id": "plate"},
            {"id": "destination", "name": "Destination", "labware_id": "plate"},
            {"id": "tips", "name": "Tips", "role": "tips", "labware_id": "rack"},
        ],
        "steps": [{"id": "transfer", "kind": "transfer", "source": "source",
                   "destination": "destination", "volume_ul": 5}],
    })


def test_only_aspirated_source_gets_reviewed_catalog_value():
    plan = _plan()
    changes = seed_reviewed_source_dead_volumes(plan, {"labware": [
        {"id": "plate", "dead_volume_ul": 6.5, "dead_volume_status": "reviewed"},
        {"id": "rack", "dead_volume_ul": 2.0, "dead_volume_status": "reviewed"},
    ]})
    assert changes == [{"path": "/materials/0/dead_volume_ul", "value": 6.5,
                        "labware_id": "plate", "source": "reviewed_labware_catalog"}]
    assert plan.materials[0].dead_volume_ul == 6.5
    assert plan.materials[1].dead_volume_ul is None
    assert all(material.initial_volume_ul is None for material in plan.materials)
    assert plan.materials[2].available_tips is None
    assert plan.decisions == []


def test_placeholder_and_existing_values_are_not_replaced():
    plan = _plan()
    assert seed_reviewed_source_dead_volumes(plan, {"labware": [
        {"id": "plate", "dead_volume_ul": 6.5, "dead_volume_status": "placeholder"},
    ]}) == []
    assert plan.materials[0].dead_volume_ul is None
    plan.materials[0].dead_volume_ul = 8
    assert seed_reviewed_source_dead_volumes(plan, {"labware": [
        {"id": "plate", "dead_volume_ul": 6.5, "dead_volume_status": "reviewed"},
    ]}) == []
    assert plan.materials[0].dead_volume_ul == 8


def test_ambiguous_catalog_id_is_not_used():
    plan = _plan()
    assert seed_reviewed_source_dead_volumes(plan, {"labware": [
        {"id": "plate", "dead_volume_ul": 6.5, "dead_volume_status": "reviewed"},
        {"id": "plate", "dead_volume_ul": 4.0, "dead_volume_status": "reviewed"},
    ]}) == []
    assert plan.materials[0].dead_volume_ul is None


def test_nested_mix_vessel_is_an_aspirated_source():
    plan = _plan()
    plan.steps = [plan.steps[0].model_copy(update={
        "id": "repeat", "kind": "repeat", "source": None, "destination": None,
        "steps": [plan.steps[0].model_copy(update={"id": "mix", "kind": "mix",
                   "source": None, "destination": None, "material": "destination"})],
    })]
    changes = seed_reviewed_source_dead_volumes(plan, {"labware": [
        {"id": "plate", "dead_volume_ul": 6.5, "dead_volume_status": "reviewed"},
    ]})
    assert [item["path"] for item in changes] == ["/materials/1/dead_volume_ul"]
