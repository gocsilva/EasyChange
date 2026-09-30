from collections import defaultdict


class IdFactory:
    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int, counts or {})

    def next(self, prefix: str) -> str:
        self._counts[prefix] += 1
        return f"{prefix}{self._counts[prefix]}"

    def counts(self) -> dict[str, int]:
        return dict(self._counts)
