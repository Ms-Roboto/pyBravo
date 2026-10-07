"""Pure accounting checks for proposed liquid-handling reactions.

Callers supply a structured recipe and, optionally, a sequence of pipette
strokes. This module does not infer scientific facts from prose or execute a
protocol. It can audit any platform's reaction math before a hardware or
simulator-specific validation step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal


@dataclass(frozen=True)
class Addition:
    """One planned component addition to each reaction.

    ``stock_strength_x`` and ``target_strength_x`` express the same unit,
    typically a multiplier such as 2× stock and 1× final concentration.
    ``delivery`` distinguishes on-deck liquid from a documented manual step.
    """

    component: str
    volume_ul: float | None
    stock_strength_x: float | None = None
    target_strength_x: float | None = None
    is_diluent: bool = False
    delivery: Literal["robot", "manual"] = "robot"


@dataclass(frozen=True)
class Reaction:
    name: str
    final_volume_ul: float
    additions: tuple[Addition, ...]
    diluent_name: str | None = None


@dataclass(frozen=True)
class AccountingIssue:
    code: str
    message: str
    component: str | None = None
    expected: float | None = None
    observed: float | None = None
    stroke_index: int | None = None


@dataclass(frozen=True)
class PipetteRange:
    name: str
    min_volume_ul: float
    max_volume_ul: float


@dataclass(frozen=True)
class Stroke:
    pipette: str
    kind: Literal["pick_up_tip", "drop_tip", "aspirate", "dispense", "mix", "move_to", "blow_out"]
    volume_ul: float | None = None
    location: str | None = None
    repetitions: int = 1


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def audit_reaction(
    reaction: Reaction,
    *,
    available_components: Iterable[str] | None = None,
    volume_tolerance_ul: float = 0.01,
    strength_tolerance_x: float = 0.01,
) -> tuple[AccountingIssue, ...]:
    """Audit final volume, diluent, stock dilution, and available ingredients.

    Ingredient names are caller-supplied stable identifiers. If known, pass
    available on-deck names to catch a proposed robot addition from a source
    that was never supplied; manual additions remain explicitly distinct.
    """
    if not _positive(reaction.final_volume_ul):
        return (AccountingIssue("invalid_final_volume", "Final reaction volume must be positive and finite."),)
    if not math.isfinite(volume_tolerance_ul) or volume_tolerance_ul < 0:
        raise ValueError("volume_tolerance_ul must be finite and nonnegative")
    if not math.isfinite(strength_tolerance_x) or strength_tolerance_x < 0:
        raise ValueError("strength_tolerance_x must be finite and nonnegative")
    available = ({name.strip().casefold() for name in available_components}
                 if available_components is not None else None)
    issues: list[AccountingIssue] = []
    known_total = 0.0
    unresolved_volume = False
    by_component: dict[str, tuple[float, float, float]] = {}
    has_diluent = False
    for addition in reaction.additions:
        name = addition.component.strip()
        if not name:
            issues.append(AccountingIssue("missing_component_name", "An addition has no component name."))
            continue
        is_diluent = addition.is_diluent or (reaction.diluent_name is not None
                                              and name.casefold() == reaction.diluent_name.casefold())
        has_diluent |= is_diluent
        if addition.delivery not in {"robot", "manual"}:
            issues.append(AccountingIssue("invalid_delivery", f"{name} has an unknown delivery mode.", name))
        elif available is not None and addition.delivery == "robot" and name.casefold() not in available:
            issues.append(AccountingIssue(
                "unavailable_component", f"{name} is proposed for robot delivery but is not in the supplied sources.",
                name,
            ))
        volume = addition.volume_ul
        if volume is None:
            unresolved_volume = True
            issues.append(AccountingIssue("missing_volume", f"{name} has no specified volume.", name))
            continue
        if not math.isfinite(volume) or volume < 0:
            unresolved_volume = True
            issues.append(AccountingIssue("invalid_volume", f"{name} volume must be finite and nonnegative.", name))
            continue
        known_total += volume
        if (addition.stock_strength_x is None) != (addition.target_strength_x is None):
            issues.append(AccountingIssue(
                "incomplete_strength", f"{name} needs both stock and target strengths for dilution accounting.", name,
            ))
        elif addition.stock_strength_x is not None and addition.target_strength_x is not None:
            stock = addition.stock_strength_x
            target = addition.target_strength_x
            if not _positive(stock) or not _positive(target):
                issues.append(AccountingIssue("invalid_strength", f"{name} strengths must be positive and finite.", name))
                continue
            key = name.casefold()
            prior = by_component.get(key)
            if prior is not None and (prior[1], prior[2]) != (stock, target):
                issues.append(AccountingIssue(
                    "conflicting_strength", f"{name} has conflicting stock/target strengths.", name,
                ))
                continue
            by_component[key] = ((prior[0] if prior else 0.0) + volume, stock, target)

    if not unresolved_volume:
        gap = reaction.final_volume_ul - known_total
        if abs(gap) > volume_tolerance_ul:
            issues.append(AccountingIssue(
                "final_volume_mismatch",
                f"{reaction.name} totals {known_total:g} µL; target is {reaction.final_volume_ul:g} µL.",
                expected=reaction.final_volume_ul, observed=known_total,
            ))
        if gap > volume_tolerance_ul and reaction.diluent_name and not has_diluent:
            issues.append(AccountingIssue(
                "missing_diluent",
                f"{reaction.name} is short by {gap:g} µL and has no {reaction.diluent_name} addition.",
                component=reaction.diluent_name, expected=gap, observed=0.0,
            ))
        for component, (volume, stock, target) in by_component.items():
            expected_volume = reaction.final_volume_ul * target / stock
            if abs(volume - expected_volume) > volume_tolerance_ul:
                issues.append(AccountingIssue(
                    "stock_dilution_mismatch",
                    f"{component} needs {expected_volume:g} µL of {stock:g}× stock for "
                    f"{target:g}× in {reaction.final_volume_ul:g} µL; plan has {volume:g} µL.",
                    component=component, expected=expected_volume, observed=volume,
                ))
            if known_total > 0:
                realized = stock * volume / known_total
                if abs(realized - target) > strength_tolerance_x:
                    issues.append(AccountingIssue(
                        "realized_strength_mismatch",
                        f"{component} would be {realized:g}× in the planned {known_total:g} µL, "
                        f"rather than {target:g}×.", component=component, expected=target, observed=realized,
                    ))
    return tuple(issues)


def audit_strokes(
    strokes: Iterable[Stroke], pipettes: Iterable[PipetteRange],
    *, volume_tolerance_ul: float = 0.01,
) -> tuple[AccountingIssue, ...]:
    """Check tip state, pipette range, held volume, and mix location."""
    if not math.isfinite(volume_tolerance_ul) or volume_tolerance_ul < 0:
        raise ValueError("volume_tolerance_ul must be finite and nonnegative")
    ranges = {pipette.name: pipette for pipette in pipettes}
    attached = {name: False for name in ranges}
    held = {name: 0.0 for name in ranges}
    last_liquid_location: dict[str, str | None] = {name: None for name in ranges}
    issues: list[AccountingIssue] = []
    for index, stroke in enumerate(strokes, start=1):
        pipette = ranges.get(stroke.pipette)
        if pipette is None:
            issues.append(AccountingIssue(
                "unknown_pipette", f"Stroke {index} refers to unknown pipette {stroke.pipette}.",
                stroke_index=index,
            ))
            continue
        if not _positive(pipette.max_volume_ul) or not math.isfinite(pipette.min_volume_ul) or (
                pipette.min_volume_ul < 0) or (
                pipette.min_volume_ul > pipette.max_volume_ul):
            issues.append(AccountingIssue("invalid_pipette_range", f"{pipette.name} has an invalid volume range."))
            continue
        name = pipette.name
        if stroke.kind == "pick_up_tip":
            if attached[name]:
                issues.append(AccountingIssue("tip_already_attached", f"Stroke {index} picks a second tip on {name}.",
                                              stroke_index=index))
            attached[name] = True
            held[name] = 0.0
            last_liquid_location[name] = None
            continue
        if stroke.kind == "drop_tip":
            if not attached[name]:
                issues.append(AccountingIssue("no_tip", f"Stroke {index} drops a tip that {name} does not hold.",
                                              stroke_index=index))
            attached[name] = False
            held[name] = 0.0
            last_liquid_location[name] = None
            continue
        if stroke.kind == "move_to":
            last_liquid_location[name] = stroke.location
            continue
        if stroke.kind == "blow_out":
            if not attached[name]:
                issues.append(AccountingIssue("no_tip", f"Stroke {index} blows out without a tip on {name}.",
                                              stroke_index=index))
            held[name] = 0.0
            continue
        if stroke.kind not in {"aspirate", "dispense", "mix"}:
            issues.append(AccountingIssue("unknown_stroke", f"Stroke {index} has unknown action {stroke.kind}.",
                                          stroke_index=index))
            continue
        if not attached[name]:
            issues.append(AccountingIssue("no_tip", f"Stroke {index} uses {name} without a tip.", stroke_index=index))
        volume = stroke.volume_ul
        if not _positive(volume):
            issues.append(AccountingIssue("invalid_stroke_volume", f"Stroke {index} has no positive liquid volume.",
                                          stroke_index=index))
            continue
        if volume + volume_tolerance_ul < pipette.min_volume_ul:
            issues.append(AccountingIssue(
                "below_pipette_minimum", f"Stroke {index} requests {volume:g} µL on {name}; "
                f"minimum is {pipette.min_volume_ul:g} µL.", expected=pipette.min_volume_ul,
                observed=volume, stroke_index=index,
            ))
        if volume > pipette.max_volume_ul + volume_tolerance_ul:
            issues.append(AccountingIssue(
                "above_pipette_maximum", f"Stroke {index} requests {volume:g} µL on {name}; "
                f"maximum is {pipette.max_volume_ul:g} µL.", expected=pipette.max_volume_ul,
                observed=volume, stroke_index=index,
            ))
        location = stroke.location or last_liquid_location[name]
        if location is None:
            issues.append(AccountingIssue(
                "missing_liquid_location", f"Stroke {index} {stroke.kind} on {name} has no liquid well location; "
                "after tip pickup, an omitted location may target the tip rack.", stroke_index=index,
            ))
        else:
            last_liquid_location[name] = location
        if stroke.kind == "aspirate":
            held[name] += volume
            if held[name] > pipette.max_volume_ul + volume_tolerance_ul:
                issues.append(AccountingIssue(
                    "pipette_overfilled", f"Stroke {index} leaves {held[name]:g} µL in {name}; "
                    f"capacity is {pipette.max_volume_ul:g} µL.", expected=pipette.max_volume_ul,
                    observed=held[name], stroke_index=index,
                ))
        elif stroke.kind == "dispense":
            if volume > held[name] + volume_tolerance_ul:
                issues.append(AccountingIssue(
                    "dispense_exceeds_held", f"Stroke {index} dispenses {volume:g} µL from {name} "
                    f"with only {held[name]:g} µL held.", expected=held[name], observed=volume,
                    stroke_index=index,
                ))
            held[name] = max(0.0, held[name] - volume)
        else:
            if held[name] + volume > pipette.max_volume_ul + volume_tolerance_ul:
                issues.append(AccountingIssue(
                    "mix_exceeds_capacity", f"Stroke {index} mixes {volume:g} µL while {name} already holds "
                    f"{held[name]:g} µL; capacity is {pipette.max_volume_ul:g} µL.",
                    expected=pipette.max_volume_ul, observed=held[name] + volume, stroke_index=index,
                ))
            if stroke.repetitions < 1:
                issues.append(AccountingIssue(
                    "invalid_mix_repetitions", f"Stroke {index} has no positive mix repetition count.",
                    stroke_index=index,
                ))
    for name, has_tip in attached.items():
        if has_tip:
            issues.append(AccountingIssue("tip_left_attached", f"{name} ends with an attached tip."))
    return tuple(issues)
