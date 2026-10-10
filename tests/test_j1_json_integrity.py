"""J1 frames must have one unambiguous meaning before execution and replay."""
from __future__ import annotations

import pytest

from easychange.core.command_service import CommandService, _decode_structured_payload
from easychange.core.workspace import Workspace


@pytest.mark.parametrize("payload,error",[
    ('{"op":"operation","op":"batch","type":"replace_exact"}',
     "DUPLICATE_STRUCTURED_JSON_KEY"),
    ('{"op":"operation","type":"replace_exact","args":{"path":"one","path":"two"}}',
     "DUPLICATE_STRUCTURED_JSON_KEY"),
    ('{"op":"operation","type":"replace_exact","offset":NaN}',
     "NONFINITE_STRUCTURED_JSON_NUMBER"),
    ('{"op":"operation","type":"replace_exact","timeout":Infinity}',
     "NONFINITE_STRUCTURED_JSON_NUMBER"),
    ('{"op":"operation","type":"replace_exact","timeout":-Infinity}',
     "NONFINITE_STRUCTURED_JSON_NUMBER"),
    ('{"op":"operation","type":"replace_exact","timeout":1e9999}',
     "NONFINITE_STRUCTURED_JSON_NUMBER"),
])
def test_invalid_j1_is_rejected_before_claim_or_mutation(tmp_path, monkeypatch, payload, error):
    service=CommandService(Workspace.open(tmp_path))
    try:
        attempts=[]
        monkeypatch.setattr(
            service,"_execute_structured",
            lambda *args,**kwargs: attempts.append((args,kwargs)))
        result=service.execute(":ec QJ1INVALID1 :j1 "+payload)
        assert result.ok is False
        assert result.code=="INVALID_STRUCTURED_PAYLOAD"
        assert error in (result.error or "")
        assert attempts==[]
        assert service.result_store.get("QJ1INVALID1") is None
    finally:
        service.close()


def test_valid_finite_nested_j1_still_parses_and_preserves_types():
    result=_decode_structured_payload(
        '{"op":"operation","type":"read","params":{"fraction":0.125,'
        '"count":1,"enabled":true,"label":"value"}}')
    assert result["params"]=={
        "fraction":0.125,"count":1,"enabled":True,"label":"value"}
    assert type(result["params"]["enabled"]) is bool
    assert type(result["params"]["count"]) is int
