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
