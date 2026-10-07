"""Scientific source reduction keeps complete, verbatim Methods evidence."""

from __future__ import annotations

import hashlib

from pybravo.evals.text2wetlab.source_context import prepare_scientific_source


def test_selects_longest_complete_methods_section_instead_of_abstract():
    paper = (
        "Paper title\nAbstract\nMethods\nbrief\nResults\nbrief\n"
        "Introduction\nbackground " + "x" * 100 + "\n"
        "Methods\nSample collection\nVolume 250 µL per well.\n"
        "Wash twice with ethanol.\nResults\n" + "unrelated result " * 80
    )
    context = prepare_scientific_source(paper)
    assert context.strategy == "verbatim_methods_section"
    assert context.text == (
        "Methods\nSample collection\nVolume 250 µL per well.\n"
        "Wash twice with ethanol."
    )
    assert context.start_line == 9
    assert context.end_line == 13
    assert context.source_sha256 == hashlib.sha256(paper.encode("utf-8")).hexdigest()
    assert context.excerpt_sha256 == hashlib.sha256(context.text.encode("utf-8")).hexdigest()


def test_absent_or_oversize_methods_preserve_full_source():
    source = "No section headings; keep every detail."
    assert prepare_scientific_source(source).text == source
    oversize = "Methods\n" + "x" * 100 + "\nResults\n" + "y" * 300
    context = prepare_scientific_source(oversize, max_chars=40)
    assert context.text == oversize
    assert context.strategy == "full_source_methods_not_compact"
