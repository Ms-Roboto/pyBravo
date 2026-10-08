"""Unknown well depth must not turn liquid simulation into a rim-only pass."""

from pathlib import Path

import pytest

from pybravo.physics.well_geometry import missing_liquid_well_geometry

GEOMETRY = {"base_class": "microplate", "rows": 16, "cols": 24,
            "spacing_x_mm": 4.5, "spacing_y_mm": 4.5, "offset_x_mm": 2.25, "offset_y_mm": 2.25,
            "well_depth_mm": 11.4, "well_diameter_mm": 3.5}


@pytest.mark.parametrize("field", ["well_depth_mm", "well_diameter_mm", "spacing_x_mm", "spacing_y_mm"])
@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), True, "unknown"])
def test_missing_or_invalid_dimensions_are_not_inferred(field, value):
    assert field in missing_liquid_well_geometry({**GEOMETRY, field: value})


def test_single_well_reservoir_needs_depth_and_aperture_but_not_unused_pitches():
    reservoir = {"base_class": "reservoir", "rows": 1, "cols": 1,
                 "well_depth_mm": 30, "well_diameter_mm": 60}
    assert missing_liquid_well_geometry(reservoir) == []
    assert missing_liquid_well_geometry({**reservoir, "well_depth_mm": 0}) == ["well_depth_mm"]


@pytest.fixture
def scene_factory():
    pytest.importorskip("superdex.physics")
    from pybravo.bravo import Bravo
    from pybravo.deck.labware import Labware
    from pybravo.physics.scene import BravoCollisionScene
    from pybravo.profile.profile import BravoProfile

    scenes = []

    def create(metadata):
        bravo = Bravo(profile=BravoProfile.load(Path(__file__).resolve().parents[1] / "profiles/simulation.yaml"),
                      mode="simulation")
        bravo._deck.add(2, Labware(id="test-plate", name="Test plate", height=14.4, width=85.48,
                                  length=127.76, wells=384, metadata=metadata))
        scene = BravoCollisionScene(bravo, sample_spacing_mm=1)
        scenes.append(scene)
        return bravo, scene

    yield create
    for scene in scenes:
        scene.close()


@pytest.mark.parametrize("node_type", ["liquid/Aspirate", "liquid/Dispense", "liquid/Mix"])
def test_runtime_rejects_unknown_depth_before_motion_even_when_target_would_hover(scene_factory, node_type):
    from pybravo.physics.errors import PhysicalSimulationError

    bravo, scene = scene_factory({**GEOMETRY, "well_depth_mm": 0})
    with pytest.raises(PhysicalSimulationError, match="well_depth_mm") as error:
        scene.set_context(3, node_type, {"location": 2, "distance_from_bottom": 1})
    assert error.value.details["missing_geometry"] == ["well_depth_mm"]
    assert error.value.details["node_id"] == 3
    assert scene.moves_checked == scene.samples_checked == 0
    assert not bravo.is_connected


def test_runtime_checks_known_above_rim_dispense_without_forcing_tip_entry(scene_factory):
    from pybravo.types import Axis

    bravo, scene = scene_factory(dict(GEOMETRY))
    scene.set_context(4, "liquid/Dispense", {"location": 2, "distance_from_bottom": 14.4})
    bravo._tips_on_head = True
    bravo._attached_tip_length_mm = bravo.profile.head.teach_tip_length_mm
    bravo._tips_on_head_mode = bravo._head_mode
    pose = {axis: 0.0 for axis in Axis}
    pose.update({Axis.X: bravo._teachpoints.get_teachpoint(2, Axis.X) - 2.25,
                 Axis.Y: bravo._teachpoints.get_teachpoint(2, Axis.Y) - 2.25,
                 Axis.Z: bravo._teachpoints.get_teachpoint(2, Axis.Z) - 14.4 - 3,
                 Axis.Zg: -20})
    scene.check_motion(pose, pose)
    assert scene.moves_checked == 1
    assert scene.samples_checked > 0
