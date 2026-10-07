"""Select verbatim experimental-method passages from a supplied paper.

This reduces irrelevant article context in one-shot code generation. The
complete source stays external to the generated protocol; selection never
invents, paraphrases, or supplies benchmark answers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_METHODS = re.compile(r"(?im)^(?:materials\s+and\s+methods|methods)\s*$")
_RESULTS = re.compile(r"(?im)^results\s*$")


@dataclass(frozen=True)
class SourceContext:
    text: str
    strategy: str
    source_sha256: str
    excerpt_sha256: str
    start_line: int | None
    end_line: int | None
    line_spans: tuple[tuple[int, int], ...] = ()


_SECTION = re.compile(r"(?im)^(?:results\s*>|discussion\s*$|conclusions?\s*$)")
_RESULT_SUBSECTION = re.compile(r"(?im)^results\s*>\s*(.+)$")
_METHOD_SUBSECTION = re.compile(
    r"(?im)^(?:materials\s+and\s+methods|methods)\s*>\s*(.+)$"
)
_COMMON = frozenset({"a", "an", "and", "as", "by", "for", "from", "in", "of", "on", "or",
                     "the", "to", "with", "protocol", "using", "use", "robot", "paper", "ot", "2"})
_ROUTINE = _COMMON | frozenset({
    "are", "at", "be", "each", "into", "its", "labware", "plate", "plates",
    "sample", "samples", "that", "these", "this", "well", "wells",
})


def _title_terms(task_instruction: str) -> set[str]:
    title = next((line.lstrip("# ").strip() for line in task_instruction.splitlines()
                  if line.lstrip().startswith("#")), task_instruction.splitlines()[0].strip())
    return {word for word in re.findall(r"[a-z0-9]+", title.casefold())
            if len(word) > 2 and word not in _COMMON}


def _relevant_result_spans(source: str, task_instruction: str) -> list[tuple[int, int, int]]:
    """Rank complete Results subsections by overlap with the task title."""
    terms = _title_terms(task_instruction)
    if not terms:
        return []
    candidates: list[tuple[int, int, int]] = []
    for heading in _RESULT_SUBSECTION.finditer(source):
        heading_terms = set(re.findall(r"[a-z0-9]+", heading.group(1).casefold()))
        score = len(terms & heading_terms)
        if score < 2:
            continue
        next_section = _SECTION.search(source, heading.end())
        end = next_section.start() if next_section is not None else len(source)
        candidates.append((score, heading.start(), end))
    return sorted(candidates, key=lambda item: (-item[0], item[1]))


def prepare_scientific_source(
    source: str, *, max_chars: int = 25_000, task_instruction: str | None = None,
) -> SourceContext:
    """Select verbatim Methods plus task-relevant Results subsections.

    If headings are absent or the section exceeds the input budget, preserve
    the full source. A paper excerpt is always verbatim and traced by offsets
    and digest so a reviewer can inspect what the model saw.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    candidates: list[tuple[int, int]] = []
    for heading in _METHODS.finditer(source):
        end_heading = _RESULTS.search(source, heading.end())
        if end_heading is not None:
            candidates.append((heading.start(), end_heading.start()))
    if not candidates:
        return SourceContext(source, "full_source_no_methods_section", source_digest,
                             source_digest, None, None)
    start, end = max(candidates, key=lambda span: span[1] - span[0])
    excerpt = source[start:end].strip()
    if len(excerpt) > max_chars or len(excerpt) >= len(source) * 0.8:
        return SourceContext(source, "full_source_methods_not_compact", source_digest,
                             source_digest, None, None)
    spans = [(start, end)]
    if task_instruction:
        for _score, extra_start, extra_end in _relevant_result_spans(source, task_instruction)[:2]:
            extra = source[extra_start:extra_end].strip()
            proposed_length = sum(len(source[a:b].strip()) for a, b in spans) + len(extra) + 4 * len(spans)
            if extra and proposed_length <= max_chars:
                spans.append((extra_start, extra_end))
    spans.sort()
    text = "\n\n".join(source[a:b].strip() for a, b in spans)
    line_spans = tuple((source.count("\n", 0, a) + 1, source.count("\n", 0, b) + 1)
                       for a, b in spans)
    return SourceContext(text,
                         "verbatim_methods_plus_relevant_results" if len(spans) > 1
                         else "verbatim_methods_section",
                         source_digest, hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         line_spans[0][0] if len(spans) == 1 else None,
                         line_spans[0][1] if len(spans) == 1 else None,
                         line_spans)


def prepare_planning_source(
    source: str, *, task_instruction: str, max_chars: int = 9_000,
) -> SourceContext:
    """Offer a bounded, verbatim paper excerpt to the optional planning pass.

    Only papers with explicit Methods subsections can be shortened. Entire
    subsections are ranked by overlap with the task text; the best matching
    Results subsection is retained first. Papers without that structure keep
    the existing full Methods selection. Code generation still receives the
    unshortened ``prepare_scientific_source`` result.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    baseline = prepare_scientific_source(source, task_instruction=task_instruction)
    if len(baseline.text) <= max_chars:
        return baseline

    methods = _METHODS.search(source)
    results = _RESULTS.search(source, methods.end()) if methods else None
    if methods is None or results is None:
        return baseline
    headings = list(_METHOD_SUBSECTION.finditer(source, methods.end(), results.start()))
    if len(headings) < 2:
        return baseline

    task_terms = {
        term for term in re.findall(r"[a-z0-9]+", task_instruction.casefold())
        if len(term) > 2 and term not in _ROUTINE
    }
    method_candidates: list[tuple[int, int, int]] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else results.start()
        section_terms = set(re.findall(r"[a-z0-9]+", source[heading.start():end].casefold()))
        method_candidates.append((len(task_terms & section_terms), heading.start(), end))
    method_candidates.sort(key=lambda item: (-item[0], item[1]))

    result_candidates = _relevant_result_spans(source, task_instruction)
    spans: list[tuple[int, int]] = []
    if result_candidates:
        _, start, end = result_candidates[0]
        if len(source[start:end].strip()) >= max_chars:
            return baseline
        spans.append((start, end))
    for _score, start, end in method_candidates:
        proposed = spans + [(start, end)]
        length = sum(len(source[a:b].strip()) for a, b in proposed) + 2 * (len(proposed) - 1)
        if length <= max_chars:
            spans.append((start, end))
    if not any(start < results.start() for start, _end in spans):
        return baseline

    spans.sort()
    excerpt = "\n\n".join(source[start:end].strip() for start, end in spans)
    line_spans = tuple(
        (source.count("\n", 0, start) + 1, source.count("\n", 0, end) + 1)
        for start, end in spans
    )
    return SourceContext(
        excerpt, "verbatim_ranked_method_subsections_for_planning",
        baseline.source_sha256,
        hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        None, None, line_spans,
    )
