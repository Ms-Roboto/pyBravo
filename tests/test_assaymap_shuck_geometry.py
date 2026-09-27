"""Golden test: AssayMAP press and shuck Z, reconstructed from reference measurements.

Every Z on this head is referenced to the *top of the labware*, not to the deck.
Anchoring there collapses three measured operations onto one constant:

    seated Z = labware_top + 60.50      every consumable, every fixture
    seated   = approach + 25.00         the press travel, pinned separately
    shuck Z  = seated - 30.00           LT250 tips
             = seated - 23.00           cartridges

Measured (the LT250 measurements for the tips, the cartridge measurements for
the cartridges; deck layout confirmed by the operator):

    labware                  teach_z  THICKNESS  approach   seated    shuck
    LT250 tip box  (pos 3)   105.300      60.53    80.270  105.270   75.270
    LT250 tip station(pos 5) 105.257      60.53    80.234  105.234   75.234
    cartridge rack (pos 6)   105.157      42.60    98.057  123.057  100.057

pyBravo already computes tips_on_position the same labware-referenced way, so the
capture reduces to the two offsets in config/tip_offsets.yaml. This test runs the
real resolver and the real formula and reconstructs the measured Z -- it does not
assert the offsets against themselves.

The profile default these replace is 15.0, which is wrong for both consumables.
"""
from __future__ import annotations

import pytest

from pybravo.tip_offsets import get_tip_offset_table
from pybravo.types import HeadType

TEACH_TIP_LENGTH_MM = 55.5   # profile head.teach_tip_length_mm for 96AM
PROFILE_DEFAULT_Z_OFFSET = 15.0
TOLERANCE_MM = 0.01

# labware name, teach_z, measured approach / seated / shuck.
# THICKNESS is deliberately NOT listed here -- it is read from the labware catalog,
# so a wrong height_mm there fails these tests against the measured Z rather than
# passing quietly. That link is the whole point: catalog -> geometry -> capture.
CAPTURED = [
    ("96AM 250uL Tip Loading Station", 105.257, 80.234, 105.234, 75.234),
    ("96 V11 LT250 Tip Box 19477.002", 105.300, 80.270, 105.270, 75.270),
    ("96AM Cartridge Rack and Receiver Plate", 105.157, 98.057, 123.057, 100.057),
]

def _catalog_height(name: str) -> float:
    """The labware's THICKNESS, as production reads it.

    Through build_labware_catalog() rather than any one file: these definitions
    ship in config/labware_catalog.d/, and only the builder merges that overlay
    with the snapshot the way the instrument sees it.
    """
    from pybravo.deck.labware import build_labware_catalog

    for definition in build_labware_catalog().list_definitions():
        if definition.name == name:
            return float(definition.height_mm)
    raise AssertionError(f"{name} is not in the labware catalog")


def _tips_on_position(teach_z: float, thickness: float) -> float:
    """Mirror of TipsOnTask._tips_on_position: deck_surface_z - labware.height."""
    return (teach_z + TEACH_TIP_LENGTH_MM) - thickness


def _resolve(tipbox: str):
    return get_tip_offset_table(reload=True).resolve(
        HeadType.HT_96_ASSAYMAP,
        tipbox_name=tipbox,
        default_z_offset=PROFILE_DEFAULT_Z_OFFSET,
        default_w_position=0.0,
    )


@pytest.mark.parametrize("tipbox,teach_z,approach,seated,shuck", CAPTURED)
def test_press_target_matches_the_capture(
    tipbox: str, teach_z: float, approach: float, seated: float, shuck: float,
) -> None:
    """tips_on_position + tips_on_z_offset must land on the seated Z the vendor software used."""
    thickness = _catalog_height(tipbox)
    offsets = _resolve(tipbox)
    assert offsets.matched, f"no tip-offset row matched {tipbox!r}"
    computed = _tips_on_position(teach_z, thickness) + offsets.tips_on_z_offset
    assert computed == pytest.approx(seated, abs=TOLERANCE_MM)


@pytest.mark.parametrize("tipbox,teach_z,approach,seated,shuck", CAPTURED)
def test_shuck_z_matches_the_capture(
    tipbox: str, teach_z: float, approach: float, seated: float, shuck: float,
) -> None:
    """tips_on_position - tips_off_z_offset must land on the shuck Z the vendor software used.

    This is the number that matters most: too shallow and the stripper plate
    never engages, too deep and the gripper drives into the head.
    """
    thickness = _catalog_height(tipbox)
    offsets = _resolve(tipbox)
    computed = _tips_on_position(teach_z, thickness) - offsets.tips_off_z_offset
    assert computed == pytest.approx(shuck, abs=TOLERANCE_MM)


@pytest.mark.parametrize("tipbox,teach_z,approach,seated,shuck", CAPTURED)
def test_seated_position_is_one_labware_referenced_constant(
    tipbox: str, teach_z: float, approach: float, seated: float, shuck: float,
) -> None:
    """Seated sits 60.50 mm below the labware top for every consumable.

    Cartridges and LT250 tips differ in length by 26.3 mm and the fixtures differ
    in thickness by 17.93 mm, yet this one number covers them all. If it ever
    stops holding, the reference plane has been misidentified, not the offset.
    """
    assert seated - (teach_z - _catalog_height(tipbox)) == pytest.approx(60.50, abs=TOLERANCE_MM)


@pytest.mark.parametrize("tipbox,teach_z,approach,seated,shuck", CAPTURED)
def test_press_travel_is_25mm(
    tipbox: str, teach_z: float, approach: float, seated: float, shuck: float,
) -> None:
    """The force press covers 25.0 mm regardless of consumable or fixture."""
    assert seated - approach == pytest.approx(25.0, abs=TOLERANCE_MM)


def test_profile_default_would_be_wrong_for_both_consumables() -> None:
    """Guard the reason these rows exist.

    Without a matching row both consumables fall back to 15.0, which is 10 mm too
    shallow for tips and 3 mm for cartridges. If a rename silently stops a row
    matching, the failure is a quiet reversion to that default.
    """
    tips = _resolve("96AM 250uL Tip Loading Station")
    cartridges = _resolve("96AM Cartridge Rack and Receiver Plate")
    assert tips.tips_off_z_offset != PROFILE_DEFAULT_Z_OFFSET
    assert cartridges.tips_off_z_offset != PROFILE_DEFAULT_Z_OFFSET
    assert tips.tips_off_z_offset != cartridges.tips_off_z_offset


def test_assaymap_rows_never_drive_w() -> None:
    """This head strips with the gripper, so no row may command a W eject.

    tips_off_w_position is a disposable-head quantity. A non-zero value here
    would drive the syringe -- which on this head holds liquid -- during Tips Off.
    """
    for tipbox, *_ in CAPTURED:
        assert _resolve(tipbox).tips_off_w_position == 0.0
