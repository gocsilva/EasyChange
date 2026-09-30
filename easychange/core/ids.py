from collections import defaultdict


class IdFactory:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)

    def next(self, prefix: str) -> str:
        self._counts[prefix] += 1
        return f"{prefix}{self._counts[prefix]}"
