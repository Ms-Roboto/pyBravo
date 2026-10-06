"""The local planner loads only instructions relevant to the source."""

from pybravo.workflow.protocols.agent_skills import selected_skills
from pybravo.workflow.protocols.ingest import ingest_text


def test_protocol_skills_are_selected_by_task():
    waiting = selected_skills(ingest_text("Wait 2 minutes."))
    assert [name for name, _ in waiting] == ["protocol-interpretation"]

    pipetting = selected_skills(ingest_text("Transfer 5 uL from the source plate using fresh tips."))
    assert [name for name, _ in pipetting] == [
        "protocol-interpretation", "deck-and-tips", "liquid-methods",
    ]
    assert all(body and "---" not in body for _, body in pipetting)
