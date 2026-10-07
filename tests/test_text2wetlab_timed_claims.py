"""Generic timed-comment checks use only the supplied procedure as evidence."""

from pybravo.evals.text2wetlab.timed_claims import audit_timed_comments


def _program(stage: str) -> str:
    return ("def run(protocol):\n"
            "    protocol.comment('Incubate samples for 5 min on deck')\n"
            f"    {stage}\n"
            "    protocol.comment('Continue with the next step')\n")


def test_cited_comment_without_a_wait_is_a_repairable_error():
    instruction = "Incubate samples for 5 min on deck, then continue."
    findings = audit_timed_comments(_program("protocol.comment('Ready')"),
                                    instruction=instruction)
    assert len(findings) == 1
    assert findings[0].code == "timed_comment_without_action"
    assert findings[0].severity == "error"
    assert "Incubate samples for 5 min" in findings[0].message


def test_matching_delay_or_operator_pause_represents_the_stage():
    instruction = "Incubate samples for 5 min on deck, then continue."
    assert audit_timed_comments(_program("protocol.delay(minutes=5)"),
                                instruction=instruction) == ()
    assert audit_timed_comments(_program("protocol.delay(seconds=300)"),
                                instruction=instruction) == ()
    assert audit_timed_comments(_program("protocol.pause('Wait for incubation')"),
                                instruction=instruction) == ()
    assert audit_timed_comments(_program(
        "thermocycler.set_block_temperature(42, hold_time_minutes=5)"),
        instruction=instruction,
    ) == ()
    mismatch = audit_timed_comments(_program("protocol.delay(minutes=2)"),
                                    instruction=instruction)
    assert len(mismatch) == 1 and mismatch[0].severity == "error"


def test_dynamic_duration_is_reviewed_without_asserting_absent_wait():
    instruction = "Incubate samples for 5 min on deck, then continue."
    findings = audit_timed_comments(_program("protocol.delay(minutes=incubation_minutes)"),
                                    instruction=instruction)
    assert len(findings) == 1 and findings[0].severity == "warning"


def test_off_deck_or_explicit_no_wait_claim_is_not_flagged():
    off_deck = ("def run(protocol):\n"
                "    protocol.comment('Incubate off-deck for 5 min')\n")
    assert audit_timed_comments(off_deck, instruction="Incubate off-deck for 5 min") == ()
    no_wait = ("def run(protocol):\n"
               "    protocol.comment('Incubate 5 min; no robot wait is needed')\n")
    assert audit_timed_comments(no_wait,
                                instruction="Incubate 5 min; no robot wait is needed") == ()
    next_comment = ("def run(protocol):\n"
                    "    protocol.comment('Incubate samples for 5 min on deck')\n"
                    "    protocol.comment('No robot wait is needed')\n")
    assert audit_timed_comments(next_comment,
                                instruction="Incubate samples for 5 min on deck") == ()


def test_source_instruction_to_record_comment_only_is_review_not_error():
    instruction = ("(Not simulated, record with protocol.comment) "
                   "Incubate samples for 5 min on deck")
    findings = audit_timed_comments(_program("protocol.comment('Ready')"),
                                    instruction=instruction)
    assert len(findings) == 1 and findings[0].severity == "warning"
    assert instruction in findings[0].message


def test_unanchored_comment_does_not_invent_a_source_requirement():
    assert audit_timed_comments(_program("protocol.comment('Ready')"),
                                instruction="Transfer samples to a plate.") == ()
