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


def prepare_scientific_source(source: str, *, max_chars: int = 25_000) -> SourceContext:
    """Prefer the longest complete Methods section before Results.

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
    start_line = source.count("\n", 0, start) + 1
    end_line = source.count("\n", 0, end) + 1
    return SourceContext(excerpt, "verbatim_methods_section", source_digest,
                         hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                         start_line, end_line)
