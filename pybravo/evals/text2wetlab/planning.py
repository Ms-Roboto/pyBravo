"""Evidence-anchored planning data for the OT-2 benchmark adapter.

Only the local model proposes experimental content. This module parses that
proposal, checks that its cited words occur in the task or supplied paper in
order, and audits arithmetic and module state. It neither writes nor runs
protocols. An anchored quotation is provenance, not proof that the model
interpreted it correctly; simulation and independent review remain necessary.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pybravo.evals.text2wetlab.reaction import Addition, Reaction, audit_reaction


class PlanParseError(ValueError):
    """A proposed plan has an invalid shape or unsupported citation."""


@dataclass(frozen=True)
class Evidence:
    source: Literal["instruction", "paper"]
    quote: str


@dataclass(frozen=True)
class DeckSource:
    id: str
    component: str
    labware: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class PlannedAddition:
    addition: Addition
    source_id: str | None
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class PlannedReaction:
    name: str
    final_volume_ul: float
    diluent_name: str | None
    additions: tuple[PlannedAddition, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class TipDemand:
    stage: str
    visits: int
    fresh_tips_per_visit: int
    shared_pickups: int
    evidence: tuple[Evidence, ...]
    stroke_volumes_ul: tuple[float, ...] = ()

    @property
    def pickups(self) -> int:
        return self.visits * self.fresh_tips_per_visit + self.shared_pickups


@dataclass(frozen=True)
class TipBudget:
    pipette: str
    channels: int
    rack_count: int
    tips_per_rack: int
    planned_resets: int
    refill_allowed: bool
    demands: tuple[TipDemand, ...]
    evidence: tuple[Evidence, ...]
    tiprack_load_name: str | None = None

    @property
    def pickups(self) -> int:
        return sum(demand.pickups for demand in self.demands)

    @property
    def pickups_per_load(self) -> int:
        return self.rack_count * (self.tips_per_rack // self.channels) if self.channels > 0 else 0


StageKind = Literal["pipette", "set_temperature", "set_lid", "set_magnet", "wait", "manual"]
ModuleType = Literal["thermocycler", "temperature", "magnetic"]


@dataclass(frozen=True)
class Stage:
    name: str
    kind: StageKind
    module_id: str | None
    module_type: ModuleType | None
    temperature_c: float | None
    lid_state: Literal["open", "closed"] | None
    magnet_state: Literal["engaged", "disengaged"] | None
    required_temperature_c: float | None
    required_lid_state: Literal["open", "closed"] | None
    required_magnet_state: Literal["engaged", "disengaged"] | None
    evidence: tuple[Evidence, ...]
    duration_s: float | None = None


@dataclass(frozen=True)
class OT2Plan:
    deck_sources: tuple[DeckSource, ...]
    reactions: tuple[PlannedReaction, ...]
    tip_budgets: tuple[TipBudget, ...]
    stages: tuple[Stage, ...]


@dataclass(frozen=True)
class PlanIssue:
    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _array(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


_EVIDENCE_SCHEMA = _object({
    "source": {"type": "string", "enum": ["instruction", "paper"]},
    "quote": {"type": "string"},
})
_EVIDENCE_LIST = _array(_EVIDENCE_SCHEMA)

PLAN_SCHEMA: dict[str, Any] = _object({
    "deck_sources": _array(_object({
        "id": {"type": "string"}, "component": {"type": "string"},
        "labware": {"type": "string"}, "evidence": _EVIDENCE_LIST,
    })),
    "reactions": _array(_object({
        "name": {"type": "string"}, "final_volume_ul": {"type": "number"},
        "diluent_name": _nullable("string"), "evidence": _EVIDENCE_LIST,
        "additions": _array(_object({
            "component": {"type": "string"}, "source_id": _nullable("string"),
            "volume_ul": _nullable("number"), "stock_strength_x": _nullable("number"),
            "target_strength_x": _nullable("number"), "is_diluent": {"type": "boolean"},
            "delivery": {"type": "string", "enum": ["robot", "manual"]},
            "evidence": _EVIDENCE_LIST,
        })),
    })),
    "tip_budgets": _array(_object({
        "pipette": {"type": "string"}, "channels": {"type": "integer"},
        "tiprack_load_name": _nullable("string"),
        "rack_count": {"type": "integer"}, "tips_per_rack": {"type": "integer"},
        "planned_resets": {"type": "integer"}, "refill_allowed": {"type": "boolean"},
        "evidence": _EVIDENCE_LIST,
        "demands": _array(_object({
            "stage": {"type": "string"}, "visits": {"type": "integer"},
            "fresh_tips_per_visit": {"type": "integer"},
            "shared_pickups": {"type": "integer"},
            "stroke_volumes_ul": _array({"type": "number"}), "evidence": _EVIDENCE_LIST,
        })),
    })),
    "stages": _array(_object({
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["pipette", "set_temperature", "set_lid",
                                            "set_magnet", "wait", "manual"]},
        "module_id": _nullable("string"),
        "module_type": {"type": ["string", "null"],
                        "enum": ["thermocycler", "temperature", "magnetic", None]},
        "temperature_c": _nullable("number"),
        "lid_state": {"type": ["string", "null"], "enum": ["open", "closed", None]},
        "magnet_state": {"type": ["string", "null"], "enum": ["engaged", "disengaged", None]},
        "required_temperature_c": _nullable("number"),
        "required_lid_state": {"type": ["string", "null"], "enum": ["open", "closed", None]},
        "required_magnet_state": {"type": ["string", "null"],
                                  "enum": ["engaged", "disengaged", None]},
        "duration_s": _nullable("number"),
        "evidence": _EVIDENCE_LIST,
    })),
})

PLAN_SYSTEM_PROMPT = """Extract a typed OT-2 experimental plan, not Python code. Return exactly the JSON schema. Use only the benchmark instruction and supplied paper text as evidence; never use a public reference protocol, solution, or hidden test. Every claim-bearing row needs a short source quote. Copy its words in order; omit Markdown backticks if needed. If joining two nonadjacent excerpts, separate them with `...` and keep each excerpt exact. Do not paraphrase a quote. List deck_sources only when the instruction's starting inventory/deck says the component is physically available, not merely because a paper recipe mentions it. Do not invent a reagent, source labware, stock concentration, temperature, or refill permission. Use null when a value is unknown and an empty array when a category does not apply. Include every distinct reaction and ordered stage needed by the task, preserving stated times as duration_s. A reaction lists all components per final vessel, including diluent, primer, template, and stock concentration when stated. For each pipette, use its exact task model and tip-rack load name, then divide tip demand into stages: visits is the number of distinct targets/columns handled; fresh_tips_per_visit is the number of new tips needed at each visit; shared_pickups counts source-only tips that safely cover multiple empty destinations; stroke_volumes_ul lists every distinct per-aspiration/dispense volume in that stage, not the total over all targets. A multichannel pickup consumes one tip per channel. planned_resets is how many full rack reload cycles the plan needs; allow refills only when the instruction authorizes them and cite that authorization. For module stages, record state-setting actions in their actual order and state requirements on subsequent pipetting stages. Keep assumptions explicit in names or evidence; do not silently complete missing task facts."""


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(value.split())


def _citation_tokens(value: str) -> list[str]:
    value = unicodedata.normalize("NFKC", value).casefold().replace("×", "x")
    return re.findall(r"[\wμ]+", value, re.UNICODE)


def _quote_is_grounded(quote: str, corpus: str, *, max_elision_words: int = 120) -> bool:
    """Accept markup-normalized quotes and bounded, ordered `...` elisions.

    Every substantive fragment must be a contiguous word sequence in the
    source. Bounded gaps allow a model to cite a sentence split by an omitted
    clause without accepting an arbitrary collage of distant source facts.
    """
    fragments = [_citation_tokens(piece) for piece in re.split(r"\.{3,}|…", quote)]
    fragments = [fragment for fragment in fragments if fragment]
    if sum(map(len, fragments)) < 3:
        return False
    source_words = _citation_tokens(corpus)
    previous_end = 0
    for fragment in fragments:
        found = False
        for start in range(previous_end, len(source_words) - len(fragment) + 1):
            if previous_end and start - previous_end > max_elision_words:
                break
            if source_words[start:start + len(fragment)] == fragment:
                previous_end = start + len(fragment)
                found = True
                break
        if not found:
            return False
    return True


def _inventory_text(instruction: str) -> str:
    match = re.search(r"^##\s+What is in the labware at the start\s*$", instruction, re.MULTILINE | re.IGNORECASE)
    if match is None:
        return instruction
    remainder = instruction[match.end():]
    next_section = re.search(r"^##\s+", remainder, re.MULTILINE)
    return remainder[:next_section.start()] if next_section else remainder


def _component_tokens(value: str) -> set[str]:
    """Small lexical guard against citing one stock as a different stock."""
    tokens = set(re.findall(r"[a-z0-9]+", _normalized(value))) - {
        "a", "and", "at", "each", "for", "in", "of", "the", "well",
    }
    distinctive = tokens - {
        "buffer", "component", "liquid", "master", "mix", "reaction", "reagent",
        "sample", "solution", "stock",
    }
    return distinctive or tokens


def _mapping(value: Any, path: str, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PlanParseError(f"{path} must contain exactly {', '.join(sorted(keys))}.")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise PlanParseError(f"{path} must be an array.")
    return value


def _string(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PlanParseError(f"{path} must be a nonempty string.")
    return value.strip()


def _number(value: Any, path: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanParseError(f"{path} must be a finite number.")
    return float(value)


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanParseError(f"{path} must be a nonnegative integer.")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise PlanParseError(f"{path} must be a boolean.")
    return value


def _choice(value: Any, path: str, choices: set[str], *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in choices:
        raise PlanParseError(f"{path} must be one of {', '.join(sorted(choices))}.")
    return value


def _evidence(
    raw: Any, path: str, *, instruction: str, scientific_source: str | None,
    inventory_only: bool = False,
) -> tuple[Evidence, ...]:
    rows = _sequence(raw, path)
    if not rows:
        raise PlanParseError(f"{path} requires at least one source quote.")
    result: list[Evidence] = []
    for index, row in enumerate(rows):
        item = _mapping(row, f"{path}[{index}]", {"source", "quote"})
        source = _choice(item["source"], f"{path}[{index}].source", {"instruction", "paper"})
        quote = _string(item["quote"], f"{path}[{index}].quote")
        assert isinstance(source, str) and isinstance(quote, str)
        corpus = instruction if source == "instruction" else scientific_source
        if corpus is None or not _quote_is_grounded(quote, corpus):
            raise PlanParseError(f"{path}[{index}] quote is absent from its declared source.")
        if inventory_only and (source != "instruction" or
                               not _quote_is_grounded(quote, _inventory_text(instruction))):
            raise PlanParseError(f"{path}[{index}] must quote the task's on-deck inventory.")
        result.append(Evidence(source, quote))
    return tuple(result)


def parse_plan(
    payload: Mapping[str, Any], *, instruction: str, scientific_source: str | None = None,
) -> OT2Plan:
    """Parse model JSON and anchor every plan claim to supplied text.

    `deck_sources` must cite the task's starting inventory. This prevents a
    paper-only reagent from being silently treated as on-deck supply.
    """
    root = _mapping(payload, "plan", {"deck_sources", "reactions", "tip_budgets", "stages"})
    sources: list[DeckSource] = []
    for index, raw in enumerate(_sequence(root["deck_sources"], "deck_sources")):
        path = f"deck_sources[{index}]"
        row = _mapping(raw, path, {"id", "component", "labware", "evidence"})
        evidence = _evidence(row["evidence"], path + ".evidence", instruction=instruction,
                             scientific_source=scientific_source, inventory_only=True)
        component = _string(row["component"], path + ".component")
        labware = _string(row["labware"], path + ".labware")
        assert isinstance(component, str) and isinstance(labware, str)
        if not any(_component_tokens(component) & _component_tokens(item.quote) for item in evidence):
            raise PlanParseError(f"{path} inventory quote does not name its claimed component {component!r}.")
        if _normalized(labware) not in _normalized(instruction):
            raise PlanParseError(f"{path} labware {labware!r} is absent from the task instruction.")
        sources.append(DeckSource(
            id=_string(row["id"], path + ".id"),
            component=component, labware=labware, evidence=evidence,
        ))
    reactions: list[PlannedReaction] = []
    for index, raw in enumerate(_sequence(root["reactions"], "reactions")):
        path = f"reactions[{index}]"
        row = _mapping(raw, path, {"name", "final_volume_ul", "diluent_name", "additions", "evidence"})
        additions: list[PlannedAddition] = []
        for addition_index, raw_addition in enumerate(_sequence(row["additions"], path + ".additions")):
            add_path = f"{path}.additions[{addition_index}]"
            item = _mapping(raw_addition, add_path, {
                "component", "source_id", "volume_ul", "stock_strength_x", "target_strength_x",
                "is_diluent", "delivery", "evidence",
            })
            additions.append(PlannedAddition(
                Addition(
                    component=_string(item["component"], add_path + ".component"),
                    volume_ul=_number(item["volume_ul"], add_path + ".volume_ul", nullable=True),
                    stock_strength_x=_number(item["stock_strength_x"], add_path + ".stock_strength_x",
                                             nullable=True),
                    target_strength_x=_number(item["target_strength_x"], add_path + ".target_strength_x",
                                              nullable=True),
                    is_diluent=_boolean(item["is_diluent"], add_path + ".is_diluent"),
                    delivery=_choice(item["delivery"], add_path + ".delivery", {"robot", "manual"}),
                ),
                source_id=_string(item["source_id"], add_path + ".source_id", nullable=True),
                evidence=_evidence(item["evidence"], add_path + ".evidence", instruction=instruction,
                                   scientific_source=scientific_source),
            ))
        reactions.append(PlannedReaction(
            name=_string(row["name"], path + ".name"),
            final_volume_ul=_number(row["final_volume_ul"], path + ".final_volume_ul"),
            diluent_name=_string(row["diluent_name"], path + ".diluent_name", nullable=True),
            additions=tuple(additions),
            evidence=_evidence(row["evidence"], path + ".evidence", instruction=instruction,
                               scientific_source=scientific_source),
        ))
    budgets: list[TipBudget] = []
    for index, raw in enumerate(_sequence(root["tip_budgets"], "tip_budgets")):
        path = f"tip_budgets[{index}]"
        row = _mapping(raw, path, {"pipette", "channels", "tiprack_load_name", "rack_count", "tips_per_rack",
                                   "planned_resets", "refill_allowed", "demands", "evidence"})
        demands: list[TipDemand] = []
        for demand_index, raw_demand in enumerate(_sequence(row["demands"], path + ".demands")):
            demand_path = f"{path}.demands[{demand_index}]"
            item = _mapping(raw_demand, demand_path, {"stage", "visits", "fresh_tips_per_visit",
                                                      "shared_pickups", "stroke_volumes_ul", "evidence"})
            demands.append(TipDemand(
                stage=_string(item["stage"], demand_path + ".stage"),
                visits=_integer(item["visits"], demand_path + ".visits"),
                fresh_tips_per_visit=_integer(item["fresh_tips_per_visit"],
                                              demand_path + ".fresh_tips_per_visit"),
                shared_pickups=_integer(item["shared_pickups"], demand_path + ".shared_pickups"),
                evidence=_evidence(item["evidence"], demand_path + ".evidence", instruction=instruction,
                                   scientific_source=scientific_source),
                stroke_volumes_ul=tuple(
                    _number(value, f"{demand_path}.stroke_volumes_ul[{volume_index}]")
                    for volume_index, value in enumerate(_sequence(item["stroke_volumes_ul"],
                                                                   demand_path + ".stroke_volumes_ul"))
                ),
            ))
        pipette = _string(row["pipette"], path + ".pipette")
        tiprack = _string(row["tiprack_load_name"], path + ".tiprack_load_name", nullable=True)
        assert isinstance(pipette, str)
        if _normalized(pipette) not in _normalized(instruction):
            raise PlanParseError(f"{path} pipette {pipette!r} is absent from the task instruction.")
        if tiprack is not None and _normalized(tiprack) not in _normalized(instruction):
            raise PlanParseError(f"{path} tip rack {tiprack!r} is absent from the task instruction.")
        refill_allowed = _boolean(row["refill_allowed"], path + ".refill_allowed")
        evidence = _evidence(row["evidence"], path + ".evidence", instruction=instruction,
                             scientific_source=scientific_source)
        if refill_allowed and not any(re.search(r"reset_tipracks|unlimited|refill|reload|replenish",
                                             item.quote, re.IGNORECASE)
                                      for item in evidence if item.source == "instruction"):
            raise PlanParseError(f"{path} claims refills without a cited task authorization.")
        budgets.append(TipBudget(
            pipette=pipette,
            channels=_integer(row["channels"], path + ".channels"),
            rack_count=_integer(row["rack_count"], path + ".rack_count"),
            tips_per_rack=_integer(row["tips_per_rack"], path + ".tips_per_rack"),
            planned_resets=_integer(row["planned_resets"], path + ".planned_resets"),
            refill_allowed=refill_allowed,
            demands=tuple(demands),
            evidence=evidence, tiprack_load_name=tiprack,
        ))
    stages: list[Stage] = []
    for index, raw in enumerate(_sequence(root["stages"], "stages")):
        path = f"stages[{index}]"
        row = _mapping(raw, path, {"name", "kind", "module_id", "module_type", "temperature_c",
                                   "lid_state", "magnet_state", "required_temperature_c",
                                   "required_lid_state", "required_magnet_state", "duration_s", "evidence"})
        stages.append(Stage(
            name=_string(row["name"], path + ".name"),
            kind=_choice(row["kind"], path + ".kind",
                         {"pipette", "set_temperature", "set_lid", "set_magnet", "wait", "manual"}),
            module_id=_string(row["module_id"], path + ".module_id", nullable=True),
            module_type=_choice(row["module_type"], path + ".module_type",
                                {"thermocycler", "temperature", "magnetic"}, nullable=True),
            temperature_c=_number(row["temperature_c"], path + ".temperature_c", nullable=True),
            lid_state=_choice(row["lid_state"], path + ".lid_state", {"open", "closed"}, nullable=True),
            magnet_state=_choice(row["magnet_state"], path + ".magnet_state",
                                 {"engaged", "disengaged"}, nullable=True),
            required_temperature_c=_number(row["required_temperature_c"],
                                           path + ".required_temperature_c", nullable=True),
            required_lid_state=_choice(row["required_lid_state"], path + ".required_lid_state",
                                       {"open", "closed"}, nullable=True),
            required_magnet_state=_choice(row["required_magnet_state"], path + ".required_magnet_state",
                                          {"engaged", "disengaged"}, nullable=True),
            evidence=_evidence(row["evidence"], path + ".evidence", instruction=instruction,
                               scientific_source=scientific_source),
            duration_s=_number(row["duration_s"], path + ".duration_s", nullable=True),
        ))
    return OT2Plan(tuple(sources), tuple(reactions), tuple(budgets), tuple(stages))


def audit_plan(
    plan: OT2Plan, *, geometry: Mapping[str, Mapping[str, Any]] | None = None,
    temperature_tolerance_c: float = 0.5,
) -> tuple[PlanIssue, ...]:
    """Audit grounded plan arithmetic, tip supply, and module transitions."""
    if not math.isfinite(temperature_tolerance_c) or temperature_tolerance_c < 0:
        raise ValueError("temperature_tolerance_c must be finite and nonnegative")
    issues: list[PlanIssue] = []
    if not plan.stages:
        issues.append(PlanIssue("empty_stage_plan", "stages",
                                "The plan has no ordered OT-2 or manual workflow stages."))
    if any(stage.kind == "pipette" for stage in plan.stages) and not plan.tip_budgets:
        issues.append(PlanIssue("missing_tip_budget", "tip_budgets",
                                "The plan includes pipetting but no pipette tip budget."))
    seen_stage_names: set[str] = set()
    for index, stage in enumerate(plan.stages):
        if stage.name in seen_stage_names:
            issues.append(PlanIssue("duplicate_stage_name", f"stages[{index}]",
                                    f"Stage name {stage.name!r} is repeated."))
        seen_stage_names.add(stage.name)
    source_ids: dict[str, DeckSource] = {}
    for index, source in enumerate(plan.deck_sources):
        if source.id in source_ids:
            issues.append(PlanIssue("duplicate_source_id", f"deck_sources[{index}]",
                                    f"Deck source {source.id} is declared twice."))
        source_ids[source.id] = source
    for index, reaction in enumerate(plan.reactions):
        path = f"reactions[{index}]"
        if not reaction.additions:
            issues.append(PlanIssue("empty_reaction", path,
                                    f"Reaction {reaction.name} has no component additions."))
        checked = Reaction(reaction.name, reaction.final_volume_ul,
                           tuple(item.addition for item in reaction.additions), reaction.diluent_name)
        for finding in audit_reaction(checked):
            issues.append(PlanIssue(finding.code, path, finding.message))
        for add_index, item in enumerate(reaction.additions):
            add_path = f"{path}.additions[{add_index}]"
            if item.addition.delivery == "robot" and item.source_id not in source_ids:
                issues.append(PlanIssue("missing_deck_source", add_path,
                                        f"{item.addition.component} has no cited on-deck source."))
            elif item.addition.delivery == "manual" and item.source_id is not None:
                issues.append(PlanIssue("manual_source_conflict", add_path,
                                        "A manual addition should not claim a robot deck source."))
            elif item.addition.delivery == "manual":
                issues.append(PlanIssue("manual_addition_required", add_path,
                                        f"{item.addition.component} must be added manually; the OT-2 code "
                                        "should record an explicit operator step.", "warning"))
            elif item.source_id in source_ids:
                source = source_ids[item.source_id]
                wanted = _component_tokens(item.addition.component)
                available = _component_tokens(source.component)
                if wanted.isdisjoint(available):
                    issues.append(PlanIssue("source_component_mismatch", add_path,
                                            f"{item.addition.component} is assigned to source {source.id}, "
                                            f"which contains {source.component}."))
    budget_names: set[str] = set()
    stage_names = {stage.name for stage in plan.stages}
    for index, budget in enumerate(plan.tip_budgets):
        path = f"tip_budgets[{index}]"
        if budget.pipette in budget_names:
            issues.append(PlanIssue("duplicate_tip_budget", path,
                                    f"Pipette {budget.pipette} has multiple budgets."))
        budget_names.add(budget.pipette)
        if not 1 <= budget.channels <= 8 or budget.tips_per_rack < budget.channels or budget.rack_count < 1:
            issues.append(PlanIssue("invalid_tip_capacity", path,
                                    "Channels, rack count, and rack positions do not form a usable tip supply."))
            continue
        if geometry is not None and budget.tiprack_load_name in geometry:
            rack_geometry = geometry[budget.tiprack_load_name]
            if not rack_geometry.get("is_tiprack") or rack_geometry.get("well_count") != budget.tips_per_rack:
                issues.append(PlanIssue("tiprack_geometry_mismatch", path,
                                        f"{budget.tiprack_load_name} does not provide the planned "
                                        f"{budget.tips_per_rack} tip positions per rack."))
        model = re.search(r"p(20|300|1000)(?:_|\b)", budget.pipette.casefold())
        working_range = {
            "20": (1.0, 20.0), "300": (20.0, 300.0), "1000": (100.0, 1000.0),
        }.get(model.group(1)) if model else None
        tip_capacity = re.search(r"_(\d+)ul\b", budget.tiprack_load_name or "", re.IGNORECASE)
        if working_range is not None and tip_capacity is not None:
            working_range = (working_range[0], min(working_range[1], float(tip_capacity.group(1))))
        for demand_index, demand in enumerate(budget.demands):
            if demand.stage not in stage_names:
                issues.append(PlanIssue("unknown_tip_stage", f"{path}.demands[{demand_index}]",
                                        f"Tip demand refers to absent stage {demand.stage}."))
            demand_path = f"{path}.demands[{demand_index}]"
            if demand.pickups and not demand.stroke_volumes_ul:
                issues.append(PlanIssue("missing_stroke_volume", demand_path,
                                        f"{demand.stage} has tip pickups but no planned stroke volumes.", "warning"))
            for volume in demand.stroke_volumes_ul:
                if not math.isfinite(volume) or volume <= 0:
                    issues.append(PlanIssue("invalid_stroke_volume", demand_path,
                                            f"{demand.stage} has invalid stroke volume {volume}."))
                elif working_range is not None and not working_range[0] <= volume <= working_range[1]:
                    issues.append(PlanIssue("pipette_range_mismatch", demand_path,
                                            f"{budget.pipette} plans {volume:g} µL in {demand.stage}, "
                                            f"outside its {working_range[0]:g}–{working_range[1]:g} µL "
                                            "working range with the named tip rack."))
        available = budget.pickups_per_load
        if any(demand.visits for demand in budget.demands) and budget.pickups == 0:
            issues.append(PlanIssue("zero_tip_budget", path,
                                    f"{budget.pipette} plans liquid visits but no tip pickup."))
        required_resets = max(0, math.ceil(budget.pickups / available) - 1)
        if budget.planned_resets < required_resets:
            issues.append(PlanIssue("insufficient_tips", path,
                                    f"{budget.pipette} needs {budget.pickups} pickups of {budget.channels} tips; "
                                    f"the loaded racks permit {available} pickups per load and need at least "
                                    f"{required_resets} reset(s), but the plan has {budget.planned_resets}."))
        if budget.planned_resets and not budget.refill_allowed:
            issues.append(PlanIssue("unapproved_tip_refill", path,
                                    f"{budget.pipette} plans a rack reset without task authorization."))
        if budget.planned_resets > required_resets:
            issues.append(PlanIssue("premature_tip_reset", path,
                                    f"{budget.pipette} plans {budget.planned_resets} resets but only "
                                    f"{required_resets} full-rack reset(s) are needed.", "warning"))
    module_types: dict[str, ModuleType] = {}
    module_states: dict[str, dict[str, str | float | None]] = {}
    for index, stage in enumerate(plan.stages):
        path = f"stages[{index}]"
        if stage.duration_s is not None and stage.duration_s < 0:
            issues.append(PlanIssue("invalid_stage_duration", path,
                                    f"{stage.name} has a negative duration."))
        if stage.module_id is None:
            if stage.kind in {"set_temperature", "set_lid", "set_magnet"}:
                issues.append(PlanIssue("missing_module", path,
                                        f"{stage.kind} needs a module identifier."))
            continue
        if stage.module_type is None:
            issues.append(PlanIssue("missing_module_type", path,
                                    f"Module {stage.module_id} has no type."))
            continue
        old_type = module_types.setdefault(stage.module_id, stage.module_type)
        if old_type != stage.module_type:
            issues.append(PlanIssue("module_type_conflict", path,
                                    f"Module {stage.module_id} changes from {old_type} to {stage.module_type}."))
            continue
        state = module_states.setdefault(stage.module_id, {
            "temperature": None, "lid": "open" if stage.module_type == "thermocycler" else None,
            "magnet": "disengaged" if stage.module_type == "magnetic" else None,
        })
        if stage.kind == "set_temperature":
            if stage.temperature_c is None:
                issues.append(PlanIssue("missing_temperature", path,
                                        "Temperature-setting stage has no temperature."))
            else:
                state["temperature"] = stage.temperature_c
        elif stage.kind == "set_lid":
            if stage.module_type != "thermocycler" or stage.lid_state is None:
                issues.append(PlanIssue("invalid_lid_action", path,
                                        "Lid action needs a thermocycler and open/closed state."))
            else:
                state["lid"] = stage.lid_state
        elif stage.kind == "set_magnet":
            if stage.module_type != "magnetic" or stage.magnet_state is None:
                issues.append(PlanIssue("invalid_magnet_action", path,
                                        "Magnet action needs a magnetic module and engaged/disengaged state."))
            else:
                state["magnet"] = stage.magnet_state
        if stage.kind != "pipette":
            continue
        if stage.module_type == "thermocycler" and state["lid"] == "closed":
            issues.append(PlanIssue("closed_lid_pipetting", path,
                                    f"Pipetting into {stage.module_id} is planned while its lid is closed."))
        requirements: tuple[tuple[str, str | float | None], ...] = (
            ("temperature", stage.required_temperature_c),
            ("lid", stage.required_lid_state),
            ("magnet", stage.required_magnet_state),
        )
        for key, required in requirements:
            if required is None:
                continue
            actual = state[key]
            if key == "temperature":
                matches = (isinstance(actual, (int, float))
                           and abs(actual - required) <= temperature_tolerance_c)
            else:
                matches = actual == required
            if not matches:
                issues.append(PlanIssue("module_state_mismatch", path,
                                        f"{stage.name} requires {stage.module_id} {key}={required}; "
                                        f"current planned state is {actual}."))
    return tuple(issues)


def plan_to_prompt(plan: OT2Plan) -> str:
    """Render concise, model-authored data for a later local code-generation call."""
    payload = {
        "deck_sources": [{"id": item.id, "component": item.component, "labware": item.labware}
                         for item in plan.deck_sources],
        "reactions": [{
            "name": item.name, "final_volume_ul": item.final_volume_ul,
            "diluent_name": item.diluent_name,
            "additions": [{"component": added.addition.component,
                           "volume_ul": added.addition.volume_ul, "source_id": added.source_id,
                           "stock_strength_x": added.addition.stock_strength_x,
                           "target_strength_x": added.addition.target_strength_x,
                           "delivery": added.addition.delivery}
                          for added in item.additions],
        } for item in plan.reactions],
        "tip_budgets": [{"pipette": item.pipette, "channels": item.channels,
                         "tiprack_load_name": item.tiprack_load_name,
                         "pickups": item.pickups, "pickups_per_load": item.pickups_per_load,
                         "planned_resets": item.planned_resets,
                         "demands": [{"stage": demand.stage, "visits": demand.visits,
                                      "fresh_tips_per_visit": demand.fresh_tips_per_visit,
                                      "shared_pickups": demand.shared_pickups,
                                      "stroke_volumes_ul": demand.stroke_volumes_ul}
                                     for demand in item.demands]} for item in plan.tip_budgets],
        "stages": [{"name": item.name, "kind": item.kind, "module_id": item.module_id,
                    "module_type": item.module_type, "duration_s": item.duration_s,
                    "temperature_c": item.temperature_c, "lid_state": item.lid_state,
                    "magnet_state": item.magnet_state,
                    "required_temperature_c": item.required_temperature_c,
                    "required_lid_state": item.required_lid_state,
                    "required_magnet_state": item.required_magnet_state} for item in plan.stages],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
