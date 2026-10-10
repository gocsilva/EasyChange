"""Durable result retention may not be disabled by NaN/infinity parameters."""
from __future__ import annotations

import math
import pytest

from easychange.core.result_store import DurableResultStore


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"),
                                 "inf", "NaN"])
@pytest.mark.parametrize("parameter", ["ttl_seconds", "active_ttl_seconds"])
def test_nonfinite_retention_rejected_before_creating_store(tmp_path, bad, parameter):
    destination=tmp_path/"receipts"
    with pytest.raises(ValueError,match="INVALID_RESULT_RETENTION_TTL"):
        DurableResultStore(destination,**{parameter:bad})
    assert not destination.exists()


def test_finite_retention_limits_preserve_existing_minimums(tmp_path):
    store=DurableResultStore(
        tmp_path/"minimums",max_results=8,ttl_seconds=1,active_ttl_seconds=2)
    assert store.max_results==16
    assert store.ttl_seconds==60.0
    assert store.active_ttl_seconds==3600

    normal=DurableResultStore(
        tmp_path/"normal",max_results=128,
        ttl_seconds=1800.0,active_ttl_seconds=86_400.0)
    assert math.isfinite(normal.ttl_seconds)
    assert normal.active_ttl_seconds==86_400.0
