"""Shared policy for EasyChange's existing operations over OPTICAL or GATT.

This module selects delivery, not business operations. No new EasyChange tool
names are needed. A GATT link requires verified application-layer encryption
and a live REMOTE_PC EasyChange session; mere BLE HID pairing is insufficient.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

MODES = frozenset(("AUTO", "OPTICAL", "PIXEL", "HYBRID", "GATT"))


class TransportUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Choice:
    transport: str
    reason: str
    background_safe: bool


class TransportRouter:
    """Fail-closed selection; never fall back to HID after an uncertain mutation."""

    def __init__(self, mode: str = "AUTO") -> None:
        self.mode = "AUTO"
        self.set_mode(mode)
        self._gatt_ready_at = 0.0
        self._gatt_authenticated = False
        self._gatt_session = ""
        self._gatt_latency = None
        self._optical_latency = None
        self._gatt_samples = 0
        self._optical_samples = 0

    def set_mode(self, mode: str) -> None:
        value = str(mode or "").strip().upper()
        if value not in MODES:
            raise ValueError("INVALID_EASYCHANGE_TRANSPORT_MODE")
        self.mode = value

    def gatt_heartbeat(self, *, authenticated: bool, session_id: str, now: float | None = None) -> None:
        instant = time.monotonic() if now is None else float(now)
        # Reject unverified/malformed session identities instead of coercing
        # None, bytes, whitespace and truthy non-boolean authentication.
        if authenticated is not True or not isinstance(session_id, str) or not session_id.strip():
            self.revoke_gatt()
            return
        self._gatt_authenticated = True
        self._gatt_ready_at = instant
        self._gatt_session = session_id

    def revoke_gatt(self) -> None:
        self._gatt_authenticated = False
        self._gatt_ready_at = 0.0
        self._gatt_session = ""

    def record_delivery(self, transport: str, duration_ms: float) -> None:
        if transport not in {"OPTICAL", "GATT"} or not 0 <= float(duration_ms) <= 600000:
            raise ValueError("INVALID_TRANSPORT_DELIVERY")
        attr = "_gatt_latency" if transport == "GATT" else "_optical_latency"
        sample_attr = "_gatt_samples" if transport == "GATT" else "_optical_samples"
        prior = getattr(self, attr)
        setattr(self, attr, float(duration_ms) if prior is None else .35 * float(duration_ms) + .65 * prior)
        setattr(self, sample_attr, getattr(self, sample_attr) + 1)

    def select(self, *, now: float | None = None, in_flight_mutation: bool = False) -> Choice:
        """Select only before dispatch. UNKNOWN mutation must reconcile same sequence."""
        if in_flight_mutation:
            raise TransportUnavailable("MUTATION_IN_FLIGHT_RECONCILE_ORIGINAL_SEQUENCE")
        instant = time.monotonic() if now is None else float(now)
        gatt_ready = bool(
            self._gatt_authenticated
            and self._gatt_session
            and 0 <= instant - self._gatt_ready_at <= 5.0
        )
        if self.mode in {"OPTICAL", "PIXEL", "HYBRID"}:
            return Choice("OPTICAL", "OPERATOR_FORCED_" + self.mode, False)
        if self.mode == "GATT":
            if not gatt_ready:
                raise TransportUnavailable("GATT_AUTHENTICATED_SESSION_REQUIRED")
            return Choice("GATT", "OPERATOR_FORCED_GATT", True)
        if not gatt_ready:
            return Choice("OPTICAL", "GATT_NOT_AUTHENTICATED_OR_STALE", False)
        if (
            self._gatt_samples >= 3 and self._optical_samples >= 3
            and self._gatt_latency is not None and self._optical_latency is not None
            and self._gatt_latency > self._optical_latency * 1.15
        ):
            return Choice("OPTICAL", "MEASURED_OPTICAL_FASTER", False)
        return Choice("GATT", "AUTHENTICATED_BACKGROUND_FAST_PATH", True)
