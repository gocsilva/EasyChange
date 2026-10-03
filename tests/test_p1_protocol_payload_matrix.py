from __future__ import annotations

import json
import random
import string
import time

import pytest

from easychange.remote.optical_protocol import (
    decode_result_chunks,
    encode_result_chunks,
    encode_result_header,
)


def _payload(size: int) -> str:
    rng = random.Random(0xEC200000 + size)
    alphabet = string.ascii_letters + string.digits + " {}[]():,;._-"
    return "".join(rng.choice(alphabet) for _ in range(size))


@pytest.mark.parametrize("size", [1024, 10 * 1024, 100 * 1024, 1024 * 1024])
def test_ec2_payload_matrix_round_trips_with_small_header(size):
    result = {
        "ok": True,
        "command": "benchmark",
        "data": {"payload": _payload(size)},
        "error": None,
        "code": None,
        "command_id": "CBENCH",
        "duration_ms": 1,
        "sequence": f"QBENCH{size}",
    }
    started = time.perf_counter()
    chunks = encode_result_chunks(result)
    encoded_ms = (time.perf_counter() - started) * 1000

    header = encode_result_header(result, chunk_count=len(chunks))
    header_value = json.loads(header)
    assert header_value["p"] == "EC2H"
    assert header_value["f"] is True
    assert header_value["n"] == len(chunks)
    assert len(header.encode("utf-8")) < 512

    started = time.perf_counter()
    decoded = decode_result_chunks(chunks)
    decoded_ms = (time.perf_counter() - started) * 1000
    assert decoded == result

    # Non-timing correctness guard. Timings are collected by the companion
    # benchmark script because CI timing thresholds would be flaky.
    assert encoded_ms >= 0
    assert decoded_ms >= 0
    assert len(chunks) >= 1
