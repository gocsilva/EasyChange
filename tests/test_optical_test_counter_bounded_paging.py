from collections import Counter

from easychange.core.optical_test_counter import OpticalTestCounter


class IterationGuardCounter(Counter):
    def __iter__(self):
        for index, key in enumerate(super().__iter__()):
            if index >= 64:
                raise AssertionError("QR status materialized unrelated digest entries")
            yield key


def test_optical_status_pages_without_materializing_full_manifest():
    counter = OpticalTestCounter()
    session = "a" * 16
    counter.start(session)
    counter._digests = IterationGuardCounter(
        {f"{number:064x}": 1 for number in range(1000)}
    )

    page = counter.status(session, cursor=10, limit=3)

    assert [item["sha256"] for item in page["manifest"]] == [
        f"{number:064x}" for number in range(10, 13)
    ]
    assert page["next_cursor"] == 13
    assert page["qr_unique"] == 1000
