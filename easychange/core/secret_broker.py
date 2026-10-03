from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    text = str(value or "").strip()
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except Exception as exc:
        raise ValueError("INVALID_SECRET_ENVELOPE_BASE64") from exc


class SecretBroker:
    """Ephemeral encrypted host-secret ingress for EasyChange.

    The private key and decrypted values exist only in process memory. Public
    metadata is safe for optical transport. Imported plaintext is never returned
    and this class has no persistence API by design.
    """

    ALGORITHM = "X25519+HKDF-SHA256+AES-256-GCM"
    DEFAULT_TTL_SECONDS = 4 * 60 * 60
    MAX_SECRET_BYTES = 16 * 1024

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = max(300.0, min(24 * 60 * 60.0, float(ttl_seconds)))
        self._lock = threading.RLock()
        self._private: X25519PrivateKey
        self._broker_id = ""
        self._created_at = 0.0
        self._expires_at = 0.0
        self._values: dict[str, str] = {}
        self._rotate()

    def _rotate(self) -> None:
        self._private = X25519PrivateKey.generate()
        self._broker_id = "SB" + secrets.token_hex(8).upper()
        self._created_at = time.time()
        self._expires_at = self._created_at + self.ttl_seconds
        self._values.clear()

    def _ensure_live(self) -> None:
        if time.time() >= self._expires_at:
            self._rotate()

    def public_metadata(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_live()
            public = self._private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            return {
                "broker_id": self._broker_id,
                "algorithm": self.ALGORITHM,
                "public_key_b64": _b64e(public),
                "created_at": self._created_at,
                "expires_at": self._expires_at,
                "secret_values_returned": False,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_live()
            return {
                "broker_id": self._broker_id,
                "algorithm": self.ALGORITHM,
                "imported_sources": sorted(self._values),
                "count": len(self._values),
                "expires_at": self._expires_at,
                "secret_values_returned": False,
            }

    def import_envelope(
        self,
        *,
        broker_id: str,
        source_name: str,
        ephemeral_public_b64: str,
        nonce_b64: str,
        ciphertext_b64: str,
    ) -> dict[str, Any]:
        source = str(source_name or "").strip()
        if not _ENV_NAME_RE.fullmatch(source):
            raise ValueError("INVALID_SECRET_SOURCE_NAME")
        with self._lock:
            self._ensure_live()
            if str(broker_id or "") != self._broker_id:
                raise RuntimeError("SECRET_BROKER_EXPIRED_OR_ROTATED")

            peer_raw = _b64d(ephemeral_public_b64)
            nonce = _b64d(nonce_b64)
            ciphertext = _b64d(ciphertext_b64)
            if len(peer_raw) != 32 or len(nonce) != 12:
                raise ValueError("INVALID_SECRET_ENVELOPE")
            if len(ciphertext) > self.MAX_SECRET_BYTES + 64:
                raise ValueError("SECRET_ENVELOPE_TOO_LARGE")

            try:
                peer = X25519PublicKey.from_public_bytes(peer_raw)
                shared = self._private.exchange(peer)
                salt = hashlib.sha256(self._broker_id.encode("utf-8")).digest()
                info = (
                    "EasyChangeSecretBroker:"
                    + self._broker_id
                    + ":"
                    + source
                ).encode("utf-8")
                key = HKDF(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=salt,
                    info=info,
                ).derive(shared)
                aad = (self._broker_id + ":" + source).encode("utf-8")
                plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
            except Exception as exc:
                raise ValueError("SECRET_ENVELOPE_AUTH_FAILED") from exc

            if len(plaintext) > self.MAX_SECRET_BYTES:
                raise ValueError("SECRET_VALUE_TOO_LARGE")
            try:
                value = plaintext.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("SECRET_VALUE_UTF8_REQUIRED") from exc
            self._values[source] = value
            return {
                "broker_id": self._broker_id,
                "source_name": source,
                "imported": True,
                "expires_at": self._expires_at,
                "secret_values_returned": False,
            }

    def values(self) -> dict[str, str]:
        with self._lock:
            self._ensure_live()
            return dict(self._values)

    def clear(self) -> None:
        with self._lock:
            # Rotate instead of merely clearing so previously captured
            # ciphertext cannot be replayed into a later logical session.
            self._rotate()
