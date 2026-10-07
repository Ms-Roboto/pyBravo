"""Source-preserving liquid accounting over trusted, ordered simulator events.

This module never infers a recipe from text. Callers provide initial inventory,
well limits, and optional stage boundaries. Unknown initial quantities remain
unknown; a simulator action alone does not prove that a reagent was loaded.
Component amounts describe idealized, homogeneous bookkeeping, not validated
mixing, concentrations, liquid handling quality, or physical safety.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence


@dataclass(frozen=True, order=True)
class WellRef:
    labware: str
    well: str


@dataclass(frozen=True)
class InitialWell:
    """Initial contents of one physical well; ``None`` means unmeasured."""

    volume_ul: float | None
    material_id: str | None = None
    components_ul: Mapping[str, float] | None = None


@dataclass(frozen=True)
class WellLimit:
    """Caller-supplied limits; these are not inferred from labware names."""

    max_volume_ul: float | None = None
    target_final_volume_ul: float | None = None


@dataclass(frozen=True)
class StageBoundary:
    """Snapshot after a zero-based event index; -1 means before all events."""

    name: str
    after_event_index: int


@dataclass(frozen=True)
class WellSnapshot:
    volume_ul: float | None
    observed_net_change_ul: float
    components_ul: Mapping[str, float] | None
    possible_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class TipSnapshot:
    volume_ul: float
    components_ul: Mapping[str, float] | None
    possible_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class StageSnapshot:
    name: str
    after_event_index: int
    wells: Mapping[WellRef, WellSnapshot]
    tips: Mapping[tuple[str, int], TipSnapshot]


@dataclass(frozen=True)
class FlowIssue:
    code: str
    severity: Literal["error", "warning"]
    message: str
    event_index: int | None = None
    well: WellRef | None = None


@dataclass(frozen=True)
class LedgerResult:
    final_wells: Mapping[WellRef, WellSnapshot]
    snapshots: tuple[StageSnapshot, ...]
    issues: tuple[FlowIssue, ...]

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


WellResolver = Callable[[Mapping[str, Any], int], Sequence[WellRef]]
_EPS = 1e-8


def _nonnegative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite, nonnegative number")
    return float(value)


def _source_id(ref: WellRef) -> str:
    return f"{ref.labware}/{ref.well}"


def _frozen_components(components: Mapping[str, float] | None) -> Mapping[str, float] | None:
    return None if components is None else MappingProxyType(dict(sorted(components.items())))


def _scaled(components: Mapping[str, float], fraction: float) -> dict[str, float]:
    return {source: amount * fraction for source, amount in components.items() if amount * fraction > _EPS}


@dataclass
class _Liquid:
    volume_ul: float = 0.0
    components_ul: dict[str, float] | None = field(default_factory=dict)
    source_ids: set[str] = field(default_factory=set)
    homogeneous: bool = True

    def add(self, aliquot: _Liquid) -> None:
        if aliquot.volume_ul <= _EPS:
            return
        if self.volume_ul > _EPS and self.source_ids != aliquot.source_ids:
            self.homogeneous = False
        if self.components_ul is not None and aliquot.components_ul is not None:
            for source, amount in aliquot.components_ul.items():
                self.components_ul[source] = self.components_ul.get(source, 0.0) + amount
        else:
            self.components_ul = None
        self.volume_ul += aliquot.volume_ul
        self.source_ids.update(aliquot.source_ids)

    def take(self, amount: float) -> _Liquid:
        full = abs(self.volume_ul - amount) <= _EPS
        if self.components_ul is None or (not self.homogeneous and not full):
            components = None
        elif full:
            components = dict(self.components_ul)
        else:
            components = _scaled(self.components_ul, amount / self.volume_ul)
        result = _Liquid(amount, components, set(self.source_ids), self.homogeneous)
        self.volume_ul = max(0.0, self.volume_ul - amount)
        if self.volume_ul <= _EPS:
            self.components_ul = {}
            self.source_ids.clear()
            self.homogeneous = True
        elif components is None:
            self.components_ul = None
        elif self.components_ul is not None:
            for source, taken in components.items():
                self.components_ul[source] = max(0.0, self.components_ul[source] - taken)
        return result


@dataclass
class _Well:
    volume_ul: float | None
    liquid: _Liquid
    source_id: str
    observed_net_change_ul: float = 0.0
    received_liquid: bool = False

    def take(self, amount: float) -> _Liquid:
        self.observed_net_change_ul -= amount
        if self.volume_ul is None:
            if not self.received_liquid:
                return _Liquid(amount, {self.source_id: amount}, {self.source_id})
            return _Liquid(amount, None, set(self.liquid.source_ids), False)
        aliquot = self.liquid.take(amount)
        self.volume_ul = max(0.0, self.volume_ul - amount)
        return aliquot

    def add(self, aliquot: _Liquid) -> None:
        self.observed_net_change_ul += aliquot.volume_ul
        self.received_liquid = True
        if self.volume_ul is not None:
            self.volume_ul += aliquot.volume_ul
        self.liquid.add(aliquot)

    def snapshot(self) -> WellSnapshot:
        return WellSnapshot(
            self.volume_ul,
            self.observed_net_change_ul,
            _frozen_components(self.liquid.components_ul) if self.volume_ul is not None else None,
            tuple(sorted(self.liquid.source_ids)),
        )


def _make_initial(ref: WellRef, initial: InitialWell) -> _Well:
    volume = initial.volume_ul
    source = initial.material_id or _source_id(ref)
    if not source.strip():
        raise ValueError(f"{ref} has an empty material identifier")
    if volume is None:
        if initial.components_ul is not None:
            raise ValueError(f"{ref} cannot have exact components with unknown initial volume")
        return _Well(None, _Liquid(0.0, None, {source}), source)
    volume = _nonnegative(volume, f"initial volume for {ref}")
    if initial.components_ul is None:
        components = {source: volume} if volume > _EPS else {}
    else:
        components = {name: _nonnegative(amount, f"component {name!r} for {ref}")
                      for name, amount in initial.components_ul.items()}
        if any(not name.strip() for name in components):
            raise ValueError(f"{ref} has an empty component identifier")
        if abs(sum(components.values()) - volume) > _EPS:
            raise ValueError(f"{ref} component amounts do not equal initial volume")
    return _Well(volume, _Liquid(volume, components, set(components)), source)


def _event_count(event: Mapping[str, Any], current: int | None) -> int:
    value = event.get("channels", current if current is not None else 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 384:
        raise ValueError("channels must be an integer from 1 to 384")
    return value


def _event_wells(event: Mapping[str, Any], count: int, resolver: WellResolver | None) -> tuple[WellRef, ...]:
    if resolver is not None:
        refs = tuple(resolver(event, count))
    else:
        labware, well = event.get("labware"), event.get("well")
        if count != 1 or not isinstance(labware, str) or not isinstance(well, str):
            raise ValueError("multichannel actions require an explicit well resolver")
        refs = (WellRef(labware, well),)
    if len(refs) != count or any(not isinstance(ref, WellRef) or not ref.labware or not ref.well for ref in refs):
        raise ValueError(f"well resolver must return {count} nonempty WellRef values")
    return refs


def audit_material_flow(
    events: Iterable[Mapping[str, Any]],
    *,
    initial_wells: Mapping[WellRef, InitialWell] | None = None,
    well_limits: Mapping[WellRef, WellLimit] | None = None,
    stages: Sequence[StageBoundary] = (),
    stage_targets_ul: Mapping[str, Mapping[WellRef, float]] | None = None,
    well_resolver: WellResolver | None = None,
    volume_tolerance_ul: float = 0.01,
) -> LedgerResult:
    """Reconcile each observed stroke without inventing missing scientific inputs.

    A missing initial source is treated as an unmeasured pure stock and flagged.
    A missing initial destination may already contain liquid, so its final
    volume/composition remain unknown. Explicit ``InitialWell(0)`` establishes
    an empty vessel. Multichannel callers must resolve every physical channel;
    anchor wells alone cannot establish per-well flow.
    """
    event_list = list(events)
    tolerance = _nonnegative(volume_tolerance_ul, "volume_tolerance_ul")
    inventory = dict(initial_wells or {})
    limits = dict(well_limits or {})
    stage_targets = dict(stage_targets_ul or {})
    for ref, initial in inventory.items():
        if not isinstance(ref, WellRef) or not isinstance(initial, InitialWell):
            raise ValueError("initial_wells must map WellRef to InitialWell")
        if not ref.labware or not ref.well:
            raise ValueError("initial well references need nonempty labware and well names")
    for ref, limit in limits.items():
        if not isinstance(ref, WellRef) or not isinstance(limit, WellLimit):
            raise ValueError("well_limits must map WellRef to WellLimit")
        if not ref.labware or not ref.well:
            raise ValueError("well limit references need nonempty labware and well names")
        if limit.max_volume_ul is not None:
            _nonnegative(limit.max_volume_ul, f"maximum volume for {ref}")
        if limit.target_final_volume_ul is not None:
            _nonnegative(limit.target_final_volume_ul, f"target volume for {ref}")
    stage_names = {"final"}
    prior_index = -2
    for boundary in stages:
        if (not isinstance(boundary, StageBoundary) or not boundary.name.strip()
                or boundary.name in stage_names or boundary.after_event_index <= prior_index
                or boundary.after_event_index < -1 or boundary.after_event_index >= len(event_list)):
            raise ValueError("stages need unique names and increasing in-range event indices")
        stage_names.add(boundary.name)
        prior_index = boundary.after_event_index
    for name, targets in stage_targets.items():
        if name not in stage_names:
            raise ValueError(f"stage target {name!r} has no matching snapshot")
        for ref, amount in targets.items():
            if not isinstance(ref, WellRef):
                raise ValueError("stage targets must use WellRef keys")
            _nonnegative(amount, f"stage target for {ref}")

    wells = {ref: _make_initial(ref, value) for ref, value in inventory.items()}
    tips: dict[str, list[_Liquid] | None] = {}
    issues: list[FlowIssue] = []
    snapshots: list[StageSnapshot] = []
    boundaries: dict[int, StageBoundary] = {boundary.after_event_index: boundary for boundary in stages}

    for ref, well in wells.items():
        limit = limits.get(ref)
        if (limit is not None and limit.max_volume_ul is not None and well.volume_ul is not None
                and well.volume_ul > limit.max_volume_ul + tolerance):
            issues.append(FlowIssue("well_capacity_exceeded", "error",
                                    f"{ref} initially holds {well.volume_ul:g} µL; capacity is "
                                    f"{limit.max_volume_ul:g} µL.", -1, ref))

    def snapshot(name: str, index: int) -> None:
        views = MappingProxyType({ref: well.snapshot() for ref, well in sorted(wells.items())})
        tip_views = MappingProxyType({(instrument, channel): TipSnapshot(
            tip.volume_ul, _frozen_components(tip.components_ul), tuple(sorted(tip.source_ids)),
        ) for instrument, held in sorted(tips.items()) if held is not None
            for channel, tip in enumerate(held)})
        snapshots.append(StageSnapshot(name, index, views, tip_views))
        for ref, target in stage_targets.get(name, {}).items():
            current = views.get(ref)
            if current is None or current.volume_ul is None:
                issues.append(FlowIssue("unverifiable_stage_volume", "warning",
                                        f"{name}: {ref} has no known absolute volume.", index, ref))
            elif current.volume_ul > target + tolerance:
                issues.append(FlowIssue("stage_volume_over_target", "error",
                                        f"{name}: {ref} holds {current.volume_ul:g} µL; target is {target:g} µL.",
                                        index, ref))

    if -1 in boundaries:
        snapshot(boundaries[-1].name, -1)

    def snapshot_if_boundary(index: int) -> None:
        if index in boundaries:
            snapshot(boundaries[index].name, index)

    def get_well(ref: WellRef, kind: str, index: int) -> _Well:
        existing = wells.get(ref)
        if existing is not None:
            return existing
        source = _source_id(ref)
        result = _Well(None, _Liquid(0.0, None, {source}), source)
        wells[ref] = result
        issues.append(FlowIssue(
            "unmeasured_initial_source" if kind == "aspirate" else "unmeasured_initial_destination",
            "warning", f"Initial volume of {ref} was not supplied; its absolute balance is unknown.", index, ref,
        ))
        return result

    for index, event in enumerate(event_list):
        kind, instrument = event.get("kind"), event.get("instrument")
        if kind in {"pick", "drop", "aspirate", "dispense", "blow_out", "mix"}:
            if not isinstance(instrument, str) or not instrument:
                issues.append(FlowIssue("missing_instrument", "error", "Liquid event lacks an instrument.", index))
                snapshot_if_boundary(index)
                continue
            held = tips.get(instrument)
            try:
                channels = _event_count(event, len(held) if held is not None else None)
            except ValueError as exc:
                issues.append(FlowIssue("invalid_channels", "error", str(exc), index))
                snapshot_if_boundary(index)
                continue
            if kind == "pick":
                if held is not None:
                    issues.append(FlowIssue("tip_already_attached", "error", f"{instrument} picked a second tip.", index))
                else:
                    tips[instrument] = [_Liquid() for _ in range(channels)]
            elif kind == "drop":
                if held is None:
                    issues.append(FlowIssue("no_tip", "error", f"{instrument} dropped no tip.", index))
                else:
                    tips[instrument] = None
            elif held is None:
                issues.append(FlowIssue("no_tip", "error", f"{instrument} has no attached tip.", index))
            elif channels != len(held):
                issues.append(FlowIssue("channel_mismatch", "error", f"{instrument} changed channel count.", index))
            elif kind == "mix":
                try:
                    refs = _event_wells(event, channels, well_resolver)
                except ValueError as exc:
                    issues.append(FlowIssue("unresolved_wells", "error", str(exc), index))
                    snapshot_if_boundary(index)
                    continue
                for ref in refs:
                    get_well(ref, kind, index).liquid.homogeneous = True
            else:
                raw_volume = event.get("volume")
                if kind == "blow_out":
                    volumes = [tip.volume_ul for tip in held]
                    if not any(value > _EPS for value in volumes):
                        snapshot_if_boundary(index)
                        continue
                elif (isinstance(raw_volume, bool) or not isinstance(raw_volume, (int, float))
                      or not math.isfinite(raw_volume) or raw_volume <= 0):
                    issues.append(FlowIssue("invalid_stroke_volume", "error",
                                            "Aspirate/dispense volume must be finite and positive.", index))
                    snapshot_if_boundary(index)
                    continue
                else:
                    volumes = [float(raw_volume)] * channels
                try:
                    refs = _event_wells(event, channels, well_resolver)
                except ValueError as exc:
                    if kind == "blow_out":
                        issues.append(FlowIssue("unlocated_blow_out", "warning",
                                                "Blow-out liquid left the tip, but its destination is unknown.", index))
                        for tip in held:
                            if tip.volume_ul > _EPS:
                                tip.take(tip.volume_ul)
                    else:
                        issues.append(FlowIssue("unresolved_wells", "error", str(exc), index))
                    snapshot_if_boundary(index)
                    continue
                for channel, (ref, amount) in enumerate(zip(refs, volumes)):
                    if amount <= _EPS:
                        continue
                    tip = held[channel]
                    well = get_well(ref, kind, index)
                    if kind == "aspirate":
                        if well.volume_ul is not None and amount > well.volume_ul + _EPS:
                            issues.append(FlowIssue("source_volume_exhausted", "error",
                                                    f"{ref} has {well.volume_ul:g} µL, cannot aspirate {amount:g} µL.",
                                                    index, ref))
                            continue
                        tip.add(well.take(amount))
                    else:
                        if amount > tip.volume_ul + _EPS:
                            issues.append(FlowIssue("dispense_exceeds_held", "error",
                                                    f"{instrument} holds {tip.volume_ul:g} µL, cannot dispense "
                                                    f"{amount:g} µL.", index, ref))
                            continue
                        well.add(tip.take(amount))
                        limit = limits.get(ref)
                        if (limit is not None and limit.max_volume_ul is not None and well.volume_ul is not None
                                and well.volume_ul > limit.max_volume_ul + tolerance):
                            issues.append(FlowIssue("well_capacity_exceeded", "error",
                                                    f"{ref} holds {well.volume_ul:g} µL; capacity is "
                                                    f"{limit.max_volume_ul:g} µL.", index, ref))
        snapshot_if_boundary(index)

    snapshot("final", len(event_list) - 1)
    for ref, limit in limits.items():
        if limit.target_final_volume_ul is None:
            continue
        current = wells.get(ref)
        if current is None or current.volume_ul is None:
            issues.append(FlowIssue("unverifiable_final_volume", "warning",
                                    f"{ref} has no known absolute final volume.", well=ref))
            continue
        difference = current.volume_ul - limit.target_final_volume_ul
        if abs(difference) > tolerance:
            code = "target_volume_overfill" if difference > 0 else "target_volume_underfill"
            issues.append(FlowIssue(code, "error", f"{ref} holds {current.volume_ul:g} µL; "
                                    f"final target is {limit.target_final_volume_ul:g} µL.", well=ref))
    return LedgerResult(snapshots[-1].wells, tuple(snapshots), tuple(issues))
