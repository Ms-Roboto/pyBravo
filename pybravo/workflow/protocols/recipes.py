"""Versioned protocol composition hints; recipes never carry robot setpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .ingest import IngestedProtocol

_PATH = Path(__file__).resolve().parents[3] / "config" / "protocol_recipes.yaml"


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    status: str
    title: str
    triggers: list[str] = Field(default_factory=list)
    trigger_match: Literal["all", "any"] = "all"
    intent: str
    operations: list[str]
    source_path: str
    review_points: list[str] = Field(default_factory=list)


def recipe_catalog() -> dict:
    data = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    if data.get("version") != 1:
        raise ValueError("Unsupported protocol recipe catalog version")
    rows = [Recipe.model_validate(item).model_dump(mode="json") for item in data.get("recipes", [])]
    body = {"standard": "pybravo.protocol-recipes", "schema_version": "1.0.0", "recipes": rows}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {**body, "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


def relevant_recipe_hints(source: IngestedProtocol) -> list[dict]:
    text = " ".join(paragraph.text for paragraph in source.paragraphs).casefold()
    return [
        {key: recipe[key] for key in ("id", "version", "status", "title", "intent", "operations", "review_points")}
        for recipe in recipe_catalog()["recipes"]
        if recipe["triggers"] and (
            all(trigger.casefold() in text for trigger in recipe["triggers"])
            if recipe["trigger_match"] == "all"
            else any(trigger.casefold() in text for trigger in recipe["triggers"])
        )
    ]
