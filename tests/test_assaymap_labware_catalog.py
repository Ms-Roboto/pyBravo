"""The AssayMAP fixtures must be in the catalog, with the heights the geometry uses.

Every AssayMAP Z is referenced to the top of the labware -- seated, shuck and press
positions all derive from `teachpoint.z - labware.height` (see notes sections 4b
and 5b). So `height_mm` here is not descriptive metadata, it is an input to head
motion, and it must equal the vendor file's THICKNESS exactly.

Imported from the vendor .xml.roiZip files by notes/vendor_labware_to_catalog.py.
Unlike the profile, these are Agilent's standard definitions and are not
machine-specific.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

OVERLAY = Path(__file__).resolve().parents[1] / "config" / "labware_catalog.d" / "96am.yaml"
TIP_OFFSETS = Path(__file__).resolve().parents[1] / "config" / "tip_offsets.yaml"

# Labware a tip-offset row names but which pyBravo has no definition for. Such a
# row is staged and correct but cannot fire until the vendor file is imported.
# Empty now that both LT250 boxes have been imported -- keep the mechanism, it
# caught a real gap once.
PENDING_IMPORT: set[str] = set()

# name -> (THICKNESS from the vendor file, base_class)
VENDOR_FIXTURES = {
    "96 V11 LT250 Tip Box 19477.002": (60.53, "tip_box"),
    "96 V11 LT250 Tip Box Standard": (60.00, "tip_box"),
    "96AM 250uL Tip Loading Station": (60.53, "tip_box"),
    "96AM Cartridge Rack and Receiver Plate": (42.60, "AssayMap Cartridge Rack"),
    "96AM Cartridge Seating Station": (64.00, "AssayMap Cartridge Rack"),
    "96AM Receiver Plate": (27.75, "microplate"),
    "96AM Tip Wash Station": (49.50, "Tip Wash Station"),
}


@pytest.fixture(scope="module")
def catalog() -> dict[str, dict]:
    overlay = yaml.safe_load(OVERLAY.read_text())
    return {row["name"]: row for row in overlay["labware"]}


@pytest.mark.parametrize("name,expected", sorted(VENDOR_FIXTURES.items()))
def test_fixture_height_matches_vendor_thickness(
    catalog: dict[str, dict], name: str, expected: tuple[float, str]
) -> None:
    """height_mm feeds head motion, so it must be the vendor THICKNESS exactly."""
    thickness, _ = expected
    assert name in catalog, f"{name} missing from the labware catalog"
    assert catalog[name]["height_mm"] == pytest.approx(thickness, abs=1e-6)


@pytest.mark.parametrize("name,expected", sorted(VENDOR_FIXTURES.items()))
def test_fixture_base_class(catalog: dict[str, dict], name: str, expected: tuple[float, str]) -> None:
    """base_class must be the vendor's own class name, verbatim.

    the vendor software names these after the fixture rather than after its behaviour, and the
    catalog stores what the vendor calls them. Behaviour is bridged separately, in
    bravo.py's _VENDOR_BASE_CLASS_ALIASES.
    """
    _, base_class = expected
    assert catalog[name]["base_class"] == base_class


@pytest.mark.parametrize("name", sorted(VENDOR_FIXTURES))
def test_fixture_is_a_96_well_9mm_grid(catalog: dict[str, dict], name: str) -> None:
    """96 wells at 9.0 mm, which is what partial-head addressing steps by."""
    entry = catalog[name]
    assert entry["wells"] == 96
    assert entry["spacing_x_mm"] == pytest.approx(9.0)
    assert entry["spacing_y_mm"] == pytest.approx(9.0)


def test_tip_offset_rows_reference_real_labware(catalog: dict[str, dict]) -> None:
    """Every live AssayMAP tip-offset row must name a labware that exists.

    This is the quiet failure mode: a row whose `tipbox` does not match any
    labware simply never fires, and the head silently reverts to the profile
    default of 15.0 mm -- which is wrong for both consumables. A rename would
    otherwise break the press and shuck Z with no error anywhere.

    PENDING_IMPORT is listed explicitly rather than skipped, so that importing one
    of those labware definitions makes this test tell you to move it, instead of
    the row quietly starting or failing to fire.
    """
    rows = yaml.safe_load(TIP_OFFSETS.read_text())["offsets"]
    assaymap = [r for r in rows if r.get("head_type") == "HT_96_ASSAYMAP"]
    assert assaymap, "no AssayMAP tip-offset rows found"
    known = {n.strip().lower() for n in catalog}
    pending = {n.strip().lower() for n in PENDING_IMPORT}

    for row in assaymap:
        tipbox = str(row["tipbox"]).strip().lower()
        assert tipbox in known or tipbox in pending, (
            f"tip_offsets.yaml row names {row['tipbox']!r}, which is neither in the "
            f"labware catalog nor listed as pending import -- this row would never fire"
        )

    landed = pending & known
    assert not landed, (
        f"{sorted(landed)} is now in the catalog -- remove it from PENDING_IMPORT; "
        f"its tip-offset row is live from here on"
    )


def test_the_two_lt250_boxes_are_distinct_fixtures() -> None:
    """They differ by 0.53 mm, and that difference must survive into the catalog.

    Treating "the LT250 tip box" as one thing would put every press and shuck on
    the 19477.002 deck 0.53 mm out when the Standard box is loaded. Because the
    offsets are anchored to the labware top, the two share tip-offset rows and the
    fixture difference is carried entirely by height_mm -- which only works if
    these stay distinct entries.
    """
    overlay = yaml.safe_load(OVERLAY.read_text())
    by_name = {row["name"]: row for row in overlay["labware"]}
    a = by_name["96 V11 LT250 Tip Box 19477.002"]["height_mm"]
    b = by_name["96 V11 LT250 Tip Box Standard"]["height_mm"]
    assert a != b
    assert a - b == pytest.approx(0.53, abs=1e-6)


def test_cartridge_rack_behaves_as_a_tip_box() -> None:
    """The vendor class name must still reach pybravo's tip-box paths.

    Those paths are not only guards. Tips Off tracking and the tip-box occupancy
    check are keyed on the same string, so a cartridge rack that does not
    normalize would silently stop tracking what is on the head.
    """
    from pybravo.bravo import _VENDOR_BASE_CLASS_ALIASES

    assert _VENDOR_BASE_CLASS_ALIASES["assaymap cartridge rack"] == "tip_box"


def test_wash_station_is_not_aliased_to_tip_box() -> None:
    """The wash station must stay plate-style so its wells remain selectable.

    _plate_selection returns None for a tip box, so aliasing the wash station
    would make its wells unselectable -- and aspirating from them is the entire
    point of the wash task.
    """
    from pybravo.bravo import _VENDOR_BASE_CLASS_ALIASES

    assert "tip wash station" not in _VENDOR_BASE_CLASS_ALIASES


def test_base_class_normalization_is_case_insensitive() -> None:
    """The catalog stores mixed case; every lookup lowercases first."""
    from pybravo.bravo import _VENDOR_BASE_CLASS_ALIASES, Bravo
    from pybravo.deck.labware import Labware

    for stored in ("AssayMap Cartridge Rack", "assaymap cartridge rack", "  ASSAYMAP CARTRIDGE RACK  "):
        lw = Labware(id="lw-test", name="x", height=1.0, width=1.0, length=1.0,
                     metadata={"base_class": stored})
        assert Bravo._labware_base_class(lw) == "tip_box"
    assert all(k == k.strip().lower() for k in _VENDOR_BASE_CLASS_ALIASES)


def test_fixtures_survive_the_real_catalog_builder() -> None:
    """The rows must load through build_labware_catalog(), not merely parse as YAML.

    This is the test that was missing. LabwareDefinition takes a fixed set of
    fields and **silently skips the whole row** on an unexpected one, logging a
    warning and carrying on. Three extra provenance keys in the generated snapshot
    therefore dropped all seven AssayMAP fixtures from the runtime catalog while
    every YAML-level assertion here still passed -- the labware simply did not
    exist as far as the instrument was concerned.

    Reading the file proves the file. Only the builder proves the catalog.
    """
    from pybravo.deck.labware import build_labware_catalog

    catalog = build_labware_catalog()
    loaded = {d.name: d for d in catalog.list_definitions()}
    missing = sorted(set(VENDOR_FIXTURES) - set(loaded))
    assert not missing, f"dropped by the catalog builder: {missing}"

    for name, (thickness, base_class) in VENDOR_FIXTURES.items():
        assert loaded[name].height_mm == pytest.approx(thickness, abs=1e-6), name
        assert loaded[name].base_class == base_class, name


def test_every_snapshot_row_is_loadable() -> None:
    """No row in the snapshot may be silently skipped -- ours or anyone's."""
    from pybravo.deck.labware import build_labware_catalog

    overlay = yaml.safe_load(OVERLAY.read_text())
    loaded = {d.name for d in build_labware_catalog().list_definitions()}
    missing = sorted({row["name"] for row in overlay["labware"]} - loaded)
    assert not missing, f"overlay rows dropped by the catalog builder: {missing}"


def test_the_overlay_does_not_replace_anything_in_the_snapshot() -> None:
    """Shipping labware with a feature must not clobber a site's own catalog.

    That is the whole reason these live in config/labware_catalog.d/ rather than
    in the snapshot: with Mongo configured the snapshot is overwritten on every
    successful load, and committing a full snapshot would replace whatever
    catalog a site already has. An overlay that shadowed an existing id would
    reintroduce exactly that problem, quietly.
    """
    snapshot_path = Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml"
    snapshot_ids = {row["id"] for row in yaml.safe_load(snapshot_path.read_text())["labware"]}
    overlay_ids = {row["id"] for row in yaml.safe_load(OVERLAY.read_text())["labware"]}
    assert not (snapshot_ids & overlay_ids), "overlay shadows a snapshot entry"


def test_the_merged_catalog_holds_both_sources() -> None:
    """The instrument must see the snapshot and the overlay, not one or the other."""
    from pybravo.deck.labware import build_labware_catalog

    snapshot_path = Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml"
    snapshot = yaml.safe_load(snapshot_path.read_text())["labware"]
    overlay = yaml.safe_load(OVERLAY.read_text())["labware"]
    loaded = {d.name for d in build_labware_catalog().list_definitions()}

    for row in snapshot:
        assert row["name"] in loaded, f"snapshot entry lost: {row['name']}"
    for row in overlay:
        assert row["name"] in loaded, f"overlay entry lost: {row['name']}"


def test_both_cartridge_fixtures_resolve_to_the_cartridge_tip():
    """Tips On decides whether to zero W from the mounted consumable. Both
    cartridge fixtures must therefore resolve to the cartridge definition, not
    to the LT250 teach tip that capacity-based inference would pick."""
    import yaml

    rows = {row["name"]: row for row in yaml.safe_load(OVERLAY.read_text())["labware"]}
    for name in ("96AM Cartridge Rack and Receiver Plate", "96AM Cartridge Seating Station"):
        row = rows[name]
        assert row["tip_definition_id"] == "am_cartridge_60ul", name
        assert row["disposable_tip_capacity_ul"] == 60.0, name
