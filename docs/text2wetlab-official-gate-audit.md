# Text2WetLab verifier gap audit

This audit uses the pinned dataset revision `d7c8a9b93428997447eeaf2ee9e27ac3ce026872`.
It describes why pyBravo's local simulator reports are not a Harbor score. It does not
author protocols or run the Harbor judge.

The six standard Harbor tasks use the same [grade.py](https://huggingface.co/datasets/EvanOLeary/Text2WetLab/blob/d7c8a9b93428997447eeaf2ee9e27ac3ce026872/tasks/harbor/golden-gate-assembly/tests/grade.py):
it checks `protocol_lint.violations`, then `anti_hack.tripped`, then runs the
Opentrons simulator through `runlog.py`, and finally asks its judge to score five
binary rubric items. Its `simulate()` accepts only the runlog's output **file**;
the verifier comments that protocol stdout can contain a forged result. The
local runner now also requires that file for those six tasks. It accepts stdout
only for [RNA's different verifier](https://huggingface.co/datasets/EvanOLeary/Text2WetLab/blob/d7c8a9b93428997447eeaf2ee9e27ac3ce026872/tasks/harbor/opentrons-rna-extraction/tests/grade.py),
which reads the runlog's last stdout line.

The local runner applies pinned static lint, a simulator, the pinned runlog and
additional deterministic safety checks. It does **not** run the pinned
[anti-hack gate](https://huggingface.co/datasets/EvanOLeary/Text2WetLab/blob/d7c8a9b93428997447eeaf2ee9e27ac3ce026872/tasks/harbor/golden-gate-assembly/tests/anti_hack.py)
or either rubric judge. Thus a local `simulator_passed` cannot establish one of
the 35 official item scores. Harbor `grade.py` also exits successfully after
writing a failed verdict, so its exit code alone cannot establish a pass; inspect
the verifier's `reward.json` and item scores.

The saved `/tmp/pybravo-text2wetlab-golden-planning/report.json` illustrates the
other gap: its older top-level status is `simulator_passed`, while its embedded
`local_rubric_audit.status` is `failed` because required science was missing.
Older A1, split, AMPure and E. coli reports predate some current local checks.
They are useful historical evidence, not a current seven-task pass. The offline
`scripts/audit_text2wetlab_evidence.py` checks candidate/source digests and
current local gates across saved reports, retaining `official_score: null`:

```sh
.venv/bin/python scripts/audit_text2wetlab_evidence.py \
  --report /tmp/pybravo-text2wetlab-golden-planning/report.json
```

The auditor's `mechanical_evidence_complete` only means that one saved local
candidate cleared its listed local gates without a failed coverage audit. A
`needs_review` rubric status remains a scientific review item. Even seven such
local results would still require the actual pinned Harbor verifier and its
35 scored items before a 100% claim.
