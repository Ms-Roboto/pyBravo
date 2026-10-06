from pybravo.workflow.protocols.evaluation import summarize_sessions


def test_summary_keeps_explicit_agreement_separate_from_top_rank_selection():
    sessions = [{
        "plan": {
            "steps": [{"kind": "transfer", "method_ref": {"method_id": "water", "revision": "rev-1"}}],
            "decisions": [{"path": "/steps/0/method_selection", "value": {
                "selected": {"method_id": "water", "revision": "rev-1"},
                "offered": [{"method_id": "water", "revision": "rev-1", "rank": 1,
                             "match_kind": "closest"}],
            }}],
        },
        "validation": {"questions": [{"id": "q1"}], "issues": [{"severity": "warning"}]},
        "simulation": {"status": "passed"},
        "approval": {"qualification": "scientist_reviewed_simulated"},
        "history": [{"event": "scientist_edit"}],
    }]

    result = summarize_sessions(sessions)
    assert result["sessions_with_pinned_methods"] == 1
    assert result["top_candidate_selections"] == 1
    assert result["closest_candidate_selections"] == 1
    assert result["explicit_expert_agreement"] == {"unscored": 1}
    assert result["scientist_edit_revisions"] == 1
    assert result["strict_simulation_status"] == {"passed": 1}
    assert result["unresolved_questions"] == 1
    assert result["validation_error_issues"] == 0


def test_new_reviewed_method_is_counted_as_curation_not_top_candidate_acceptance():
    result = summarize_sessions([{"plan": {"decisions": [{
        "path": "/steps/0/method_selection",
        "value": {
            "selected": {"method_id": "new-reviewed", "revision": "rev-new"},
            "offered": [{"method_id": "imported:water", "revision": "rev-old", "rank": 1}],
        },
    }]}}])
    assert result["method_selection_events"] == 1
    assert result["curated_method_selections"] == 1
    assert result["top_candidate_selections"] == 0


def test_bulk_method_proposal_is_not_expert_agreement():
    result = summarize_sessions([{"plan": {"decisions": [{
        "path": "/steps/0/method_selection",
        "value": {
            "selected": {"method_id": "water", "revision": "rev"},
            "offered": [{"method_id": "water", "revision": "rev", "rank": 1}],
            "selection_mode": "bulk_proposal",
        },
    }]}}])
    assert result["bulk_method_proposals"] == 1
    assert result["top_candidate_selections"] == 0
