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


@pytest.mark.parametrize("unsafe_json", (
    '{"workspace":"demo","line":NaN}',
    '{"workspace":"demo","line":Infinity}',
    '{"workspace":"demo","line":-Infinity}',
    '{"workspace":"demo","line":1e10000}',
))
def test_nonfinite_imported_machine_session_fails_closed_without_rewriting(tmp_path, unsafe_json):
    path = tmp_path / ".easychange" / "session.json"
    path.parent.mkdir()
    path.write_text(unsafe_json, encoding="utf-8")
    session = MachineSession(path, "demo")
    assert session.data["workspace"] == "demo"
    assert session.data["line"] == 1
    assert path.read_text(encoding="utf-8") == unsafe_json


@pytest.mark.parametrize("unsafe", (
    float("nan"), float("inf"), float("-inf")
))
def test_nonfinite_session_save_rejects_before_replacing_existing_file(tmp_path, unsafe):
    path = tmp_path / ".easychange" / "session.json"
    session = MachineSession(path, "demo")
    session.save()
    original = path.read_bytes()
    session.data["line"] = unsafe
    with pytest.raises(ValueError):
        session.save()
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".session.json.*.tmp"))


@pytest.mark.parametrize("payload", (
    '{"workspace":"demo","session_id":"SVALID","session_id":"SOVERWRITE"}',
    '{"workspace":"demo","aliases":{"o":"open","o":"write"}}',
    '{"workspace":"demo","history":[":open"],"history":[":delete"]}',
    '{"workspace":"wrong","workspace":"demo","session_id":"SFORGED"}',
))
def test_duplicate_machine_session_fields_fail_closed_without_rewriting(tmp_path, payload):
    folder=tmp_path / ".easychange"
    folder.mkdir()
    path=folder / "session.json"
    path.write_text(payload,encoding="utf-8")
    imported=MachineSession(path,"demo")
    assert imported.data["workspace"]=="demo"
    assert imported.data["history"]==[]
    assert imported.data["aliases"]["o"]=="open"
    assert imported.data["session_id"].startswith("S")
    assert imported.data["session_id"] not in ("SVALID","SOVERWRITE","SFORGED")
    assert path.read_text(encoding="utf-8")==payload


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


@pytest.mark.parametrize("field, invalid, fallback", (
    ("transport", None, "local"),
    ("transport", 42, "local"),
    ("transport", ["hid"], "local"),
    ("transport", "unrecognized", "local"),
    ("output_mode", {"format": "json"}, "text"),
    ("output_mode", 3, "text"),
    ("output_mode", "unknown", "text"),
    ("mode", True, None),
    ("mode", ["machine"], None),
    ("line", False, 1),
    ("line", "100", 1),
    ("line", -1, 1),
    ("line", 2_147_483_648, 1),
    ("current_file", {"path": "src/a.py"}, None),
    ("current_file", ["src/a.py"], None),
    ("current_file", "bad\x00name", None),
    ("current_file", "x" * 4097, None),
))
def test_malformed_persisted_scalar_does_not_break_machine_session(
        tmp_path, field, invalid, fallback):
    path = tmp_path / ".easychange" / "session.json"
    path.parent.mkdir()
    original = json.dumps({
        "workspace": str(tmp_path), "session_id": "SSCALAR",
        "mode": "machine", field: invalid,
    }).encode("utf-8")
    path.write_bytes(original)

    restored = MachineSession(path, str(tmp_path))
    assert restored.data.get(field) == fallback
    assert restored.data["session_id"] == "SSCALAR"
    assert path.read_bytes() == original


def test_valid_persisted_editor_and_transport_scalar_modes_round_trip(tmp_path):
    path = tmp_path / ".easychange" / "session.json"
    initial = MachineSession(path, str(tmp_path))
    fields = {
        "current_file": "src/models/Arquivo.cs",
        "line": 2147483647,
        "output_mode": "json",
        "transport": "esp_hid",
        "mode": "machine",
    }
    initial.data.update(fields)
    initial.save()
    restored = MachineSession(path, str(tmp_path))
    assert {key: restored.data[key] for key in fields} == fields
