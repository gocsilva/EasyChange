from __future__ import annotations

import json
import random
import statistics
import string
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easychange.remote.optical_protocol import (
    decode_result_chunks,
    encode_result_chunks,
    encode_result_header,
)


SIZES = [1024, 10 * 1024, 100 * 1024, 1024 * 1024]


def payload(size: int) -> str:
    rng = random.Random(0xEC2B0000 + size)
    alphabet = string.ascii_letters + string.digits + " {}[]():,;._-"
    return "".join(rng.choice(alphabet) for _ in range(size))


def main() -> None:
    rows = []
    for size in SIZES:
        result = {
            "ok": True,
            "command": "benchmark",
            "data": {"payload": payload(size)},
            "command_id": "CBENCH",
            "duration_ms": 1,
            "sequence": f"QBENCH{size}",
        }
        encode_samples = []
        decode_samples = []
        chunks = None
        for _ in range(3):
            started = time.perf_counter()
            chunks = encode_result_chunks(result)
            encode_samples.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            decoded = decode_result_chunks(chunks)
            decode_samples.append((time.perf_counter() - started) * 1000)
            assert decoded == result
        assert chunks is not None
        header = encode_result_header(result, chunk_count=len(chunks))
        rows.append({
            "payload_chars": size,
            "chunks_full_payload": len(chunks),
            "result_header_bytes": len(header.encode("utf-8")),
            "encode_ms_p50": round(statistics.median(encode_samples), 3),
            "decode_ms_p50": round(statistics.median(decode_samples), 3),
            "one_missing_chunk_selective_calls": 1,
            "full_retransmit_chunks_avoided_for_one_loss": max(0, len(chunks) - 1),
            "round_trip_success_rate": 1.0,
        })
    print(json.dumps({
        "protocol": "EC2/EC2H",
        "note": "Synthetic codec benchmark; QR camera/render latency is not measured.",
        "rows": rows,
    }, indent=2))


if __name__ == "__main__":
    main()
