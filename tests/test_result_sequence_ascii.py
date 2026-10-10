"""Durable sequence names must map injectively into ASCII filenames."""
from __future__ import annotations

import pytest

from easychange.core.result_store import DurableResultStore


@pytest.mark.parametrize("sequence", (
    "", "Qß", "Qı", "Q１２３", "Q١٢٣", "QＫ", "Q😀",
    "Q/path", "Q\\path", "Q.dot", "Q space", "Q"*129, None, 123,
))
def test_invalid_or_unicode_sequence_never_becomes_a_receipt_path(tmp_path, sequence):
    store=DurableResultStore(tmp_path / "receipts")
    with pytest.raises(ValueError,match="INVALID_RESULT_SEQUENCE"):
        store._path(sequence)
    with pytest.raises(ValueError,match="INVALID_RESULT_SEQUENCE"):
        store.claim_running(sequence,"a"*64,":state",mutation=False)
    assert list(store.root.glob("*.json.zlib"))==[]


def test_distinct_unicode_identifier_cannot_alias_existing_ascii_receipt(tmp_path):
    store=DurableResultStore(tmp_path / "receipts")
    original,claimed=store.claim_running("QSS","a"*64,":state",mutation=False)
    assert claimed is True
    with pytest.raises(ValueError,match="INVALID_RESULT_SEQUENCE"):
        store.claim_running("Qß","b"*64,":state",mutation=False)
    assert store.get("QSS")["execution_id"]==original["execution_id"]
    # Historical ASCII case normalization remains compatible.
    assert store._path("qss")==store._path("QSS")
    assert store._path("q-7_abc").name=="Q-7_ABC.json.zlib"
