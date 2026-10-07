"""A focused process repair cannot silently change liquid task facts."""

from pybravo.evals.text2wetlab.adapter import EventLog
from pybravo.evals.text2wetlab.patch_repair import preserve_existing_task_actions


def _log(events):
    return EventLog(events, {"plate on 1": "plate_id", "reservoir on 2": "reservoir_id"})


def test_process_repair_may_insert_untimed_transition_without_changing_work():
    baseline = _log([
        {"kind": "thermocycler", "text": "Setting Thermocycler well block temperature to 42.0 °C with a hold time of 30 seconds"},
        {"kind": "aspirate", "instrument": "p300", "labware": "reservoir on 2",
         "well": "A1", "volume": 50, "channels": 1},
        {"kind": "dispense", "instrument": "p300", "labware": "plate on 1",
         "well": "A1", "volume": 50, "channels": 1},
        {"kind": "thermocycler", "text": "Setting Thermocycler well block temperature to 37.0 °C with a hold time of 60.0 minutes"},
    ])
    candidate = _log([
        baseline.events[0],
        {"kind": "thermocycler", "text": "Setting Thermocycler well block temperature to 37.0 °C"},
        *baseline.events[1:],
    ])
    assert preserve_existing_task_actions(baseline, candidate) is None


def test_process_repair_rejects_changed_volume_or_missing_timed_hold():
    baseline = _log([
        {"kind": "aspirate", "instrument": "p20", "labware": "reservoir on 2",
         "well": "A1", "volume": 1, "channels": 1},
        {"kind": "thermocycler", "text": "Setting block temperature to 42 °C with a hold time of 30 seconds"},
    ])
    changed_volume = _log([{**baseline.events[0], "volume": 2}, baseline.events[1]])
    missing_hold = _log([baseline.events[0]])
    assert "liquid transfers" in preserve_existing_task_actions(baseline, changed_volume)
    assert "timed module" in preserve_existing_task_actions(baseline, missing_hold)
