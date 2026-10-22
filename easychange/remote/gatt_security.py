"""Authenticated payload envelope for optional EasyChange BLE GATT communication.

The ESP is a byte relay; it never needs to inspect source code or know the
application session key. An unconfirmed pairing cannot carry instructions.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MAGIC = b"ECG1"
MAX_PLAINTEXT = 1024 * 1024
ROLES = {"host": 0, "remote": 1}


class GattSecurityError(ValueError):
    pass


@dataclass
class GattHandshake:
    """Fresh X25519 handshake; require an out-of-band short-code comparison."""
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise GattSecurityError("INVALID_ROLE")
        self._private = X25519PrivateKey.generate()
        self.public_key = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._root: bytes | None = None
        self._session_id: bytes | None = None
        self._sas: str | None = None
        self._used = False

    def receive_peer(self, peer_public_key: bytes) -> str:
        if self._root is not None:
            raise GattSecurityError("HANDSHAKE_ALREADY_USED")
        if not isinstance(peer_public_key, bytes) or len(peer_public_key) != 32:
            raise GattSecurityError("INVALID_PEER_KEY")
        if peer_public_key == self.public_key:
            raise GattSecurityError("IDENTICAL_PEER_KEY")
        peer = X25519PublicKey.from_public_bytes(peer_public_key)
        ordered = sorted((self.public_key, peer_public_key))
        context = hashlib.sha256(MAGIC + ordered[0] + ordered[1]).digest()
        shared = self._private.exchange(peer)
        self._root = HKDF(algorithm=hashes.SHA256(), length=32, salt=context, info=b"easychange-gatt-ecdh-v1").derive(shared)
        self._session_id = hashlib.sha256(b"ecg-session" + context).digest()[:8]
        proof = hmac.digest(self._root, b"easychange-gatt-human-verification", "sha256")
        self._sas = f"{int.from_bytes(proof[:4], 'big') % 1000000:06d}"
        return self._sas

    def approve(self, observed_code: str) -> "GattSecureSession":
        if self._used or self._root is None or self._session_id is None or self._sas is None:
            raise GattSecurityError("HANDSHAKE_NOT_READY")
        if not isinstance(observed_code, str) or not hmac.compare_digest(observed_code, self._sas):
            raise GattSecurityError("PAIRING_PROOF_MISMATCH")
        self._used = True
        return GattSecureSession(self.role, self._root, self._session_id)


class GattSecureSession:
    """Direction-bound AES-256-GCM; enforces unique nonces and strictly increasing RX IDs."""
    def __init__(self, role: str, root: bytes, session_id: bytes) -> None:
        if role not in ROLES or not isinstance(root, bytes) or len(root) != 32 or len(session_id) != 8:
            raise GattSecurityError("INVALID_SESSION")
        self.role = role
        self.session_id = session_id
        self._keys = {
            name: AESGCM(HKDF(
                algorithm=hashes.SHA256(), length=32, salt=session_id,
                info=b"easychange-gatt-aesgcm-v1-" + name.encode("ascii"),
            ).derive(root))
            for name in ROLES
        }
        self._tx_sequence = 0
        self._rx_sequence = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        if not isinstance(plaintext, bytes) or len(plaintext) > MAX_PLAINTEXT:
            raise GattSecurityError("MESSAGE_TOO_LARGE")
        if self._tx_sequence >= (1 << 64) - 1:
            raise GattSecurityError("SEQUENCE_EXHAUSTED")
        self._tx_sequence += 1
        counter = self._tx_sequence.to_bytes(8, "big")
        header = MAGIC + bytes([ROLES[self.role]]) + self.session_id + counter
        return header + self._keys[self.role].encrypt(b"\x00" * 4 + counter, plaintext, header)

    def decrypt(self, frame: bytes) -> bytes:
        if not isinstance(frame, bytes) or len(frame) < 4 + 1 + 8 + 8 + 16:
            raise GattSecurityError("FRAME_INVALID")
        if frame[:4] != MAGIC or frame[5:13] != self.session_id:
            raise GattSecurityError("SESSION_MISMATCH")
        opposite = "remote" if self.role == "host" else "host"
        if frame[4] != ROLES[opposite]:
            raise GattSecurityError("DIRECTION_MISMATCH")
        counter = frame[13:21]
        sequence = int.from_bytes(counter, "big")
        if sequence <= self._rx_sequence:
            raise GattSecurityError("REPLAY_REJECTED")
        if len(frame) > MAX_PLAINTEXT + 37:
            raise GattSecurityError("FRAME_TOO_LARGE")
        try:
            decoded = self._keys[opposite].decrypt(b"\x00" * 4 + counter, frame[21:], frame[:21])
        except Exception as exc:
            raise GattSecurityError("AUTHENTICATION_FAILED") from exc
        self._rx_sequence = sequence
        return decoded
