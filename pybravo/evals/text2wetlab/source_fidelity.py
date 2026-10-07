"""Conservative checks that a generated program still follows its cited plan.

The plan is authored by the local model and grounded in the supplied task and
paper. These checks do not invent experimental quantities. They detect manual
liquid handoffs without a named stage and compare unambiguous, direct source
deliveries in a simulator event log with planned per-vessel additions.
Intermediate preparation and mixed-source tips are intentionally left for
review instead of being assigned a possibly false lineage.
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
dose in the final reaction. Cite the source stock concentration and calculate
the dilution factor; do not merely round the aliquot up. Otherwise keep the
unsupported transfer unresolved, or assign it to a clearly named manual
operator-handoff stage with its actual component and volume when the task
permits manual work. Before code generation, account
for every robot-delivered component in a named pipette stage and every manual
addition in a named manual stage. The code's delivered quantities must agree
with the cited plan; if a source-supported alternative changes the plan,
regenerate and re-audit the plan first."""


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
