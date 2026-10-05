"""Reviewable, non-executable representation of a scientist's protocol.

The model deliberately accepts unknown values as ``None``. Validation supplies
questions, and compilation is unavailable until they have been resolved. Catalog
geometry and hardware capabilities belong to server context, never model output.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NumericEvidence(StrictModel):
    field: Literal["volume_ul", "duration_s", "cycles", "repeat"]
    value: float
    unit: Literal["uL", "mL", "nL", "s", "min", "h", "count"]
    paragraph_id: str


class ScientistDecision(StrictModel):
    path: str
    value: JsonValue
    reason: str = Field(min_length=1)
    actor: Literal["scientist"] = "scientist"


class ProtocolQuestion(StrictModel):
    id: str
    path: str
    prompt: str


class ProtocolMaterial(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    role: Literal["liquid", "tips", "waste"] = "liquid"
    labware_id: str | None = None
    deck_slot: int | None = None
    initial_volume_ul: float | None = None
    dead_volume_ul: float | None = None
    well_volumes_ul: dict[str, float] = Field(default_factory=dict)
    available_tips: list[str] | None = None
    tip_definition_id: str | None = None


class ProtocolStep(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["transfer", "mix", "manual", "wait", "move_plate", "repeat"]
    description: str = ""
    source_paragraph_ids: list[str] = Field(default_factory=list)
    source_values: list[NumericEvidence] = Field(default_factory=list)
    source: str | None = None
    destination: str | None = None
    material: str | None = None
    source_anchor: str | None = None
    destination_anchor: str | None = None
    anchor: str | None = None
    volume_ul: float | None = None
    cycles: int | None = None
    duration_s: float | None = None
    destination_slot: int | None = None
    message: str | None = None
    repeat: int | None = 1
    steps: list[ProtocolStep] = Field(default_factory=list)


class ProtocolPlan(StrictModel):
    schema_version: Literal["1"] = "1"
    name: str = Field(min_length=1, max_length=300)
    description: str = ""
    materials: list[ProtocolMaterial] = Field(default_factory=list)
    steps: list[ProtocolStep] = Field(default_factory=list)
    decisions: list[ScientistDecision] = Field(default_factory=list)
    questions: list[ProtocolQuestion] = Field(default_factory=list)


class ProtocolHeadMode(StrictModel):
    subset_type: Literal["all_barrels", "row", "column", "rectangle", "single_barrel"]
    subset_config: Literal["back_left", "back_right", "front_left", "front_right"]
    row_count: int | None = None
    column_count: int | None = None


class ProtocolSetup(StrictModel):
    name: str = ""
    head_mode: ProtocolHeadMode | None = None
    tip_strategy: Literal["fresh_each_step", "reuse_all"] | None = None
    tip_reuse_reason: str | None = None
    tip_rack_ids: list[str] = Field(default_factory=list)
    tip_disposal_id: str | None = None
    liquid_class: str | None = None
    distance_from_bottom_mm: float | None = None
