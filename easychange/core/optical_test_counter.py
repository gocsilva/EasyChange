"""Test-only physical EC2 QR display accounting (no private payload retention).

The GUI increments only after assigning a pixmap to a visible QR QLabel.
Unique SHA-256 digests let the MCP compare local HDMI captures with the
remote display manifest without exporting command output or source text.
"""
from __future__ import annotations

from collections import Counter
from itertools import islice
import hashlib
import json
import re
import threading


class OpticalTestCounter:
    MAX_UNIQUE = 50000
    MAX_DISPLAYS = 500000

    def __init__(self):
        self._guard = threading.RLock()
        self.session_id = ""
        self.state = "IDLE"
        self.displays = 0
        self.pages = 0
        self._digests = Counter()
        self.overflow = False
        self.non_qr_displays = 0

    def start(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{16,40}", session_id):
            raise ValueError("INVALID_QR_TEST_SESSION")
        with self._guard:
            if self.state == "RUNNING":
                if self.session_id == session_id:
                    return self._summary()
                raise ValueError("QR_TEST_ALREADY_RUNNING")
            self.session_id = session_id
            self.state = "RUNNING"
            self.displays = 0
            self.pages = 0
            self._digests.clear()
            self.overflow = False
            self.non_qr_displays = 0
            return self._summary()

    def record(self, payloads: list[str]) -> None:
        with self._guard:
            if self.state != "RUNNING":
                return
            self.pages += 1
            for payload in payloads:
                if not isinstance(payload, str):
                    continue
                try:
                    envelope = json.loads(payload)
                except (ValueError, TypeError):
                    continue
                if not isinstance(envelope, dict) or envelope.get("p") not in {"EC2", "EC2H"}:
                    continue
                if self.displays >= self.MAX_DISPLAYS:
                    self.overflow = True
                    break
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if digest not in self._digests and len(self._digests) >= self.MAX_UNIQUE:
                    self.overflow = True
                    break
                self._digests[digest] += 1
                self.displays += 1

    def mark_non_qr(self, count: int = 1) -> None:
        with self._guard:
            if self.state == "RUNNING":
                self.non_qr_displays += max(0, int(count))

    def pause(self, session_id: str) -> dict:
        with self._guard:
            self._check_id(session_id)
            if self.state == "RUNNING":
                self.state = "PAUSED"
            return self._summary()

    def status(self, session_id: str, *, cursor: int = 0, limit: int = 64) -> dict:
        with self._guard:
            self._check_id(session_id)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("INVALID_QR_TEST_CURSOR")
            if type(limit) is not int or limit < 1 or limit > 128:
                raise ValueError("INVALID_QR_TEST_LIMIT")
            # Counter retains first-display insertion order. New QR digests
            # append instead of shifting previously issued cursor offsets.
            total = len(self._digests)
            part = list(islice(self._digests, cursor, cursor + limit))
            return {
                **self._summary(),
                "manifest": [{"sha256": key, "displays": self._digests[key]} for key in part],
                "next_cursor": cursor+len(part) if cursor+len(part) < total else None,
                "cursor": cursor,
            }

    def _check_id(self, session_id: str):
        if not self.session_id or self.session_id != session_id:
            raise ValueError("QR_TEST_SESSION_NOT_FOUND")

    def _summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "qr_display_events": self.displays,
            "qr_unique": len(self._digests),
            "pages_presented": self.pages,
            "overflow": self.overflow,
            "non_qr_displays": self.non_qr_displays,
            "counter_semantics": "QLabel pixmap assignment, not guaranteed HDMI delivery",
        }
