"""The software simulator uses the configured Bravo's class catalog directly."""

from pathlib import Path

from pybravo import liquid_classes
from pybravo.bravo import Bravo
from pybravo.controllers.simulation import SimulationController
from pybravo.web.server import _validate_workflow_liquid_classes
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.methods import lookup_methods
from pybravo.workflow.protocols.liquid_class_proposals import LiquidClassProposalQuery, propose_liquid_classes


SIMULATION_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"
CONFIGURED_BRAVO = "04-91-62-CF-7B-B0"


def test_simulator_uses_the_same_st10_classes_without_a_second_catalog():
    bravo = Bravo(profile=SIMULATION_PROFILE)
    try:
        assert bravo.profile.connection.controller_type == "simulation"
        assert bravo.profile.connection.use_ethernet is False
        assert bravo.profile.connection.address == ""
        assert bravo.machine_id == CONFIGURED_BRAVO
        bravo.connect()
        assert isinstance(bravo.controller, SimulationController)

        context = machine_context(bravo)
        shared = liquid_classes.list_liquid_classes(
            machine_id=CONFIGURED_BRAVO, head_type="HT_384_D_70", tip_id="st_10ul"
        )
        active = [row for row in context["liquid_classes"] if row.get("tip_id") == "st_10ul"]
        assert {row["liquid_class_id"] for row in active} == {row["liquid_class_id"] for row in shared}
        assert {row["liquid_class_id"] for row in active} >= {
            "liq_66d89a6738", "liq_15de77e0bd", "liq_89aeface22",
        }
        assert liquid_classes.list_liquid_classes(machine_id="SIMULATED") == []

        bravo._tips_on_head = True
        bravo._tip_definition_id = "st_10ul"
        selected = bravo._resolve_liquid_class("D10 384 Head 1-10 1ul-s")
        assert selected["liquid_class_id"] == "liq_15de77e0bd"
        assert selected["machine_id"] == CONFIGURED_BRAVO

        # Changing profile identity invalidates saved protocol context pins.
        bravo.profile.connection.machine_id = "SIMULATED"
        assert machine_context(bravo)["context_hash"] != context["context_hash"]
    finally:
        bravo.disconnect()


def test_method_lookup_and_prelaunch_resolve_the_shared_class_but_do_not_qualify_it():
    bravo = Bravo(profile=SIMULATION_PROFILE)
    try:
        context = machine_context(bravo)
        result = lookup_methods(context, {
            "operation": "transfer",
            "tip_id": "st_10ul",
            "tipbox_id": "lw-4914769d0af7",
            "source_labware_id": "01KD4PYY7N4EG0E22P3QP1RHB9",
            "destination_labware_id": "lw-3918306f45b8",
            "reagent_id": "neat DMSO",
            "reagent_family": "DMSO",
            "volume_ul": 5.0,
            "source_anchor": "A1",
            "destination_anchor": "A1",
        })
        assert result["issues"] == []
        imported = next(row for row in result["candidates"] if row["method_id"] == "imported:liq_15de77e0bd")
        assert imported["status"] == "imported_unverified"
        assert imported["execution_ready"] is False
        assert imported["applicability"]["machine_ids"] == [CONFIGURED_BRAVO]
        reported = next(row for row in result["candidates"]
                        if row["method_id"] == "candidate:384st10-neat-dmso-pp-ldv-5ul")
        assert reported["match_kind"] == "exact"
        assert reported["execution_ready"] is False
        assert "dispense.distance_from_bottom_mm" in reported["missing_fields"]

        proposals = propose_liquid_classes(context, LiquidClassProposalQuery(
            tip_id="st_10ul", volume_ul=5.0, reagent_id="neat DMSO", reagent_family="DMSO",
            source_labware_id="01KD4PYY7N4EG0E22P3QP1RHB9",
            destination_labware_id="lw-3918306f45b8",
            source_initial_volume_ul=45.0, source_dead_volume_ul=6.5,
            total_source_withdrawal_ul=10.0, destination_initial_volume_ul=0.0,
        ))
        selected = proposals["candidates"][0]
        assert selected["liquid_class_id"] == "liq_89aeface22"
        assert selected["provenance"]["source_archive"]["roi_state"] == "IN_DEVELOPMENT"
        assert selected["geometry_proposal"]["aspirate"]["distance_from_bottom_mm"] == 1.694
        assert selected["geometry_proposal"]["dispense"]["status"] == "unresolved"

        graph = {"nodes": [{"id": 1, "type": "liquid/Aspirate", "properties": {
            "liquid_class": "D10 384 Head 1-10 1ul-s"
        }}]}
        assert _validate_workflow_liquid_classes(graph, bravo) == []
        graph["nodes"][0]["properties"]["liquid_class"] = "Nonexistent ST10 class"
        assert _validate_workflow_liquid_classes(graph, bravo)[0]["field"] == "liquid_class"
    finally:
        bravo.disconnect()
