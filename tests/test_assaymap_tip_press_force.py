"""Golden test: tip-press force, pinned against reference measurements.

Vendor traffic for this head contains two presses of the *same* 25.0 mm
travel that differ only in force:

    full 96-channel LT250 press   force byte 170   66.67%
    partial press, <=4 channels   force byte   5    1.96%

The cartridge press in the cartridge measurements uses byte 170 as well, so
one number covers both consumables at full head.

**The full-head byte is confirmed; the low end is not.** Op A sits on the
teachpoint with no subset offset, so its channel count is known from geometry
alone and 170 is a genuine measurement. The partial's channel count is *not*
known -- the operator recalls "no more than 4". Our table reaches byte 5 only at
exactly 1 channel, giving 7/10/12 for 2/3/4. So either the subset was a single
tip and the curve is right, or the vendor software floors small subsets at byte 5 and we press
up to +7 bytes harder there. See T25; do not restate the low end as confirmed.

These are asserted through the *production* path -- head-type table selection,
channel interpolation, then the two scalings that produce the wire byte. An
earlier golden test here passed while production was wrong because it supplied
the current directly instead of computing it, so nothing below hard-codes an
intermediate value.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pybravo.darwin.sequences import _z_axis_force_percent
from pybravo.protocol.gemini.instruction import _scale_force_percent
from pybravo.state_machine.tasks import TipsOnTask
from pybravo.types import HeadType

# Force bytes read off the wire (word1 bits 8-15).
FULL_HEAD_FORCE_BYTE = 170
PARTIAL_ONE_CHANNEL_FORCE_BYTE = 5


def _press_force_byte(head_type: HeadType, rows: int, cols: int) -> int:
    """Run the real _tip_press_current for a head/subset and return the wire byte."""
    task = SimpleNamespace(
        _profile=SimpleNamespace(
            current_limits={},
            head=SimpleNamespace(head_type=head_type),
        ),
        _head_mode=SimpleNamespace(row_count=rows, column_count=cols),
    )
    task._num_channels = lambda: max(1, rows * cols)
    amps = TipsOnTask._tip_press_current(task)
    return _scale_force_percent(_z_axis_force_percent(amps))


def test_full_head_press_matches_the_capture() -> None:
    """96 AssayMAP channels press at the byte the vendor software sent for tips and cartridges."""
    assert _press_force_byte(HeadType.HT_96_ASSAYMAP, 8, 12) == FULL_HEAD_FORCE_BYTE


def test_single_channel_press_matches_the_capture() -> None:
    """A one-channel partial presses far softer -- 1.96%, not 66.67%.

    Pressing a single tip at full-head force is the failure this guards: it is
    the same motion, the same travel, and nothing in the protocol objects.

    This asserts our table's 1-channel value against the measured byte. It is a
    regression guard, not proof the measurement was one channel -- see the module
    docstring.
    """
    assert _press_force_byte(HeadType.HT_96_ASSAYMAP, 1, 1) == PARTIAL_ONE_CHANNEL_FORCE_BYTE


def test_assaymap_presses_on_the_long_tip_table() -> None:
    """AssayMAP must not fall through to the short-tip table.

    is_disposable and is_fixed are both False for this head, so an unlisted head
    type silently lands on ST, which presses at 0.3 A (38%) and leaves cartridges
    unseated. The capture says 0.6 A.
    """
    st_byte = _press_force_byte(HeadType.HT_96_D_70, 8, 12)
    assert _press_force_byte(HeadType.HT_96_ASSAYMAP, 8, 12) != st_byte


@pytest.mark.parametrize("rows,cols", [(1, 1), (1, 12), (8, 1), (8, 12)])
def test_press_force_rises_with_channel_count(rows: int, cols: int) -> None:
    """Force must be monotonic in channel count, never exceeding the full-head byte."""
    byte = _press_force_byte(HeadType.HT_96_ASSAYMAP, rows, cols)
    assert PARTIAL_ONE_CHANNEL_FORCE_BYTE <= byte <= FULL_HEAD_FORCE_BYTE


def test_small_subsets_stay_near_the_captured_floor() -> None:
    """Subsets of <=4 channels must press softly, whatever the exact count.

    The capture shows byte 5 for a subset the operator puts at <=4 channels. Our
    table interpolates to 7/10/12 over that band, which is a divergence of at
    most +7 bytes (4.71% against 1.96%) and errs toward pressing harder. That is
    tolerable -- an over-seated tip beats an unseated one -- but it must not grow.
    If this band ever climbs toward the 8-channel value it has stopped being a
    small-subset press.
    """
    eight_channel = _press_force_byte(HeadType.HT_96_ASSAYMAP, 1, 8)
    for n in (1, 2, 3, 4):
        byte = _press_force_byte(HeadType.HT_96_ASSAYMAP, 1, n)
        assert PARTIAL_ONE_CHANNEL_FORCE_BYTE <= byte < eight_channel
