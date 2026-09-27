"""The tip store's save path must round-trip kind and overflow_ul.

Before this, any edit through the Tip Editor rewrote the row without them, so
an AssayMAP cartridge silently became a disposable tip and Tips On would zero
W during the next cartridge mount.
"""
from __future__ import annotations

from pybravo import tips


def test_patch_keeps_kind_and_overflow(monkeypatch, tmp_path):
    store = {"tips": [{
        "tip_id": "am_cartridge_60ul", "label": "cartridge", "capacity_ul": 60.0,
        "length_mm": 29.2, "source": "x", "model_3d": None,
        "compatible_heads": ["HT_96_ASSAYMAP"], "kind": "cartridge", "overflow_ul": 140.0,
    }]}
    saved = {}
    monkeypatch.setattr(tips, "load_store", lambda: store)
    monkeypatch.setattr(tips, "save_store", lambda s: saved.update(s))

    row = tips.patch_tip_definition("am_cartridge_60ul", {"label": "renamed"})
    assert row["label"] == "renamed"
    assert row["kind"] == "cartridge"
    assert row["overflow_ul"] == 140.0
    assert saved["tips"][0]["kind"] == "cartridge"


def test_create_defaults_to_a_plain_tip(monkeypatch):
    store = {"tips": []}
    monkeypatch.setattr(tips, "load_store", lambda: store)
    monkeypatch.setattr(tips, "save_store", lambda s: None)
    row = tips.create_tip_definition({"tip_id": "t1", "label": "t", "capacity_ul": 10.0})
    assert row["kind"] == "tip"
    assert row["overflow_ul"] is None
