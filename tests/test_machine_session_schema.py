"""Persisted machine sessions must tolerate malformed nested JSON fields."""
from __future__ import annotations

import json

import pytest

from easychange.core.ids import IdFactory
from easychange.core.state import MachineSession


@pytest.mark.parametrize("bad_fields", (
    {"aliases": ["o", "open"]},
    {"aliases": {"o": 123}},
    {"aliases": {"o": None}},
    {"history": {"last": ":read"}},
    {"history": [":read", None]},
    {"id_counts": []},
    {"id_counts": {"C": "9"}},
    {"id_counts": {"C": True}},
    {"id_counts": {"C": -1}},
    {"file_hashes": ["a", "b"]},
    {"file_hashes": {"src/a.py": None}},
))
def test_invalid_nested_session_field_isolated_without_rewriting_disk(tmp_path, bad_fields):
    folder = tmp_path / ".easychange"
    folder.mkdir()
    path = folder / "session.json"
    raw = json.dumps({
        "workspace": str(tmp_path), "session_id": "SABCDEF",
        "mode": "machine", **bad_fields,
    }).encode("utf-8")
    path.write_bytes(raw)

    session = MachineSession(path, str(tmp_path))
    assert path.read_bytes() == raw
    assert session.data["mode"] == "machine"
    assert session.data["workspace"] == str(tmp_path)
    if "aliases" in bad_fields:
        assert session.data["aliases"]["o"] == "open"
    if "history" in bad_fields:
        assert session.data["history"] == []
    if "id_counts" in bad_fields:
        counts = IdFactory(session.data.get("id_counts")).next("C")
        assert counts == "C1"
    if "file_hashes" in bad_fields:
        assert session.data.get("file_hashes", {}) == {}


@pytest.mark.parametrize("invalid", (None, False, 42, "", "x" * 129))
def test_invalid_persisted_session_id_keeps_fresh_safe_identity(tmp_path, invalid):
    folder = tmp_path / ".easychange"
    folder.mkdir()
    path = folder / "session.json"
    path.write_text(json.dumps({
        "workspace": str(tmp_path), "session_id": invalid
    }), encoding="utf-8")
    session = MachineSession(path, str(tmp_path))
    assert isinstance(session.data["session_id"], str)
    assert session.data["session_id"].startswith("S")
    assert session.data["session_id"] != invalid


def test_valid_session_fields_keep_compatibility_and_roundtrip(tmp_path):
    path = tmp_path / ".easychange" / "session.json"
    first = MachineSession(path, str(tmp_path))
    first.data.update({
        "session_id": "SVALID",
        "history": [":open", ":search text"],
        "aliases": {"o": "open", "custom": "search"},
        "id_counts": {"C": 7},
        "file_hashes": {"src/a.py": "123abc"},
        "mode": "machine",
        "output_mode": "compact",
    })
    first.save()
    second = MachineSession(path, str(tmp_path))
    assert second.data["session_id"] == "SVALID"
    assert second.data["history"] == [":open", ":search text"]
    assert second.data["aliases"]["custom"] == "search"
    assert IdFactory(second.data["id_counts"]).next("C") == "C8"
    assert second.data["file_hashes"]["src/a.py"] == "123abc"
    assert second.data["output_mode"] == "compact"


def test_nonmatching_workspace_cannot_import_other_workspace_state(tmp_path):
    path = tmp_path / ".easychange" / "session.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "workspace": "/unrelated/source",
        "session_id": "SOTHER",
        "aliases": {"o": "dangerous-override"},
    }), encoding="utf-8")
    session = MachineSession(path, str(tmp_path))
    assert session.data["workspace"] == str(tmp_path)
    assert session.data["session_id"] != "SOTHER"
    assert session.data["aliases"]["o"] == "open"


@pytest.mark.parametrize("field, invalid", (
    ("results", ["stale"]),
    ("results", {"R1": None}),
    ("file_refs", ["F1", "path"]),
    ("file_refs", {"F1": None}),
    ("macros", ["build", "test"]),
    ("macros", {"release": "rm -rf target"}),
    ("macros", {"release": [":build", 1]}),
    ("snapshots", ["S1", 1]),
    ("snapshots", {"S1": True}),
    ("snapshots", {"S1": -2}),
))
def test_session_cannot_boot_with_non_mapping_or_invalid_mapping_values(tmp_path, field, invalid):
    path = tmp_path / ".easychange" / "session.json"
    path.parent.mkdir()
    original = json.dumps({
        "workspace": str(tmp_path),
        "session_id": "SHEALTHY",
        field: invalid,
    }).encode("utf-8")
    path.write_bytes(original)
    session = MachineSession(path, str(tmp_path))
    assert session.data.get(field, {}) == {}
    assert session.data["session_id"] == "SHEALTHY"
    assert path.read_bytes() == original


def test_valid_extra_session_maps_are_preserved(tmp_path):
    path = tmp_path / ".easychange" / "session.json"
    original = MachineSession(path, str(tmp_path))
    expected = {
        "results": {"R1": {"ok": True}},
        "file_refs": {"F1": "src/a.py"},
        "macros": {"ci": [":build", ":test"]},
        "snapshots": {"S1": 4},
    }
    original.data.update(expected)
    original.save()
    reloaded = MachineSession(path, str(tmp_path))
    for key, value in expected.items():
        assert reloaded.data[key] == value
