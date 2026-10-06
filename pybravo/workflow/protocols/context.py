"""Read-only snapshot of the configured machine and authoritative catalogs."""

from dataclasses import asdict

from pybravo import liquid_classes
from pybravo.deck.labware import normalize_labware_definitions
from pybravo.tip_offsets import get_tip_offset_table
from pybravo.tips import get_tip_definitions_for_head
from pybravo.workflow.protocols.store import digest
from pybravo.workflow.protocols.tipbox_choices import compatible_tipbox_choices, tipbox_catalog_candidates


def machine_context(bravo) -> dict:
    profile = bravo.profile._to_dict()
    definitions, _ = normalize_labware_definitions(bravo.labware_catalog.list_definitions())
    head = bravo.profile.head.head_type.name
    tips = []
    for definition in get_tip_definitions_for_head(head):
        tip = asdict(definition)
        tip["id"] = tip["tip_id"]
        tip["supported_head_types"] = list(tip["compatible_heads"])
        tips.append(tip)
    classes = liquid_classes.list_liquid_classes(machine_id=bravo.machine_id, head_type=head)
    context = {
        "profile": profile, "profile_name": bravo.profile.name, "machine_id": bravo.machine_id,
        "head_type": head, "head_mode": bravo._head_mode.to_dict() if bravo._head_mode else {},
        "has_gripper": (profile["connection"]["controller_type"] != "agile_srt"
                        and "G" in bravo.profile.axes and "Zg" in bravo.profile.axes),
        "labware": [definition.to_summary() for definition in definitions], "tip_definitions": tips,
        "liquid_classes": classes, "pipette_techniques": liquid_classes.list_pipette_techniques(),
        "max_operations": 500,
        "tip_offsets": [asdict(entry) for entry in get_tip_offset_table().entries],
    }
    if "W" in bravo.profile.axes:
        context["head_max_volume_ul"] = bravo.profile.axes["W"].range.max_pos
    context["tipbox_choices"] = compatible_tipbox_choices(head, context["labware"], tips)
    context["tipbox_catalog_candidates"] = tipbox_catalog_candidates(
        head, context["labware"], tips, tip_offsets=context["tip_offsets"],
    )
    context["tipbox_choices_reason"] = "" if context["tipbox_choices"] else (
        f"No catalog tip box has a verified rack grid and explicitly linked tip definition "
        f"compatible with {head}. Complete the catalog metadata "
        "or select the correct configured head before choosing tips."
    )
    context["profile_hash"] = digest(profile)
    # An approved protocol pins the method library as well as the physical
    # catalogs. Editing an authored method or its referenced liquid class must
    # invalidate the previous strict simulation and approval.
    from pybravo.workflow.protocols.methods import method_registry
    context["method_registry_digest"] = method_registry(context)["digest"]
    # Deliberately exclude volatile runtime positions and fitted tips. The setup
    # supplies head mode/tips; the approved profile/catalog values must not drift.
    context["context_hash"] = digest({k: v for k, v in context.items() if k != "head_mode"})
    context["connected"] = bravo.is_connected
    context["controller_type"] = profile["connection"]["controller_type"]
    return context
