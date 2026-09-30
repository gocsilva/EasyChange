"""Compare UTF-8 raw HID bytes with EC1 zlib + Base64URL on representative payloads."""
from __future__ import annotations

import json
import statistics
import time

from easychange.core.transport_codec import encode_command


def main() -> dict:
    source = "def replace_line(value):\n    return value.strip()\n# protocolo EasyChange e HID\n"
    rows = []
    for target in (128, 256, 1024, 5120, 10240, 51200):
        payload = (source * ((target // len(source)) + 1))[:target]
        samples = []
        for _ in range(20):
            start = time.perf_counter()
            wire, metadata = encode_command(payload)
            samples.append((time.perf_counter() - start) * 1000)
        rows.append({"raw_bytes": len(payload.encode("utf-8")), "wire_bytes": metadata["wire_bytes"],
                     "encoding": metadata["encoding"], "median_encode_ms": round(statistics.median(samples), 4),
                     "savings_percent": round((1 - metadata["wire_bytes"] / metadata["raw_bytes"]) * 100, 2)})
    return {"synthetic": True, "threshold_bytes": 256, "measurements": rows}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, sort_keys=True))
