"""Localized model repairs must apply only to the rejected source coordinates."""

import hashlib

import pytest

from pybravo.evals.text2wetlab.patch_repair import (
    PatchError,
    apply_line_patch,
    line_patch_messages,
    numbered_source,
)


def test_numbered_source_and_prompt_keep_task_and_candidate_distinct():
    source = "first()\nsecond()\n"
    messages = line_patch_messages(source, instruction="Move 5 µL into A1.",
                                   diagnostic="Line 2 failed.")
    assert numbered_source(source) == "0001| first()\n0002| second()"
    assert "Move 5 µL" in messages[1]["content"]
    assert hashlib.sha256(source.encode()).hexdigest() in messages[2]["content"]
    assert "0002| second()" in messages[2]["content"]


def test_single_line_replacement_keeps_every_other_line():
    source = "before()\nwrong()\nafter()\n"
    patched = apply_line_patch(source, {
        "edits": [{"start_line": 2, "end_line": 2, "replacement": "right()"}],
    })
    assert patched == "before()\nright()\nafter()\n"


def test_insertion_and_append_use_original_line_coordinates():
    source = "one()\ntwo()\n"
    patched = apply_line_patch(source, {"edits": [
        {"start_line": 3, "end_line": 2, "replacement": "three()"},
        {"start_line": 1, "end_line": 0, "replacement": "zero()"},
    ]})
    assert patched == "zero()\none()\ntwo()\nthree()\n"


@pytest.mark.parametrize("edits", [
    [],
    [{"start_line": 0, "end_line": 0, "replacement": "x"}],
    [{"start_line": 4, "end_line": 3, "replacement": "x"}],
    [{"start_line": 1, "end_line": 3, "replacement": "x"}],
    [{"start_line": 1, "end_line": 1, "replacement": "x"},
     {"start_line": 2, "end_line": 1, "replacement": "y"}],
    [{"start_line": 2, "end_line": 1, "replacement": "x"},
     {"start_line": 2, "end_line": 1, "replacement": "y"}],
    [{"start_line": True, "end_line": 0, "replacement": "x"}],
    [{"start_line": 1, "end_line": 1, "replacement": "x", "extra": 1}],
])
def test_invalid_or_ambiguous_patches_fail_closed(edits):
    with pytest.raises(PatchError):
        apply_line_patch("one()\ntwo()\n", {"edits": edits})


def test_unmodified_protocol_is_not_a_successful_repair():
    with pytest.raises(PatchError):
        apply_line_patch("one()\n", {"edits": [
            {"start_line": 1, "end_line": 1, "replacement": "one()"},
        ]})
