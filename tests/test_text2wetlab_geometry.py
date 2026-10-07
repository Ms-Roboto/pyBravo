"""Catalog geometry is supplied as factual context, never guessed from a name."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pybravo.evals.text2wetlab import geometry


def test_geometry_uses_installed_simulator_definitions(tmp_path, monkeypatch):
    simulator = tmp_path / "opentrons_simulate"
    simulator.touch()
    (tmp_path / "python").touch()
    monkeypatch.setattr(geometry.shutil, "which", lambda _: str(simulator))
    definitions = {
        "corning_96_wellplate_360ul_flat": {
            "ordering": [[f"{row}{column}" for row in "ABCDEFGH"]
                         for column in range(1, 13)],
            "parameters": {"isTiprack": False},
        },
    }
    monkeypatch.setattr(
        geometry.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(definitions)),
    )
    result = geometry.labware_geometry_context(
        "Load `corning_96_wellplate_360ul_flat` and unknown `made_up_96_plate`.",
        simulator_command=simulator,
    )
    assert set(result) == {"corning_96_wellplate_360ul_flat"}
    assert result["corning_96_wellplate_360ul_flat"] == {
        "columns": 12,
        "rows_per_column": 8,
        "well_count": 96,
        "first_column": [f"{row}1" for row in "ABCDEFGH"],
        "last_column": [f"{row}12" for row in "ABCDEFGH"],
        "is_tiprack": False,
    }


def test_custom_labware_overrides_catalog_definition(tmp_path, monkeypatch):
    monkeypatch.setattr(geometry.shutil, "which", lambda _: None)
    custom = tmp_path / "thermo_96_wellplate_200ul.json"
    custom.write_text(json.dumps({
        "parameters": {"loadName": "thermo_96_wellplate_200ul", "isTiprack": False},
        "ordering": [[f"{row}{column}" for row in "ABCDEFGH"]
                     for column in range(1, 13)],
    }), encoding="utf-8")
    result = geometry.labware_geometry_context(
        "Use `thermo_96_wellplate_200ul`.", simulator_command=None, labware_dir=tmp_path,
    )
    assert result["thermo_96_wellplate_200ul"]["rows_per_column"] == 8
    assert result["thermo_96_wellplate_200ul"]["well_count"] == 96


def test_decimal_tube_size_load_name_keeps_exact_small_rack_wells(tmp_path, monkeypatch):
    simulator = tmp_path / "opentrons_simulate"
    simulator.touch()
    (tmp_path / "python").touch()
    monkeypatch.setattr(geometry.shutil, "which", lambda _: str(simulator))
    name = "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap"
    ordering = [[f"{row}{column}" for row in "ABCD"] for column in range(1, 7)]
    requested: list[str] = []

    def catalog_lookup(*args, **kwargs):
        requested.extend(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            name: {"ordering": ordering, "parameters": {"isTiprack": False}},
        }))

    monkeypatch.setattr(geometry.subprocess, "run", catalog_lookup)
    result = geometry.labware_geometry_context(
        f"Load `{name}` as tubes.", simulator_command=simulator,
    )

    assert name in requested
    assert result[name]["first_column"] == ["A1", "B1", "C1", "D1"]
    assert result[name]["last_column"] == ["A6", "B6", "C6", "D6"]
    assert result[name]["valid_wells"] == [well for column in ordering for well in column]
    assert "E1" not in result[name]["valid_wells"]
