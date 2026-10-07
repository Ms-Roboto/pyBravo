"""Conservative targets for material accounting from an accepted local-model plan.

The plan has reaction quantities and named stages, but no physical destination
well field. A caller must separately bind each target to an exact well using
quotes present in the instruction or supplied paper. Ambiguous or ungrounded
bindings become review gaps; this module never guesses a labware position,
well, reaction count, preparation endpoint, or experimental volume.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from pybravo.evals.text2wetlab.material_ledger import StageBoundary, WellLimit, WellRef
from pybravo.evals.text2wetlab.planning import Evidence, OT2Plan, _quote_is_grounded
from pybravo.evals.text2wetlab.planning_runtime import PlanningResult


@dataclass(frozen=True)
class TargetBinding:
    """One cited, physical target proposed outside the plan's coarse schema."""

    reaction_name: str
    well: WellRef
    kind: Literal["final_reaction", "prepared_intermediate"]
    vessel_evidence: Evidence
    volume_evidence: Evidence
    source_id: str | None = None


@dataclass(frozen=True)
class TargetGap:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class PlanLedgerTargets:
    well_limits: Mapping[WellRef, WellLimit]
    stage_targets_ul: Mapping[str, Mapping[WellRef, float]]
    stages: tuple[StageBoundary, ...]
    review_gaps: tuple[TargetGap, ...]


_VOLUME = re.compile(r"(?<![a-z0-9])(?:\d+(?:\.\d+)?|\.\d+)\s*(?:µl|μl|ul)\b", re.IGNORECASE)


def _canonical(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("µ", "u").replace("μ", "u")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _runtime_labware_stem(labware: str) -> str:
    return re.sub(r"\s+on\s+(?:slot\s+)?\d+\s*$", "", labware, flags=re.IGNORECASE)


def _well_named(quote: str, well: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(well)}(?![A-Za-z0-9])", quote, re.IGNORECASE) is not None


def _evidence_text(evidence: Evidence, instruction: str, paper: str | None) -> str | None:
    if evidence.source == "instruction":
        corpus = instruction
    elif evidence.source == "paper":
        corpus = paper
    else:
        return None
    if corpus is None or not _quote_is_grounded(evidence.quote, corpus):
        return None
    return evidence.quote


def _volume_named(quote: str, expected_ul: float) -> bool:
    for match in _VOLUME.finditer(quote):
        number = re.match(r"(?:\d+(?:\.\d+)?|\.\d+)", match.group())
        if number is not None and math.isclose(float(number.group()), expected_ul, abs_tol=0.01):
            return True
    return False


def _quantity_tied_to_vessel_or_reaction(volume_quote: str, vessel_quote: str,
                                         reaction_name: str, well: WellRef) -> bool:
    if _canonical(volume_quote) == _canonical(vessel_quote):
        return True
    if (_well_named(volume_quote, well.well)
            and _canonical(_runtime_labware_stem(well.labware)) in _canonical(volume_quote)):
        return True
    generic = {"batch", "final", "in", "mix", "of", "prepare", "prepared", "reaction", "volume", "well"}
    identity = set(_canonical(reaction_name).split()) - generic
    return bool(identity and identity <= set(_canonical(volume_quote).split())
                and identity <= set(_canonical(vessel_quote).split()))


def _accepted_plan(result: PlanningResult) -> OT2Plan | None:
    if result.plan is None or not result.attempts or result.attempts[-1].get("status") != "accepted":
        return None
    return result.plan


def targets_from_accepted_plan(
    result: PlanningResult,
    bindings: Sequence[TargetBinding],
    *,
    instruction: str,
    scientific_source: str | None = None,
    stage_event_indices: Mapping[str, int] | None = None,
    event_count: int | None = None,
) -> PlanLedgerTargets:
    """Produce only target volumes with cited vessel, quantity and stage identity.

    `stage_event_indices` must come from a trusted alignment of model stages to
    observed simulator events. This function validates that an index exists and
    is ordered relative to other supplied stages, but cannot establish the
    alignment itself. Pass `event_count` to reject out-of-range boundaries.
    Missing indices leave intermediate targets as review gaps.
    """
    if event_count is not None and (isinstance(event_count, bool) or not isinstance(event_count, int)
                                    or event_count < 0):
        raise ValueError("event_count must be a nonnegative integer")
    plan = _accepted_plan(result)
    if plan is None:
        return PlanLedgerTargets(MappingProxyType({}), MappingProxyType({}), (), (
            TargetGap("plan_not_accepted", "plan", "No audited, accepted local-model plan is available."),
        ))

    reactions: dict[str, list] = {}
    for reaction in plan.reactions:
        reactions.setdefault(reaction.name, []).append(reaction)
    sources = {source.id: source for source in plan.deck_sources}
    stages = {stage.name: (index, stage) for index, stage in enumerate(plan.stages)}
    supplied_indices = dict(stage_event_indices or {})
    limits: dict[WellRef, WellLimit] = {}
    prepared: dict[str, dict[WellRef, float]] = {}
    issues: list[TargetGap] = []
    used_wells: set[WellRef] = set()

    for index, binding in enumerate(bindings):
        path = f"bindings[{index}]"
        if not isinstance(binding, TargetBinding):
            issues.append(TargetGap("invalid_binding", path, "Expected a TargetBinding object."))
            continue
        if binding.well in used_wells:
            issues.append(TargetGap("duplicate_target_well", path,
                                    f"{binding.well} is bound more than once; no conflicting target is applied."))
            limits.pop(binding.well, None)
            for targets in prepared.values():
                targets.pop(binding.well, None)
            continue
        used_wells.add(binding.well)
        matches = reactions.get(binding.reaction_name, ())
        if len(matches) != 1:
            issues.append(TargetGap("ambiguous_reaction", path,
                                    f"Reaction {binding.reaction_name!r} is absent or not unique in the plan."))
            continue
        reaction = matches[0]
        expected = reaction.final_volume_ul
        if not math.isfinite(expected) or expected < 0:
            issues.append(TargetGap("invalid_reaction_volume", path, "Reaction volume is not finite and nonnegative."))
            continue
        vessel_quote = _evidence_text(binding.vessel_evidence, instruction, scientific_source)
        volume_quote = _evidence_text(binding.volume_evidence, instruction, scientific_source)
        if vessel_quote is None or volume_quote is None:
            issues.append(TargetGap("ungrounded_target_evidence", path,
                                    "Vessel and volume quotes must each occur in their declared source."))
            continue
        if (not binding.well.labware.strip() or not binding.well.well.strip()
                or not _well_named(vessel_quote, binding.well.well)
                or _canonical(_runtime_labware_stem(binding.well.labware)) not in _canonical(vessel_quote)):
            issues.append(TargetGap("vessel_identity_unresolved", path,
                                    "The cited passage does not identify this exact labware and well."))
            continue
        if not _volume_named(volume_quote, expected):
            issues.append(TargetGap("volume_identity_unresolved", path,
                                    f"The cited passage does not state the plan's {expected:g} µL target."))
            continue
        if not _quantity_tied_to_vessel_or_reaction(volume_quote, vessel_quote,
                                                    binding.reaction_name, binding.well):
            issues.append(TargetGap("volume_vessel_link_unresolved", path,
                                    "The quantity quote does not identify the same well or named reaction."))
            continue

        if binding.kind == "final_reaction":
            if binding.source_id is not None:
                issues.append(TargetGap("unexpected_source_binding", path,
                                        "A final reaction target must not claim an intermediate source ID."))
                continue
            limits[binding.well] = WellLimit(target_final_volume_ul=expected)
        elif binding.kind == "prepared_intermediate":
            source = sources.get(binding.source_id or "")
            if source is None or source.produced_by_stage is None:
                issues.append(TargetGap("intermediate_source_unresolved", path,
                                        "The target needs a generated deck source with a producer stage."))
                continue
            stage_name = source.produced_by_stage
            stage_row = stages.get(stage_name)
            if (stage_row is None or stage_row[1].kind not in {"pipette", "manual"}
                    or _canonical(source.labware) != _canonical(_runtime_labware_stem(binding.well.labware))
                    or _canonical(source.component) not in _canonical(vessel_quote)):
                issues.append(TargetGap("intermediate_identity_unresolved", path,
                                        "Generated source, producer stage, component and cited vessel do not agree."))
                continue
            if stage_name not in supplied_indices:
                issues.append(TargetGap("stage_boundary_unresolved", path,
                                        f"No trusted event boundary was supplied for stage {stage_name!r}."))
                continue
            prepared.setdefault(stage_name, {})[binding.well] = expected
        else:
            issues.append(TargetGap("invalid_target_kind", path, "Unknown target kind."))

    # A target for a stage is useful only if the event boundary is a valid,
    # ordered index. Do not silently attach it to a nearby event.
    ordered_boundaries: list[StageBoundary] = []
    last_event_index = -2
    for stage_name in sorted(prepared, key=lambda name: stages[name][0]):
        boundary = supplied_indices[stage_name]
        if (isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0
                or boundary <= last_event_index or (event_count is not None and boundary >= event_count)):
            issues.append(TargetGap("stage_boundary_invalid", f"stage_event_indices/{stage_name}",
                                    "Stage event boundaries must be integer indices in increasing plan order."))
            prepared.pop(stage_name)
            continue
        last_event_index = boundary
        ordered_boundaries.append(StageBoundary(stage_name, boundary))

    return PlanLedgerTargets(
        MappingProxyType(limits),
        MappingProxyType({name: MappingProxyType(values) for name, values in prepared.items()}),
        tuple(ordered_boundaries),
        tuple(issues),
    )
