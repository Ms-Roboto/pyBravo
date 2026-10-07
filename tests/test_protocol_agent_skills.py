"""The local planner loads only instructions relevant to the source."""

from pybravo.workflow.protocols.agent_skills import selected_skills
from pybravo.workflow.protocols.ingest import ingest_text
from pybravo.workflow.protocols.llm import _EXTRACTION_PROMPT


def test_protocol_skills_are_selected_by_task():
    waiting = selected_skills(ingest_text("Wait 2 minutes."))
    assert [name for name, _ in waiting] == ["protocol-interpretation"]

    pipetting = selected_skills(ingest_text("Transfer 5 uL from the source plate using fresh tips."))
    assert [name for name, _ in pipetting] == [
        "protocol-interpretation", "deck-and-tips", "liquid-methods",
    ]
    assert all(body and "---" not in body for _, body in pipetting)

    solvent = selected_skills(ingest_text("The solvent is DMSO."))
    assert "liquid-methods" in dict(solvent)

    plural_hardware = selected_skills(ingest_text("Place four plates and four tipboxes on the deck."))
    assert [name for name, _ in plural_hardware] == ["protocol-interpretation", "deck-and-tips"]


def test_quadrant_skills_keep_tip_reuse_distinct_from_aspiration():
    skills = dict(selected_skills(ingest_text(
        "Transfer 5 uL from each of four 384 Labcyte PP source plates into the "
        "quadrants of two 1536 Labcyte LDV destination plates using ST10 tips."
    )))
    assert "at least 10 µL usable volume per source well" in skills["protocol-interpretation"]
    assert "Tip reuse and shared aspiration are separate decisions" in skills["liquid-methods"]
    assert "one 10 µL aspiration" in skills["liquid-methods"]
    assert "one distinct ST10 rack per source" in skills["384-to-1536-quadrants"]
    assert "source 1→A1, 2→A2, 3→B1, 4→B2" in skills["384-to-1536-quadrants"]
    assert "Each sample well is its own contamination domain" in skills["deck-and-tips"]


def test_quadrant_geometry_is_not_sent_for_unrelated_protocols():
    skills = dict(selected_skills(ingest_text("Transfer 100 uL from reservoir to plate wells A1 through A12.")))
    assert "384-to-1536-quadrants" not in skills
    assert "four source plates into two" not in _EXTRACTION_PROMPT.lower()
    assert "source 1→A1" not in skills["deck-and-tips"]
