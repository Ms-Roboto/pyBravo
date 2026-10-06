"""Reusable patterns are discoverable but carry no hardware qualification."""

from pybravo.workflow.protocols.ingest import ingest_text
from pybravo.workflow.protocols.recipes import recipe_catalog, relevant_recipe_hints


def test_recipe_catalog_and_relevance():
    catalog = recipe_catalog()
    assert catalog["digest"] and catalog["recipes"]
    assert all(row["status"] == "synthetic_pattern" for row in catalog["recipes"])
    assert relevant_recipe_hints(ingest_text("Wait for 2 minutes.")) == []
    ids = {row["id"] for row in relevant_recipe_hints(ingest_text(
        "Transfer four 384 plates into the quadrants of two 1536 plates."
    ))}
    assert "four_384_to_1536_quadrants" in ids
    assert "serial_dilution" in {row["id"] for row in relevant_recipe_hints(ingest_text(
        "Perform a serial dilution."
    ))}


def test_quadrant_recipe_exposes_source_isolation_and_capacity_decisions():
    hints = relevant_recipe_hints(ingest_text(
        "Transfer 5 uL from each of four 384 Labcyte PP plates into the quadrants "
        "of two 1536 Labcyte LDV plates without cross contamination."
    ))
    recipe = next(row for row in hints if row["id"] == "four_384_to_1536_quadrants")
    intent = recipe["intent"].lower()
    assert "four source-dedicated st10" in intent
    assert "source 1/a1, 2/a2, 3/b1, 4/b2" in intent
    assert "10 ul usable per well plus dead volume" in intent
    assert "nominal st10 capacity alone does not justify one 10 ul" in intent
    assert "separate aspirations" in intent
    assert {"physical_tip_inventory", "per_source_tip_reuse_policy",
            "shared_aspiration_effective_capacity"} <= set(recipe["review_points"])
