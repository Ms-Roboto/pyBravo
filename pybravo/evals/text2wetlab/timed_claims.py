"""Conservative review of source-grounded timed claims in OT-2 comments.

A ``protocol.comment`` records words but performs no wait. This module checks
literal top-level comments in ``run`` against the supplied procedure and the
following stage's explicit delay, timed module hold, or operator pause. It
does not infer an incubation from a benchmark rubric or from a comment alone.
"""

from __future__ import annotations

import ast
import re

from pybravo.evals.text2wetlab.planning import PlanIssue

_DURATION = re.compile(
    r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|h|minutes?|mins?|min|seconds?|secs?|s)\b", re.IGNORECASE,
)
_TIMED_STAGE = re.compile(r"\b(?:incubat\w*|wait\w*|hold\w*|bind\w*|clear\w*|settle\w*|dry|elut\w*)\b",
                          re.IGNORECASE)
_OFF_DECK = re.compile(r"\b(?:off[ -]?deck|outside\s+the\s+robot|"
                       r"out\s+of\s+the\s+robot|at\s+the\s+bench|"
                       r"remove\w*\s+from\s+the\s+(?:deck|instrument))\b",
                       re.IGNORECASE)
_NO_ROBOT_WAIT = re.compile(r"\b(?:no\s+(?:robot\s+)?wait\s+(?:is\s+)?needed|"
                            r"do\s+not\s+(?:delay|wait)|without\s+(?:a\s+)?robot\s+wait)\b",
                            re.IGNORECASE)
_COMMENT_ONLY = re.compile(r"\bnot\s+simulated\b.*\bprotocol\.comment\b|"
                           r"\brecord\s+(?:it\s+)?with\s+protocol\.comment\b",
                           re.IGNORECASE)


def _duration_seconds(text: str) -> set[float]:
    return {
        float(match.group(1)) * (
            3600 if match.group(2).casefold().startswith("h") else
            60 if match.group(2).casefold().startswith("m") else 1
        ) for match in _DURATION.finditer(text)
    }


def _source_line(claim: str, instruction: str, scientific_source: str | None) -> str | None:
    matches: list[str] = []
    for corpus in (instruction, scientific_source or ""):
        for line in corpus.splitlines():
            if claim.casefold() in line.casefold():
                matches.append(line.strip()[:350])
    return next((line for line in matches if _OFF_DECK.search(line)
                 or _NO_ROBOT_WAIT.search(line) or _COMMENT_ONLY.search(line)),
                matches[0] if matches else None)


def _call(statement: ast.stmt) -> ast.Call | None:
    return statement.value if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call) else None


def _is_protocol_comment(statement: ast.stmt) -> str | None:
    call = _call(statement)
    if (call is not None and isinstance(call.func, ast.Attribute)
            and call.func.attr == "comment" and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "protocol" and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)):
        return call.args[0].value
    return None


def _literal_number(value: ast.expr) -> float | None:
    if (isinstance(value, ast.Constant) and isinstance(value.value, (int, float))
            and not isinstance(value.value, bool)):
        return float(value.value)
    return None


def _wait_seconds(call: ast.Call) -> float | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    kind = call.func.attr
    if kind not in {"delay", "set_block_temperature"}:
        return None
    seconds_key = "seconds" if kind == "delay" else "hold_time_seconds"
    minutes_key = "minutes" if kind == "delay" else "hold_time_minutes"
    quantities = {keyword.arg: _literal_number(keyword.value) for keyword in call.keywords}
    if kind == "delay" and call.args:
        quantities.setdefault("seconds", _literal_number(call.args[0]))
    seconds = quantities.get(seconds_key)
    minutes = quantities.get(minutes_key)
    if seconds is None and minutes is None:
        return None
    return (seconds or 0) + 60 * (minutes or 0)


def audit_timed_comments(
    code: str, *, instruction: str, scientific_source: str | None = None,
) -> tuple[PlanIssue, ...]:
    """Find a cited, on-deck timed comment with no matching action in its stage.

    Only literal ``protocol.comment`` calls at the top level of ``run`` are
    checked. The stage ends at the next top-level comment. A duration hidden
    behind dynamic expressions or helper calls is left for review, rather
    than being called absent. Explicit source instructions to record a step
    with comments yield a warning, not a contradictory execution error.
    """
    try:
        module = ast.parse(code)
    except SyntaxError:
        return ()
    run = next((item for item in module.body if isinstance(item, ast.FunctionDef)
                and item.name == "run"), None)
    if run is None:
        return ()
    issues: list[PlanIssue] = []
    for index, statement in enumerate(run.body):
        claim = _is_protocol_comment(statement)
        if claim is None or not _TIMED_STAGE.search(claim):
            continue
        durations = _duration_seconds(claim)
        if not durations or _OFF_DECK.search(claim) or _NO_ROBOT_WAIT.search(claim):
            continue
        quoted = _source_line(claim, instruction, scientific_source)
        if quoted is None or _OFF_DECK.search(quoted) or _NO_ROBOT_WAIT.search(quoted):
            continue
        next_comment = next((later for later in range(index + 1, len(run.body))
                            if _is_protocol_comment(run.body[later]) is not None), len(run.body))
        if next_comment < len(run.body):
            followup = _is_protocol_comment(run.body[next_comment])
            if followup is not None and _NO_ROBOT_WAIT.search(followup):
                continue
        calls = [node for step in run.body[index + 1:next_comment]
                 for node in ast.walk(step) if isinstance(node, ast.Call)]
        if any(isinstance(call.func, ast.Attribute) and call.func.attr == "pause"
               and isinstance(call.func.value, ast.Name) and call.func.value.id == "protocol"
               for call in calls):
            continue
        observed = {_wait_seconds(call) for call in calls}
        if all(any(wait is not None and abs(wait - required) <= 1
                   for wait in observed) for required in durations):
            continue
        dynamic_wait = any(isinstance(call.func, ast.Attribute)
                           and call.func.attr in {"delay", "set_block_temperature", "execute_profile"}
                           and _wait_seconds(call) is None for call in calls)
        indirect_wait = any(isinstance(call.func, ast.Name)
                            and call.func.id not in {"enumerate", "len", "list", "max", "min",
                                                     "range", "reversed", "tuple", "zip"}
                            for call in calls)
        comment_only = _COMMENT_ONLY.search(quoted) is not None
        severity = "warning" if comment_only or dynamic_wait or indirect_wait else "error"
        qualifier = ("The source explicitly asks to record this stage with a comment; "
                     if comment_only else "")
        issues.append(PlanIssue(
            "timed_comment_without_action", f"code:{statement.lineno}",
            f"{qualifier}At line {statement.lineno}, protocol.comment records a timed "
            f"on-deck stage but no matching delay, timed module hold, or operator pause "
            f"is visible before the next stage comment. Source: {quoted}",
            severity,
        ))
    return tuple(issues)
