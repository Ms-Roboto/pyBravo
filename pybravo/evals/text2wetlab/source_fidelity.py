"""Conservative checks that a generated program still follows its cited plan.

The plan is authored by the local model and grounded in the supplied task and
paper. These checks do not invent experimental quantities. They detect manual
liquid handoffs without a named stage and compare unambiguous, direct source
deliveries in a simulator event log with planned per-vessel additions.
Ambiguous mixed-source tips are intentionally left for review. Prepared
intermediates can be checked separately when their exact simulated well is
known.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Mapping, Sequence

from pybravo.evals.text2wetlab.planning import OT2Plan, PlanIssue

SOURCE_FIDELITY_GUIDANCE = """A quoted paper quantity and a cited plan are constraints on the generated program.
Do not silently change a planned transfer to the nearest executable pipette volume.
If a requested stroke is below every loaded pipette's stated working minimum,
do not invent a diluted stock or claim that an inventory vessel contains a
different concentration. If a listed diluent and a suitable vessel are
available, a newly prepared dilution may solve this: add an explicit earlier
preparation stage, keep each stock and diluent preparation stroke inside a
loaded pipette's working range, and preserve the required stock-equivalent
dose in the final reaction. Calculate the dilution factor from the prepared
stock and diluent volumes. Cite the stock concentration when it is supplied;
when it is not, preserve the stock-equivalent volume without inventing a
concentration. Do not merely round the aliquot up. Otherwise keep the
unsupported transfer unresolved, or assign it to a clearly named manual
operator-handoff stage with its actual component and volume when the task
permits manual work. Before code generation, account
for every robot-delivered component in a named pipette stage and every manual
addition in a named manual stage. The code's delivered quantities must agree
with the cited plan; if a source-supported alternative changes the plan,
regenerate and re-audit the plan first."""

_VOLUME_UL = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*[µμu]l\b", re.IGNORECASE)
_MASS = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*(ng|[µμu]g)\b(?!\s*/)",
                   re.IGNORECASE)
_MASS_CONCENTRATION = re.compile(
    r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*(ng|[µμu]g)\s*/\s*[µμu]l\b",
    re.IGNORECASE,
)
_MOLAR_CONCENTRATION = re.compile(
    r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*(nM|[µμu]M|mM)\b",
)
_DURATION = re.compile(
    r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|h|minutes?|mins?|min|seconds?|secs?|s)\b", re.IGNORECASE,
)
_TEMPERATURE_C = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*°?\s*c\b",
                            re.IGNORECASE)
_FINAL_VOLUME = re.compile(
    r"(?:\b(?:final|total)\b[^.;:]{0,45}?\b(?:of|is|was|at|=)\s*"
    r"|\b(?:reaction|pcr|assay)s?\b[^.;:]{0,45}?\b(?:in|at|of)\s*)"
    r"(\d+(?:\.\d+)?)\s*[µμu]l\b",
    re.IGNORECASE,
)


def _mass_ng(value: float, unit: str) -> float:
    return value * (1000 if unit.casefold().endswith("g") and unit.casefold() != "ng" else 1)


def _molar_um(value: float, unit: str) -> float:
    return value * (0.001 if unit == "nM" else 1000 if unit == "mM" else 1)


def _quoted_values(evidence: Sequence[Any], pattern: re.Pattern[str]) -> set[float]:
    return {float(match.group(1)) for item in evidence
            for match in pattern.finditer(item.quote)}


def audit_cited_plan_quantities(
    plan: OT2Plan, *, volume_tolerance_ul: float = 0.01,
) -> tuple[PlanIssue, ...]:
    """Compare unambiguous cited volumes and stock-equivalent masses.

    This deliberately leaves multi-quantity prose and derived premixes for
    review. It checks only one-to-one claims in a reaction/addition's own
    citation and a uniquely cited ng/µL or µg/µL starting stock.
    """
    if not math.isfinite(volume_tolerance_ul) or volume_tolerance_ul < 0:
        raise ValueError("volume_tolerance_ul must be finite and nonnegative")
    sources = {source.id: source for source in plan.deck_sources}
    issues: list[PlanIssue] = []
    for reaction_index, reaction in enumerate(plan.reactions):
        reaction_path = f"reactions[{reaction_index}]"
        final_claims = _quoted_values(reaction.evidence, _FINAL_VOLUME)
        if len(final_claims) == 1:
            quoted = next(iter(final_claims))
            if abs(quoted - reaction.final_volume_ul) > volume_tolerance_ul:
                issues.append(PlanIssue(
                    "cited_final_volume_mismatch", reaction_path,
                    f"{reaction.name} claims {reaction.final_volume_ul:g} µL but its cited "
                    f"source specifies {quoted:g} µL per final vessel.",
                ))
        for addition_index, item in enumerate(reaction.additions):
            path = f"{reaction_path}.additions[{addition_index}]"
            amount = item.addition.volume_ul
            if amount is None:
                continue
            # Batch-preparation citations often state per-reaction quantities;
            # comparing them to a prepared batch would assert false precision.
            if reaction.output_source_id is None:
                quoted_volumes = _quoted_values(item.evidence, _VOLUME_UL)
                component = re.sub(r"[^a-z0-9]+", " ", item.addition.component.casefold()).strip()
                if (len(quoted_volumes) == 1 and component and any(
                    component in re.sub(r"[^a-z0-9]+", " ", evidence.quote.casefold())
                    for evidence in item.evidence
                )):
                    quoted = next(iter(quoted_volumes))
                    if abs(quoted - amount) > volume_tolerance_ul:
                        issues.append(PlanIssue(
                            "cited_addition_volume_mismatch", path,
                            f"{item.addition.component} plans {amount:g} µL but its cited "
                            f"source specifies {quoted:g} µL.",
                        ))
            source = sources.get(item.source_id or "")
            if source is None or source.produced_by_stage is not None:
                continue
            concentrations = {
                _mass_ng(float(match.group(1)), match.group(2))
                for evidence in source.evidence
                for match in _MASS_CONCENTRATION.finditer(evidence.quote)
            }
            target_masses = {
                _mass_ng(float(match.group(1)), match.group(2))
                for evidence in item.evidence
                for match in _MASS.finditer(evidence.quote)
            }
            if len(concentrations) == len(target_masses) == 1:
                concentration = next(iter(concentrations))
                target_mass = next(iter(target_masses))
                if concentration > 0:
                    expected = target_mass / concentration
                    if abs(expected - amount) > volume_tolerance_ul:
                        issues.append(PlanIssue(
                            "cited_stock_equivalent_mismatch", path,
                            f"{target_mass:g} ng from {concentration:g} ng/µL requires "
                            f"{expected:g} µL of {source.id}; plan has {amount:g} µL.",
                        ))
            stock_molar = {
                _molar_um(float(match.group(1)), match.group(2))
                for evidence in source.evidence
                for match in _MOLAR_CONCENTRATION.finditer(evidence.quote)
            }
            final_molar = {
                _molar_um(float(match.group(1)), match.group(2))
                for evidence in item.evidence
                for match in _MOLAR_CONCENTRATION.finditer(evidence.quote)
            }
            if len(stock_molar) == len(final_molar) == 1 and reaction.final_volume_ul > 0:
                stock = next(iter(stock_molar))
                final = next(iter(final_molar))
                if stock > 0:
                    expected = final * reaction.final_volume_ul / stock
                    if abs(expected - amount) > volume_tolerance_ul:
                        issues.append(PlanIssue(
                            "cited_final_concentration_mismatch", path,
                            f"A {final:g} µM target from {stock:g} µM stock in a "
                            f"{reaction.final_volume_ul:g} µL reaction requires "
                            f"{expected:g} µL of {source.id}; plan has {amount:g} µL.",
                        ))
    return tuple(issues)


def audit_intermediate_preparation_events(
    plan: OT2Plan,
    events: Sequence[Mapping[str, Any]],
    *,
    output_locations: Mapping[str, tuple[str, str]],
    volume_tolerance_ul: float = 0.01,
) -> tuple[PlanIssue, ...]:
    """Flag observable over-preparation of a model-linked physical batch.

    ``output_locations`` maps a model-authored output source ID to its actual
    simulator labware label and well. Only positive external dispenses into
    that well count; aspirate/dispense mixing from the same well does not.
    Missing or ambiguous simulator events never establish an underfill.
    """
    if not math.isfinite(volume_tolerance_ul) or volume_tolerance_ul < 0:
        raise ValueError("volume_tolerance_ul must be finite and nonnegative")
    expected = {reaction.output_source_id: reaction.final_volume_ul
                for reaction in plan.reactions if reaction.output_source_id is not None}
    locations = {
        (labware.strip().casefold(), well.strip().casefold()): source_id
        for source_id, (labware, well) in output_locations.items()
        if source_id in expected
    }
    if not locations:
        return ()
    origins: dict[str, set[tuple[str, str]]] = defaultdict(set)
    delivered: dict[str, float] = defaultdict(float)
    for event in events:
        instrument = event.get("instrument")
        if not isinstance(instrument, str):
            continue
        kind = event.get("kind")
        if kind in {"pick", "drop"}:
            origins[instrument].clear()
            continue
        if kind not in {"aspirate", "dispense"}:
            continue
        place, well, volume = event.get("labware"), event.get("well"), event.get("volume")
        if (not isinstance(place, str) or not isinstance(well, str)
                or isinstance(volume, bool) or not isinstance(volume, (int, float))
                or not math.isfinite(volume) or volume <= 0):
            continue
        location = (place.strip().casefold(), well.strip().casefold())
        if kind == "aspirate":
            origins[instrument].add(location)
        elif location in locations and origins[instrument] and location not in origins[instrument]:
            delivered[locations[location]] += float(volume)
    return tuple(PlanIssue(
        "intermediate_overfilled", f"deck_sources/{source_id}",
        f"Simulated external dispenses put {amount:g} µL into prepared intermediate "
        f"{source_id}, above its single planned batch of {expected[source_id]:g} µL. "
        "Check for a second executable preparation or an incorrect batch volume.",
    ) for source_id, amount in delivered.items()
        if amount > expected[source_id] + volume_tolerance_ul)


def audit_parameterized_manual_stages(
    plan: OT2Plan, *, time_tolerance_s: float = 1.0,
    temperature_tolerance_c: float = 0.5,
) -> tuple[PlanIssue, ...]:
    """Require timed/manual source claims to be represented and ordered."""
    if (not math.isfinite(time_tolerance_s) or time_tolerance_s < 0 or
            not math.isfinite(temperature_tolerance_c) or temperature_tolerance_c < 0):
        raise ValueError("Manual-stage tolerances must be finite and nonnegative")
    issues: list[PlanIssue] = []
    for index, stage in enumerate(plan.stages):
        if stage.kind != "manual":
            continue
        path = f"stages[{index}]"
        times = {
            float(match.group(1)) * (3600 if match.group(2).casefold().startswith("h")
                                     else 60 if match.group(2).casefold().startswith("m") else 1)
            for evidence in stage.evidence for match in _DURATION.finditer(evidence.quote)
        }
        temperatures = _quoted_values(stage.evidence, _TEMPERATURE_C)
        if len(times) > 1 or len(temperatures) > 1:
            issues.append(PlanIssue(
                "multiple_manual_conditions_in_one_stage", path,
                f"{stage.name} cites several times or temperatures; represent the "
                "ordered phases as separate manual stages.",
            ))
            continue
        if times:
            expected = next(iter(times))
            if stage.duration_s is None or abs(stage.duration_s - expected) > time_tolerance_s:
                issues.append(PlanIssue(
                    "cited_manual_duration_mismatch", path,
                    f"{stage.name} cites {expected:g} s but plans "
                    f"{stage.duration_s!r} s.",
                ))
        if temperatures:
            expected = next(iter(temperatures))
            if (stage.temperature_c is None or
                    abs(stage.temperature_c - expected) > temperature_tolerance_c):
                issues.append(PlanIssue(
                    "cited_manual_temperature_mismatch", path,
                    f"{stage.name} cites {expected:g} °C but plans "
                    f"{stage.temperature_c!r} °C.",
                ))
        if index and (times or temperatures) and stage.after_stage is None:
            issues.append(PlanIssue(
                "manual_stage_order_unlinked", path,
                f"Timed or temperature-controlled manual stage {stage.name} needs "
                "after_stage naming the preceding workflow stage.",
            ))
    return tuple(issues)


def _strengths(value: str) -> set[float]:
    return {float(match) for match in re.findall(
        r"(?<![a-z0-9])([0-9]+(?:\.[0-9]+)?)\s*[x×](?![a-z0-9])",
        value.casefold(),
    )}


def audit_inventory_strength_claims(plan: OT2Plan) -> tuple[PlanIssue, ...]:
    """Preserve a uniquely stated stock multiplier in cited inventory evidence.

    The check is deliberately narrow. A source quotation with multiple
    multipliers is ambiguous, and an unstated concentration is not guessed.
    """
    issues: list[PlanIssue] = []
    known: dict[str, float] = {}
    for index, source in enumerate(plan.deck_sources):
        quoted = {value for item in source.evidence if item.source == "instruction"
                  for value in _strengths(item.quote)}
        if len(quoted) != 1:
            continue
        expected = next(iter(quoted))
        known[source.id] = expected
        claimed = _strengths(source.component)
        if claimed and claimed != {expected}:
            issues.append(PlanIssue(
                "source_strength_conflicts_with_inventory", f"deck_sources[{index}]",
                f"{source.id} is described as {expected:g}× in its cited inventory, "
                f"but the plan names it {source.component!r}.",
            ))
    for reaction_index, reaction in enumerate(plan.reactions):
        for addition_index, item in enumerate(reaction.additions):
            expected = known.get(item.source_id or "")
            stock = item.addition.stock_strength_x
            if expected is None or stock is None or math.isclose(stock, expected, abs_tol=0.01):
                continue
            issues.append(PlanIssue(
                "addition_stock_strength_conflicts_with_inventory",
                f"reactions[{reaction_index}].additions[{addition_index}]",
                f"{item.addition.component} uses {stock:g}× in the plan, but source "
                f"{item.source_id} is cited as {expected:g}× in the loaded inventory.",
            ))
    return tuple(issues)


def audit_manual_addition_stages(plan: OT2Plan) -> tuple[PlanIssue, ...]:
    """Require explicit, ordered operator stages for manual liquid additions.

    A free-form note that a component will be added later is not enough to
    establish where that action occurs in the protocol. The same ``stage_name``
    field used for robot additions can identify a manual stage exactly.
    """
    stages = {stage.name: stage for stage in plan.stages}
    issues: list[PlanIssue] = []
    for reaction_index, reaction in enumerate(plan.reactions):
        for addition_index, item in enumerate(reaction.additions):
            if item.addition.delivery != "manual":
                continue
            path = f"reactions[{reaction_index}].additions[{addition_index}]"
            stage = stages.get(item.stage_name) if item.stage_name is not None else None
            if stage is None or stage.kind != "manual":
                issues.append(PlanIssue(
                    "manual_addition_stage_missing", path,
                    f"Manual addition of {item.addition.component} needs stage_name equal "
                    "to an explicit, ordered manual handoff stage.",
                ))
    return tuple(issues)


def _label(value: str) -> str:
    """Normalize a simulator's ``<label> on <deck position>`` identifier."""
    name = re.split(r"\s+on\s+(?:slot\s+)?\d+\s*$", value, maxsplit=1,
                    flags=re.IGNORECASE)[0]
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def exact_prepared_output_locations(
    plan: OT2Plan, labware: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    """Resolve only model-named output wells with one exact simulator label.

    The plan supplies the output source ID, its inventory-cited well, and its
    catalog labware load name. A missing or ambiguous logger identity produces
    no mapping; this function never guesses a slot or well from protocol code.
    """
    sources = {source.id: source for source in plan.deck_sources}
    mapped: dict[str, tuple[str, str]] = {}
    for reaction in plan.reactions:
        source_id = reaction.output_source_id
        source = sources.get(source_id or "")
        if (source_id is None or source is None or source.produced_by_stage is None
                or source.well is None):
            continue
        matches = [label for label, load_name in labware.items()
                   if (load_name == source.labware and
                       re.sub(r"\s+on\s+(?:slot\s+)?\d+\s*$", "", label,
                              flags=re.IGNORECASE).strip().casefold()
                       == source.id.strip().casefold())]
        if len(matches) == 1:
            mapped[source_id] = (matches[0], source.well)
    return mapped


def audit_direct_source_delivery(
    plan: OT2Plan,
    events: Sequence[Mapping[str, Any]],
    *,
    labware: Mapping[str, str] | None = None,
    volume_tolerance_ul: float = 0.01,
) -> tuple[PlanIssue, ...]:
    """Compare source-tagged direct dispenses with per-vessel planned volumes.

    This works only when a deck-source ID matches a loaded labware label and a
    tip has aspirated from one recognized source since pickup. A tip that also
    aspirates from an unknown vessel has ambiguous liquid lineage, so its
    dispenses are skipped. Destinations that are planned generated
    intermediates are skipped because their batch volumes need separate
    accounting. The resulting finding is a concrete discrepancy, never an
    assertion that unobserved stages were correct.
    """
    if not math.isfinite(volume_tolerance_ul) or volume_tolerance_ul < 0:
        raise ValueError("volume_tolerance_ul must be finite and nonnegative")
    source_by_label = {_label(source.id): source.id for source in plan.deck_sources}
    intermediate_labels = {_label(source.id) for source in plan.deck_sources
                           if source.produced_by_stage is not None}
    expected: dict[str, set[float]] = defaultdict(set)
    for reaction in plan.reactions:
        totals: dict[str, float] = defaultdict(float)
        for item in reaction.additions:
            if (item.addition.delivery == "robot" and item.source_id is not None
                    and item.addition.volume_ul is not None):
                totals[item.source_id] += item.addition.volume_ul
        for source_id, total in totals.items():
            expected[source_id].add(total)
    # The plan can legitimately use one source at different quantities in
    # different reactions. Without a destination mapping, those cases cannot
    # be compared safely with the event log.
    unambiguous_expected = {source: next(iter(volumes)) for source, volumes in expected.items()
                            if len(volumes) == 1}
    loaded_labels = {_label(name) for name in (labware or {})}
    active_sources: dict[str, set[str]] = defaultdict(set)
    ambiguous: set[str] = set()
    aspirated_sources: set[str] = set()
    delivered: dict[tuple[str, str, str], float] = defaultdict(float)
    for event in events:
        instrument = event.get("instrument")
        kind = event.get("kind")
        if not isinstance(instrument, str):
            continue
        if kind in {"pick", "drop"}:
            active_sources[instrument].clear()
            ambiguous.discard(instrument)
            continue
        if kind not in {"aspirate", "dispense"}:
            continue
        place, well, volume = event.get("labware"), event.get("well"), event.get("volume")
        if (not isinstance(place, str) or not isinstance(well, str)
                or isinstance(volume, bool) or not isinstance(volume, (int, float))
                or not math.isfinite(volume) or volume <= 0):
            continue
        label = _label(place)
        if kind == "aspirate":
            source_id = source_by_label.get(label)
            if source_id is None:
                ambiguous.add(instrument)
            else:
                aspirated_sources.add(source_id)
                active_sources[instrument].add(source_id)
            continue
        sources = active_sources[instrument]
        if instrument in ambiguous or len(sources) != 1:
            continue
        source_id = next(iter(sources))
        if (label in {"trash", "waste", "fixedtrash"} or label in intermediate_labels
                or label in source_by_label):
            continue
        delivered[(source_id, label, well)] += float(volume)
    issues: list[PlanIssue] = []
    for source_id, target_ul in unambiguous_expected.items():
        if _label(source_id) in loaded_labels and source_id not in aspirated_sources:
            issues.append(PlanIssue(
                "planned_source_not_aspirated", f"deck_sources/{source_id}",
                f"The simulated run never aspirated from planned source {source_id}.",
            ))
        observed = [(target, well, amount) for (source, target, well), amount in delivered.items()
                    if source == source_id]
        for target, well, amount in observed:
            if abs(amount - target_ul) > volume_tolerance_ul:
                issues.append(PlanIssue(
                    "delivered_volume_differs_from_plan", f"deck_sources/{source_id}/{target}/{well}",
                    f"{source_id} delivered {amount:g} µL to {target} {well}; the cited "
                    f"plan specifies {target_ul:g} µL per destination vessel.",
                ))
    return tuple(issues)
