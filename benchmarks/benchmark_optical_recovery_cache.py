"""Measure repeated EC2 packet recovery with and without prepared-token reuse."""
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace
from easychange.remote.optical_protocol import encode_result_chunks


def _measure(callback, repeats: int = 5) -> list[float]:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        callback()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def main() -> dict:
    sequence = "BENCH-OPTICAL-RECOVERY"
    result = {
        "ok": True,
        "command": "read",
        "data": {"text": "0123456789abcdef\n" * 80_000},
        "error": None,
        "code": None,
        "command_id": "BENCH-C1",
        "duration_ms": 1,
        "sequence": sequence,
    }
    indexes = list(range(16))
    uncached = _measure(lambda: [
        encode_result_chunks(result, indexes=[index]) for index in indexes
    ])

    with tempfile.TemporaryDirectory(prefix="easychange-optical-bench-") as folder:
        service = CommandService(Workspace.open(Path(folder)))
        try:
            cached = _measure(lambda: [
                service._optical_recovery_packets(
                    sequence, result, indexes=[index]
                )
                for index in indexes
            ])
            cache_bytes = sum(service._optical_encoding_cache_sizes.values())
        finally:
            service.close()

    before = statistics.median(uncached)
    after = statistics.median(cached)
    return {
        "synthetic": True,
        "payload_chars": len(result["data"]["text"]),
        "requested_indexes": len(indexes),
        "repeats": len(uncached),
        "uncached_median_ms": round(before, 3),
        "cached_median_ms": round(after, 3),
        "improvement_percent": round((1 - after / before) * 100, 2),
        "cache_bytes": cache_bytes,
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, sort_keys=True))
