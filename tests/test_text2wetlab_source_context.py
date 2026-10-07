"""Scientific source reduction keeps complete, verbatim Methods evidence."""

from __future__ import annotations

import hashlib

from pybravo.evals.text2wetlab.source_context import (
    prepare_planning_source,
    prepare_scientific_source,
)


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


def test_task_relevant_results_section_is_retained_verbatim_with_provenance():
    paper = (
        "Title\nMaterials and methods\nPCR uses 25 µL.\n"
        "Results\nGeneral findings.\n"
        "Results > PCR\nSeven fragments amplified.\n"
        "Results > Golden Gate assembly\nDigest fragments, then assemble.\n"
        "Results > Unrelated assay\nIgnore this assay.\nDiscussion\nOther details.\n"
    )
    task = "# Golden Gate assembly from PCR fragments\nBuild four plasmids."
    context = prepare_scientific_source(paper, task_instruction=task)
    assert context.strategy == "verbatim_methods_plus_relevant_results"
    assert "PCR uses 25 µL." in context.text
    assert "Seven fragments amplified." not in context.text
    assert "Digest fragments, then assemble." in context.text
    assert "Ignore this assay." not in context.text
    assert context.line_spans == ((2, 4), (8, 10))


def test_planning_selector_keeps_whole_relevant_method_and_results_subsections():
    paper = (
        "Title\nMaterials and methods\n"
        "Materials and methods > Unrelated reagent history\n2.1\n"
        + "History has no bearing on this transfer. " * 14 + "\n"
        "Materials and methods > PCR amplification\n2.2\n"
        "PCRs were performed in 25 µL volumes. "
        + "Amplification used a thermal cycle. " * 8 + "\n"
        "Materials and methods > Liquid handling\n2.3\n"
        "PCRs were manually transferred to a thermocycler. "
        + "The robot prepared liquid reactions. " * 8 + "\n"
        "Results\nGeneral findings.\n"
        "Results > PCR assembly\n"
        "Fragments were cleaned and concentrated before assembly.\n"
        "Results > Unrelated assay\nAn unrelated assay is described here.\n"
        "Discussion\nOther details.\n"
    )
    task = "# PCR assembly\nPrepare amplification and liquid handling, then clean fragments."
    context = prepare_planning_source(paper, task_instruction=task, max_chars=1_100)
    assert context.strategy == "verbatim_ranked_method_subsections_for_planning"
    assert "PCRs were performed in 25 µL volumes" in context.text
    assert "PCRs were manually transferred to a thermocycler" in context.text
    assert "Fragments were cleaned and concentrated before assembly" in context.text
    assert "History has no bearing" not in context.text
    assert "An unrelated assay" not in context.text
    assert len(context.text) <= 1_100
    assert len(context.line_spans) == 3
    assert context.excerpt_sha256 == hashlib.sha256(context.text.encode("utf-8")).hexdigest()


def test_planning_selector_falls_back_when_source_has_no_complete_subsections():
    source = "Methods\nPCRs were performed in 25 µL volumes.\nResults\n" + "result " * 300
    baseline = prepare_scientific_source(source, task_instruction="# PCR")
    assert prepare_planning_source(source, task_instruction="# PCR", max_chars=20) == baseline
