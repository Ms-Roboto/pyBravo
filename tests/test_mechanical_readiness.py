"""Saved native drafts expose actionable mechanics without moving a controller."""

from types import SimpleNamespace

import httpx
import pytest

from pybravo.physics.planning import mechanical_issues
from pybravo.types import HeadType
from pybravo.web import server
from pybravo.workflow.storage import WorkflowStorage
from tests.test_drafter_tip_freshness import _loop_draft


def test_saved_litegraph_loop_reports_spent_pickup_and_empty_liquid_location():
    wf = _loop_draft().model_dump()
    wf['graph']['links'] = [[link.get(key) for key in
                            ('id', 'origin_id', 'origin_slot', 'target_id', 'target_slot', 'link_type')]
                           for link in wf['graph']['links']]
    issues = mechanical_issues(wf, catalog_context={})
    assert any(item['value'] == 'LOOP_FIXED_SPENT_TIP_ANCHOR' for item in issues)
    assert any(item['node_id'] == 4 and item['field'] == 'deck' and item['value'] == 2 for item in issues)


def test_malformed_saved_graph_reports_blocker_instead_of_throwing():
    issues = mechanical_issues({'name': 'Bad native graph', 'deck': {}, 'graph': {
        'nodes': [{'id': 1, 'type': 'invented/RobotMotion'}], 'links': [],
    }}, catalog_context={})
    assert issues[0]['value'] == 'INVALID_NATIVE_GRAPH'


def test_known_head_capacity_and_incompatible_rack_are_reported_before_motion():
    wf = _loop_draft(return_location=3).model_dump()
    wf['deck'] = {'1': [{'labware_id': 'lt-rack'}], '3': [{'labware_id': 'lt-return'}]}
    wf['graph']['nodes'][3]['properties']['volume'] = 100
    issues = mechanical_issues(wf, catalog_context={'head_type': 'HT_384_D_70', 'head_max_volume_ul': 70,
                                                  'tipbox_choices': []})
    assert any(item['node_id'] == 4 and item['field'] == 'volume' for item in issues)
    assert any(item['node_id'] == 3 and item['field'] == 'tip_box' for item in issues)


@pytest.mark.asyncio
async def test_mechanical_endpoint_reads_saved_workflow_without_controller_motion(tmp_path, monkeypatch):
    from pybravo.bravo import Bravo
    from pybravo.workflow.protocols import context

    storage = WorkflowStorage(tmp_path)
    saved = storage.create_generated_draft(_loop_draft().model_dump(), provenance={'model': 'test'})
    before = storage.get_workflow(saved['id'])
    live = Bravo(mode='simulation')
    monkeypatch.setattr(server, '_bravo', live)
    monkeypatch.setattr(server, '_get_workflow_storage', lambda: storage)
    monkeypatch.setattr(context, 'machine_context', lambda bravo: {'context_hash': 'fixture'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.get(f"/api/workflows/{saved['id']}/mechanical-readiness")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['motion_performed'] is False
    assert report['mechanical_plan_valid'] is False
    assert report['qualification_granted'] is False
    assert any(item['value'] == 'LOOP_FIXED_SPENT_TIP_ANCHOR' for item in report['issues'])
    assert not live.is_connected
    assert storage.get_workflow(saved['id']) == before


@pytest.mark.asyncio
async def test_picker_keeps_physical_occupancy_but_excludes_spent_pickup(monkeypatch):
    live = SimpleNamespace(profile=SimpleNamespace(head=SimpleNamespace(head_type=HeadType.HT_96_D_70)))
    monkeypatch.setattr(server, 'get_bravo', lambda: live)
    params = {'subset_type': 'single_barrel', 'tipbox_rows': 8, 'tipbox_cols': 12,
              'occupied_cells': ','.join(f'{r}:{c}' for r in range(8) for c in range(12))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        unknown = await client.get('/api/tipbox/legal_anchors', params=params)
        spent = await client.get('/api/tipbox/legal_anchors', params={**params, 'fresh_cells': ''})
        invalid = await client.get('/api/tipbox/legal_anchors', params={**params, 'fresh_cells': '8:12'})
    assert unknown.status_code == spent.status_code == 200
    assert unknown.json()['legal_anchors']
    assert spent.json()['occupied_cells_count'] == 96
    assert spent.json()['fresh_cells_count'] == 0
    assert spent.json()['legal_anchors'] == []
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_picker_checks_interleaved_quadrant_freshness_and_empty_return(monkeypatch):
    live = SimpleNamespace(profile=SimpleNamespace(head=SimpleNamespace(head_type=HeadType.HT_96_D_70)))
    monkeypatch.setattr(server, 'get_bravo', lambda: live)
    occupied = ','.join(f'{r}:{c}' for r in range(16) for c in range(24))
    fresh = ','.join(f'{r}:{c}' for r in range(0, 16, 2) for c in range(0, 24, 2))
    params = {'tipbox_rows': 16, 'tipbox_cols': 24, 'row_stride': 2, 'col_stride': 2,
              'occupied_cells': occupied, 'fresh_cells': fresh}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        pickup = await client.get('/api/tipbox/legal_anchors', params=params)
        returning = await client.get('/api/tipbox/legal_anchors', params={**params, 'purpose': 'return'})
        bad = await client.get('/api/tipbox/legal_anchors', params={**params, 'occupied_cells': 'garbage'})
    assert pickup.status_code == returning.status_code == 200
    assert [(a['row'], a['col']) for a in pickup.json()['legal_anchors']] == [(0, 0)]
    assert returning.json()['legal_anchors'] == []
    assert bad.status_code == 400
