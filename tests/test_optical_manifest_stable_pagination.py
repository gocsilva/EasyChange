import hashlib
import json

from easychange.core.optical_test_counter import OpticalTestCounter


def test_running_manifest_pagination_remains_stable_when_new_digest_sorts_earlier():
    counter = OpticalTestCounter()
    session = "f" * 32
    counter.start(session)

    def payload(number):
        return json.dumps({"p": "EC2", "s": "QSTABLE", "i": number})

    def digest(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    initial = [payload(1), payload(2)]
    counter.record(initial)
    first = counter.status(session, limit=1)
    first_hash = first["manifest"][0]["sha256"]
    late = next(payload(number) for number in range(3, 200)
                if digest(payload(number)) < first_hash)
    counter.record([late])

    hashes = [first_hash]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = counter.status(session, cursor=cursor, limit=1)
        hashes.extend(row["sha256"] for row in page["manifest"])
        cursor = page["next_cursor"]
    assert len(hashes) == len(set(hashes)) == 3
    assert set(hashes) == {digest(value) for value in [*initial, late]}
