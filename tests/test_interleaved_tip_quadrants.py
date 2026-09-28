"""Explicit 96-channel pickup/return at every second 384-rack row and column."""
from dataclasses import replace

import pytest

from pybravo.head_mode import normalize_head_mode, selected_tip_wells
from pybravo.types import Axis, HeadType
from tests.test_bravo_init import _make_workflow_executor_bravo


def configured_bravo():
    bravo, _ = _make_workflow_executor_bravo()
    bravo.profile.head.head_type = HeadType.HT_96_D_70
    bravo._head_mode = normalize_head_mode(HeadType.HT_96_D_70, "all_barrels", "back_left")
    bravo.set_labware(2, "tipbox-384")
    return bravo


@pytest.mark.parametrize("row,col", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_explicit_selection_matches_96_head_pitch_without_changing_anchor_offset(row, col):
    bravo = configured_bravo()
    try:
        selection = bravo.set_tip_selection(2, row, col, row_stride=2, col_stride=2)
        cells = selected_tip_wells(16, 24, selection)
        assert len(cells) == 96
        assert cells == [(r, c) for r in range(row, 16, 2) for c in range(col, 24, 2)]
        assert selection.to_dict()["row_stride"] == 2
        assert bravo._tip_xy_target(2, bravo.deck.get_stack(2).top, bravo.head_mode, row, col) == pytest.approx((
            bravo.teachpoints.get_teachpoint(2, Axis.X) - 2.25 + col * 4.5,
            bravo.teachpoints.get_teachpoint(2, Axis.Y) - 2.25 + row * 4.5,
        ))
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_four_distinct_native_tips_on_off_cycles_restore_the_same_wells(monkeypatch):
    bravo = configured_bravo()
    captured = []

    async def complete(task):
        # Inspect native motion task targeting, while skipping motor simulation time.
        captured.append((type(task).__name__, task._tip_xy(), task._tip_selection))

    monkeypatch.setattr(bravo._engine, "execute", complete)
    full = {(r, c) for r in range(16) for c in range(24)}
    picked = []
    try:
        for row, col in ((0, 0), (0, 1), (1, 0), (1, 1)):
            selection = bravo.set_tip_selection(2, row, col, row_stride=2, col_stride=2)
            subset = set(selected_tip_wells(16, 24, selection))
            picked.append(subset)
            await bravo.tips_on(2)
            assert bravo._occupied_tip_wells(2) == full - subset
            assert bravo._tips_on_head_selection == selection
            bravo.set_tip_selection(2, row, col, row_stride=2, col_stride=2)
            await bravo.tips_off(2)
            assert bravo._occupied_tip_wells(2) == full
            assert bravo._tips_on_head is False
        assert set.union(*picked) == full
        assert sum(map(len, picked)) == len(full)
        for index in range(0, 8, 2):
            on, off = captured[index:index + 2]
            assert on[0] == "TipsOnTask"
            assert off[0] == "TipsOffTask"
            assert on[1:] == off[1:]
    finally:
        bravo.disconnect()


def test_bad_selection_never_falls_back_to_a_different_quadrant():
    bravo = configured_bravo()
    try:
        selection = bravo.set_tip_selection(2, 0, 1, row_stride=2, col_stride=2)
        bravo._tipbox_occupancy[2].remove((0, 1))
        with pytest.raises(RuntimeError, match="missing tips"):
            bravo._effective_tip_selection(2, bravo.deck.get_stack(2).top, bravo.head_mode)
        assert bravo._tip_selection == selection
        with pytest.raises(RuntimeError, match="occupied"):
            bravo._validated_tip_wells(bravo.deck.get_stack(2).top, bravo.head_mode, selection, purpose="return")
        with pytest.raises(RuntimeError, match="full 96-channel"):
            bravo.set_tip_selection(2, 0, 12, row_stride=2, col_stride=2)
        bravo.set_head_mode("row", "back_left", row_count=1)
        with pytest.raises(RuntimeError, match="full 96-channel"):
            bravo.set_tip_selection(2, 0, 0, row_stride=2, col_stride=2)
    finally:
        bravo.disconnect()


def test_legacy_contiguous_selection_is_unchanged():
    bravo = configured_bravo()
    try:
        selection = bravo.set_tip_selection(2, 0, 0)
        assert "row_stride" not in selection.to_dict()
        assert selected_tip_wells(16, 24, selection) == [(r, c) for r in range(8) for c in range(12)]
        assert selected_tip_wells(16, 24, replace(selection, row=10, row_stride=2, col_stride=2)) == []
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_api_acknowledges_explicit_stride(monkeypatch):
    from pybravo.web import server

    bravo = configured_bravo()
    monkeypatch.setattr(server, "get_bravo", lambda: bravo)
    try:
        result = await server.set_tip_selection(server.TipSelectionRequest(
            location=2, row=1, col=1, row_stride=2, col_stride=2,
        ))
        assert result["tip_selection"] | {} == {
            "location": 2, "row": 1, "col": 1, "row_count": 8, "column_count": 12,
            "mirror_corner": "back_left", "head_anchor": "back_left", "anchor_row": 1,
            "anchor_col": 1, "row_stride": 2, "col_stride": 2,
        }
    finally:
        bravo.disconnect()
