"""Cited, model-authored reaction arithmetic for staged ActionPlan experiments.

This module never chooses protocol ingredients or volumes. It checks a model's
explicit recipe against cited reaction totals and the pinned simulator events.
Arithmetic acceptance does not qualify a biological method.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .action_ir import ActionPlanError, LabwareFacts, SourceSpan
from .phased_action_ir import PhasedSetup, _quotes, _selected_wells
from .reaction import Addition, Reaction, audit_reaction


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class MathComponent(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    phase: Literal["premix", "later"]
    volume_ul_per_reaction: float | None = Field(ge=0)
    source_material_ids: list[str] = Field(default_factory=list, max_length=48)
    source_usage: Literal["single", "each", "one_of"] = "single"
    source_labware: str | None = None
    source_well: str | None = None
    delivery: Literal["robot", "manual"] = "robot"
    basis: Literal["direct", "calculated", "assumption"]
    assumption_note: str | None = Field(default=None, max_length=600)
    stock_strength_x: float | None = Field(default=None, gt=0)
    target_strength_x: float | None = Field(default=None, gt=0)
    is_diluent: bool = False
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class ReactionMathPlan(_Strict):
    stage_id: str
    reaction_count: int = Field(ge=1, le=384)
    final_volume_ul: float = Field(gt=0)
    premix_labware: str
    premix_well: str
    premix_target_ul_per_reaction: float = Field(ge=0)
    components: list[MathComponent] = Field(min_length=1, max_length=48)
    evidence_refs: list[str] = Field(min_length=1, max_length=40)


@dataclass(frozen=True)
class ReactionMathContext:
    stage_id: str
    reaction_count: int
    final_volume_ul: float
    premix_labware: str
    premix_well: str
    task_ref: str
    paper_ref: str


def _issue(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def parse_and_audit_math(
    raw: dict[str, Any], *, context: ReactionMathContext,
    setup: PhasedSetup, spans: Mapping[str, SourceSpan],
    instruction: str, paper: str | None,
    labware_catalog: Mapping[str, LabwareFacts],
    allowed_paper_refs: Sequence[str],
) -> tuple[ReactionMathPlan | None, list[dict[str, Any]]]:
    """Validate source scope, physical stock identity, and reaction balance."""
    try:
        plan = ReactionMathPlan.model_validate(raw)
    except ValidationError as exc:
        return None, [_issue(
            "math_schema_rejected",
            path=".".join(str(part) for part in error["loc"]),
            error_type=error["type"],
        ) for error in exc.errors()[:8]]
    issues: list[dict[str, Any]] = []
    if plan.stage_id != context.stage_id:
        issues.append(_issue("stage_id_mismatch"))
    if plan.reaction_count != context.reaction_count:
        issues.append(_issue("reaction_count_mismatch", expected=context.reaction_count,
                             observed=plan.reaction_count))
    if not math.isclose(plan.final_volume_ul, context.final_volume_ul, abs_tol=0.01):
        issues.append(_issue("final_volume_mismatch", expected=context.final_volume_ul,
                             observed=plan.final_volume_ul))
    if (plan.premix_labware, plan.premix_well) != (
        context.premix_labware, context.premix_well,
    ):
        issues.append(_issue("premix_well_mismatch"))
    if not {context.task_ref, context.paper_ref} <= set(plan.evidence_refs):
        issues.append(_issue("reaction_context_citation_missing"))
    allowed_refs = {ref for ref, span in spans.items() if span.source == "instruction"}
    allowed_refs.update(ref for ref in allowed_paper_refs if ref in spans)
    if not set(plan.evidence_refs) <= allowed_refs:
        issues.append(_issue("math_citation_out_of_scope"))
    try:
        _quotes(plan.evidence_refs, spans=spans, instruction=instruction, paper=paper)
    except ActionPlanError:
        issues.append(_issue("math_citation_invalid"))
    if len({component.id for component in plan.components}) != len(plan.components):
        issues.append(_issue("duplicate_component_id"))
    supplies = {supply.material_id: supply for supply in setup.initial_supplies}
    available_components: set[str] = set()
    for index, component in enumerate(plan.components):
        refs = set(component.evidence_refs)
        if not refs <= allowed_refs:
            issues.append(_issue("component_citation_out_of_scope", index=index))
        else:
            try:
                _quotes(component.evidence_refs, spans=spans,
                        instruction=instruction, paper=paper)
            except ActionPlanError:
                issues.append(_issue("component_citation_invalid", index=index))
        if component.basis == "assumption":
            if not component.assumption_note:
                issues.append(_issue("assumption_reason_missing", index=index))
        if (len(component.source_material_ids) == 1 and
                component.source_usage != "single"):
            issues.append(_issue("single_source_usage_mismatch", index=index))
        if (len(component.source_material_ids) > 1 and
                component.source_usage == "single"):
            issues.append(_issue("grouped_source_usage_missing", index=index))
        if component.is_diluent and component.source_usage != "single":
            issues.append(_issue("grouped_diluent_unsupported", index=index))
        if component.phase == "premix":
            if (component.delivery != "robot" or
                    len(component.source_material_ids) != 1 or
                    component.source_usage != "single" or
                    not component.source_labware or not component.source_well):
                issues.append(_issue("premix_source_missing", index=index))
                continue
            material_id = component.source_material_ids[0]
            if component.id != material_id:
                issues.append(_issue("premix_component_material_id_mismatch",
                                     index=index))
            supply = supplies.get(material_id)
            load = next((item for item in setup.labware
                         if item.id == component.source_labware), None)
            if (supply is None or load is None or supply.labware != load.id or
                    load.load_name not in labware_catalog or
                    component.source_well not in _selected_wells(
                        supply, labware_catalog[load.load_name],
                    )):
                issues.append(_issue("premix_source_not_in_setup", index=index))
            elif not refs.intersection(supply.evidence_refs):
                issues.append(_issue("premix_source_citation_missing", index=index))
            else:
                available_components.add(component.id)
        elif component.source_labware is not None or component.source_well is not None:
            issues.append(_issue("later_source_well_must_be_planned_in_action_stage",
                                 index=index))
        elif component.delivery == "robot":
            if (not component.source_material_ids or
                    set(component.source_material_ids) - supplies.keys()):
                issues.append(_issue("later_source_not_in_setup", index=index))
            else:
                available_components.add(component.id)
        if component.basis == "direct" and component.volume_ul_per_reaction is not None:
            # Direct means the source itself states this µL amount. A computed
            # dilution or a chosen method must use its respective basis.
            quotes = [
                (instruction if spans[ref].source == "instruction" else paper or "")[
                    spans[ref].start:spans[ref].end
                ] for ref in refs if ref in spans
            ]
            if not any(
                any(math.isclose(float(value), component.volume_ul_per_reaction,
                                 abs_tol=0.01)
                    for value in re.findall(
                        r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:µL|μL|uL)\b", quote,
                        re.I,
                    )) for quote in quotes
            ):
                issues.append(_issue("direct_volume_not_cited", index=index))
    premix = [item for item in plan.components if item.phase == "premix"]
    later = [item for item in plan.components if item.phase == "later"]
    if not premix:
        issues.append(_issue("premix_components_missing"))
    # A source's final-reaction sentence may name materials that the named
    # premix well explicitly excludes. Require those loaded materials in the
    # later group. This is a lexical completeness gate, not semantic proof.
    paper_span = spans.get(context.paper_ref)
    task_span = spans.get(context.task_ref)
    if paper_span is not None and task_span is not None and paper is not None:
        paper_quote = paper[paper_span.start:paper_span.end]
        task_quote = instruction[task_span.start:task_span.end]
        volume_match = re.search(
            r"\b(?:PCRs?|reactions?)\s+(?:were|was|are|is)\s+"
            r"(?:performed|run|prepared)\s+in\s+\d+(?:\.\d+)?\s*"
            r"(?:µL|μL|uL)\s+volumes?\b", paper_quote, re.I,
        )
        if volume_match is not None:
            sentence = re.split(r"(?<=[.!?])\s+", paper_quote[
                volume_match.start():
            ], maxsplit=1)[0]
            later_material_ids = {id_ for item in later
                                  for id_ in item.source_material_ids}
            for material_id in sorted(supplies):
                tokens = [token for token in re.findall(r"[A-Za-z]{4,}", material_id)
                          if token.lower() not in {"source", "stock", "reagent"}]
                if (tokens and any(
                    re.search(rf"\b{re.escape(token)}\b", sentence, re.I) and
                    not re.search(rf"\b{re.escape(token)}\b", task_quote, re.I)
                    for token in tokens
                ) and material_id not in later_material_ids):
                    issues.append(_issue("source_named_later_component_missing",
                                         material_id=material_id))
    if any(item.volume_ul_per_reaction is None for item in plan.components):
        issues.append(_issue("reaction_component_volume_unresolved"))
    else:
        def effective_volume(item: MathComponent) -> float:
            multiplicity = (len(item.source_material_ids)
                            if item.source_usage == "each" else 1)
            return (item.volume_ul_per_reaction or 0) * multiplicity

        premix_total = sum(effective_volume(item) for item in premix)
        later_total = sum(effective_volume(item) for item in later)
        if not math.isclose(premix_total, plan.premix_target_ul_per_reaction,
                            abs_tol=0.01):
            issues.append(_issue("premix_component_sum_mismatch",
                                 expected=plan.premix_target_ul_per_reaction,
                                 observed=round(premix_total, 6)))
        expected_premix = plan.final_volume_ul - later_total
        if expected_premix < -0.01 or not math.isclose(
            expected_premix, plan.premix_target_ul_per_reaction, abs_tol=0.01,
        ):
            issues.append(_issue("premix_not_final_minus_later",
                                 expected=round(expected_premix, 6),
                                 observed=plan.premix_target_ul_per_reaction))
    diluents = [item.id for item in plan.components if item.is_diluent]
    if len(diluents) > 1:
        issues.append(_issue("multiple_diluents"))
    additions: list[Addition] = []
    available_reaction_components: set[str] = set()
    for component in plan.components:
        names = (component.source_material_ids if component.source_usage == "each"
                 else [component.id])
        for name in names:
            addition_id = name if component.source_usage == "each" else component.id
            additions.append(Addition(
                addition_id, component.volume_ul_per_reaction,
                stock_strength_x=component.stock_strength_x,
                target_strength_x=component.target_strength_x,
                is_diluent=component.is_diluent,
                delivery=component.delivery,
            ))
            if component.id in available_components:
                available_reaction_components.add(addition_id)
    reaction = Reaction(
        plan.stage_id, plan.final_volume_ul, tuple(additions),
        diluent_name=diluents[0] if len(diluents) == 1 else None,
    )
    issues.extend(_issue(issue.code, component=issue.component,
                         expected=issue.expected, observed=issue.observed)
                  for issue in audit_reaction(
                      reaction, available_components=available_reaction_components,
                  ))
    return plan, issues


def audit_observed_premix(
    plan: ReactionMathPlan, *, setup: PhasedSetup,
    events: Sequence[Mapping[str, Any]], event_labware: Mapping[str, str],
    start_event_index: int,
) -> list[dict[str, Any]]:
    """Compare per-stock recipe batches with exact pinned tip transfers."""
    if not 0 <= start_event_index <= len(events):
        return [_issue("invalid_stage_event_boundary")]
    loads = {item.id: item for item in setup.labware}

    def observed_name(id_: str) -> str | None:
        load = loads.get(id_)
        if load is None:
            return None
        label = load.label or load.id
        names = [name for name, load_name in event_labware.items()
                 if load_name == load.load_name and
                 (name == label or name.startswith(f"{label} on "))]
        return names[0] if len(names) == 1 else None

    target = observed_name(plan.premix_labware)
    if target is None:
        return [_issue("premix_event_labware_unresolved")]
    expected: dict[tuple[str, str], float] = defaultdict(float)
    for component in plan.components:
        if component.phase != "premix" or component.volume_ul_per_reaction is None:
            continue
        source = observed_name(component.source_labware or "")
        if source is None or component.source_well is None:
            return [_issue("premix_source_event_labware_unresolved")]
        expected[(source, component.source_well)] += (
            component.volume_ul_per_reaction * plan.reaction_count
        )
    held: dict[str, tuple[tuple[str, str] | None, float]] = {}
    instrument_channels: dict[str, int] = {}
    observed: dict[tuple[str, str], float] = defaultdict(float)
    for index, event in enumerate(events[start_event_index:], start_event_index):
        kind, instrument = event.get("kind"), event.get("instrument")
        if kind not in {"pick", "drop", "aspirate", "dispense"}:
            continue
        if not isinstance(instrument, str):
            return [_issue("premix_event_instrument_unresolved", event_index=index)]
        if kind == "pick":
            channel_count = event.get("channels", 1)
            if (isinstance(channel_count, bool) or not isinstance(channel_count, int)
                    or channel_count != 1):
                return [_issue("premix_multichannel_event_unresolved",
                               event_index=index)]
            instrument_channels[instrument] = channel_count
            held[instrument] = (None, 0.0)
            continue
        if kind == "drop":
            held.pop(instrument, None)
            instrument_channels.pop(instrument, None)
            continue
        if (instrument not in held or
                event.get("channels", instrument_channels.get(instrument, 1)) != 1 or
                not isinstance(event.get("labware"), str) or
                not isinstance(event.get("well"), str)):
            return [_issue("premix_event_pattern_unresolved", event_index=index)]
        location = (str(event["labware"]), str(event["well"]))
        volume = float(event.get("volume") or 0)
        source, amount = held[instrument]
        if kind == "aspirate":
            if amount > 0.01 and source != location:
                return [_issue("premix_mixed_source_tip_unresolved", event_index=index)]
            held[instrument] = (location, amount + volume)
        else:
            if source is None or amount + 0.01 < volume:
                return [_issue("premix_tip_liquid_unresolved", event_index=index)]
            if location == (target, plan.premix_well) and source != location:
                observed[source] += volume
            remaining = max(0.0, amount - volume)
            held[instrument] = (source if remaining > 0.01 else None, remaining)
    issues = []
    for source in sorted(set(expected) | set(observed)):
        if not math.isclose(expected.get(source, 0.0), observed.get(source, 0.0),
                            abs_tol=0.01):
            issues.append(_issue(
                "premix_source_volume_mismatch", source_labware=source[0],
                source_well=source[1], expected=round(expected.get(source, 0.0), 6),
                observed=round(observed.get(source, 0.0), 6),
            ))
    return issues
