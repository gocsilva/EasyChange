from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from easychange.core.command_service import CommandService
from easychange.core.secret_broker import SecretBroker
from easychange.core.workspace import Workspace


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _envelope(public: dict, source_name: str, plaintext: str) -> dict:
    ephemeral = X25519PrivateKey.generate()
    peer = X25519PublicKey.from_public_bytes(_b64d(public["public_key_b64"]))
    shared = ephemeral.exchange(peer)
    broker_id = public["broker_id"]
    salt = hashlib.sha256(broker_id.encode("utf-8")).digest()
    info = f"EasyChangeSecretBroker:{broker_id}:{source_name}".encode("utf-8")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(shared)
    nonce = os.urandom(12)
    aad = f"{broker_id}:{source_name}".encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "broker_id": broker_id,
        "source_name": source_name,
        "ephemeral_public_b64": _b64e(ephemeral_public),
        "nonce_b64": _b64e(nonce),
        "ciphertext_b64": _b64e(ciphertext),
    }


def _primary(result):
    assert result.ok is True, (result.code, result.error, result.data)
    data = result.data or {}
    rows = data.get("results") or []
    assert rows and rows[0].get("ok") is True
    return rows[0].get("data") or {}


def test_secret_broker_authenticated_envelope_never_returns_plaintext():
    broker = SecretBroker()
    public = broker.public_metadata()
    secret = "SENTINEL-DB-PASSWORD-123!@#"
    envelope = _envelope(public, "ACAM_HML_DB_PASSWORD", secret)

    imported = broker.import_envelope(**envelope)
    status = broker.status()

    assert imported["imported"] is True
    assert imported["secret_values_returned"] is False
    assert status["imported_sources"] == ["ACAM_HML_DB_PASSWORD"]
    assert status["secret_values_returned"] is False
    assert secret not in json.dumps(imported, ensure_ascii=False)
    assert secret not in json.dumps(status, ensure_ascii=False)
    # Internal access is intentionally available only to runtime/database
    # providers inside the EasyChange process.
    assert broker.values()["ACAM_HML_DB_PASSWORD"] == secret

    tampered = dict(envelope)
    raw = bytearray(_b64d(tampered["ciphertext_b64"]))
    raw[-1] ^= 0x01
    tampered["ciphertext_b64"] = _b64e(bytes(raw))
    with pytest.raises(ValueError, match="SECRET_ENVELOPE_AUTH_FAILED"):
        broker.import_envelope(**tampered)

    old_broker_id = public["broker_id"]
    broker.clear()
    assert broker.public_metadata()["broker_id"] != old_broker_id
    with pytest.raises(RuntimeError, match="EXPIRED_OR_ROTATED"):
        broker.import_envelope(**envelope)


def test_browser_operation_uses_secret_ref_and_scrubs_result(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    service = CommandService(Workspace.open(tmp_path))
    secret = "BROWSER-SECRET-TOKEN-123456"
    source = "BROWSER_TEST_TOKEN"
    try:
        public = _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_public",
        }))
        _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import",
            **_envelope(public, source, secret),
        }))

        captured = {}

        def fake_cdp_call(
            session_id,
            method,
            params,
            *,
            channel="page",
            timeout=10.0,
            include_events=True,
        ):
            captured["session_id"] = session_id
            captured["method"] = method
            captured["params"] = params
            captured["channel"] = channel
            assert params["headers"]["Authorization"] == "Bearer " + secret
            return {
                "schema": "easychange.browser-cdp/1",
                "result": {
                    "echo": secret,
                    "authorization": "Bearer " + secret,
                },
                "events": [],
            }

        monkeypatch.setattr(service.browser, "cdp_call", fake_cdp_call)
        result = service._execute_browser_operation({
            "type": "browser_cdp_call",
            "session_id": "S1",
            "method": "Network.setExtraHTTPHeaders",
            "channel": "page",
            "params": {
                "headers": {
                    "Authorization": "Bearer secret://BROWSER_TEST_TOKEN",
                }
            },
        })

        assert result.ok is True
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        assert secret not in payload
        assert result.data["secret_refs_used"] == [source]
        assert result.data["secret_values_returned"] is False
        assert captured["method"] == "Network.setExtraHTTPHeaders"
        assert captured["channel"] == "page"
    finally:
        service.close()
def test_mcp_host_env_reaches_runtime_only_through_encrypted_broker(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    secret = "SENTINEL-NEVER-PERSIST-987654321"
    source = "ACAM_HML_DB_PASSWORD"

    service = CommandService(Workspace.open(tmp_path))
    try:
        public = _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_public",
        }))
        envelope = _envelope(public, source, secret)
        imported = _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import",
            **envelope,
        }))
        assert imported["imported"] is True
        assert secret not in json.dumps(imported, ensure_ascii=False)

        configured = service.runtime.configure_profile(
            "secure-runtime",
            kind="custom",
            test_argv=[
                sys.executable,
                "-c",
                (
                    "import os;"
                    "print('host-secret-present=' + "
                    "str(bool(os.environ.get('DB_PASSWORD'))))"
                ),
            ],
            env_refs={"DB_PASSWORD": f"MCP_HOST_ENV:{source}"},
            inherit_env=False,
        )
        profile = service.runtime.profile("secure-runtime")
        assert profile["env_refs"]["DB_PASSWORD"] == f"MCP_HOST_ENV:{source}"
        assert secret not in json.dumps(configured, ensure_ascii=False)
        assert secret not in json.dumps(profile, ensure_ascii=False)

        tested = service.runtime.smart_test("secure-runtime", timeout=30)
        payload = json.dumps(tested, ensure_ascii=False)
        assert tested["passed"] is True
        assert "host-secret-present=True" in payload
        assert secret not in payload

        status = _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_status",
        }))
        assert status["imported_sources"] == [source]
        assert secret not in json.dumps(status, ensure_ascii=False)

        # The plaintext must not appear in any EasyChange workspace artifact.
        owned = tmp_path / ".easychange"
        for file in owned.rglob("*"):
            if file.is_file():
                try:
                    raw = file.read_bytes()
                except OSError:
                    continue
                assert secret.encode("utf-8") not in raw, file
    finally:
        service.close()


def test_plaintext_external_env_over_ec1_is_rejected_and_not_persisted(tmp_path):
    secret = "PLAINTEXT-MUST-NOT-CROSS-HID"
    service = CommandService(Workspace.open(tmp_path))
    try:
        operation = {
            "op": "operation",
            "type": "runtime_profiles",
            "_external_env": {"ACAM_HML_DB_PASSWORD": secret},
        }
        raw = ":ec QSECREJECT1 :j1 " + json.dumps(
            operation, ensure_ascii=False, separators=(",", ":")
        )
        result = service.execute(raw)

        assert result.ok is False
        assert result.code == "INVALID_STRUCTURED_PAYLOAD"
        assert "PLAINTEXT_EXTERNAL_ENV_FORBIDDEN" in (result.error or "")
        assert secret not in json.dumps(result.to_dict(), ensure_ascii=False)

        owned = tmp_path / ".easychange"
        for file in owned.rglob("*"):
            if file.is_file():
                try:
                    assert secret.encode("utf-8") not in file.read_bytes(), file
                except OSError:
                    pass
    finally:
        service.close()


def test_core_packaging_declares_secret_broker_crypto_dependency():
    pyproject = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert 'cryptography>=42' in pyproject


def test_streaming_redactor_masks_value_split_across_chunks(tmp_path):
    from easychange.core.process_service import _stream_redacted

    marker = b"ephemeral-boundary-marker"
    chunks = [b"A" * 4095 + marker[:3], marker[3:] + b"Z", b""]

    class Source:
        def __init__(self):
            self.closed = False

        def read(self, _size):
            return chunks.pop(0)

        def close(self):
            self.closed = True

    source = Source()
    target = tmp_path / "redacted.out"
    _stream_redacted(source, target, [marker.decode("utf-8")])

    raw = target.read_bytes()
    assert marker not in raw
    assert b"[REDACTED]" in raw
    assert raw.startswith(b"A" * 4095)
    assert raw.endswith(b"Z")
    assert source.closed is True


def test_async_runtime_job_never_persists_unredacted_broker_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    marker = "ephemeral-async-marker-246813579"
    source_name = "BMO_TEST_EPHEMERAL_ASYNC"

    service = CommandService(Workspace.open(tmp_path))
    try:
        public = _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_public",
        }))
        envelope = _envelope(public, source_name, marker)
        _primary(service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import",
            **envelope,
        }))

        service.runtime.configure_profile(
            "async-secure-runtime",
            kind="custom",
            test_argv=[
                sys.executable,
                "-c",
                (
                    "import os;"
                    "print('A'*4095 + os.environ.get('RUNTIME_VALUE',''))"
                ),
            ],
            env_refs={"RUNTIME_VALUE": f"MCP_HOST_ENV:{source_name}"},
            inherit_env=False,
        )

        started = service.runtime.start_test_job(
            "JSECREDACT1",
            "async-secure-runtime",
        )
        assert started["state"] == "RUNNING"

        deadline = time.time() + 10
        result = service.runtime.test_job_result("JSECREDACT1")
        while not result.get("finished") and time.time() < deadline:
            time.sleep(0.03)
            result = service.runtime.test_job_result("JSECREDACT1")

        assert result["finished"] is True
        assert result["passed"] is True

        raw_path = service.processes.root / "JSECREDACT1.out"
        raw = raw_path.read_bytes()
        assert marker.encode("utf-8") not in raw
        assert b"[REDACTED]" in raw

        durable = service.processes.job_result("JSECREDACT1")
        rendered = json.dumps(durable, ensure_ascii=False)
        assert marker not in rendered
        assert "[REDACTED]" in rendered
    finally:
        service.close()



def test_workspace_secret_source_import_never_returns_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    raw_secret = "Server=hml-db;Database=CAMBIO_ACAMS;Password=NeverExposeThis;"
    (tmp_path / "dbhml.txt").write_text(raw_secret + "\n", encoding="utf-8")
    (tmp_path / "appsettings.HML.json").write_text(
        json.dumps({
            "Bs2HttpConfigurationOptions": {
                "ApiKeyAuthorization": {
                    "ApiKey": "PROJECT-API-KEY-SECRET-123"
                }
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / ".env.test").write_text(
        "OTHER=value\nTARGET_TOKEN='ENV-SECRET-456'\n",
        encoding="utf-8",
    )

    service = CommandService(Workspace.open(tmp_path))
    try:
        raw = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "ACAM_HML_CONNECTION",
            "path": "dbhml.txt",
            "source_format": "raw",
        })
        js = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "ACAM_API_KEY",
            "path": "appsettings.HML.json",
            "source_format": "json",
            "key": "Bs2HttpConfigurationOptions.ApiKeyAuthorization.ApiKey",
        })
        env = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "ENV_TOKEN",
            "path": ".env.test",
            "source_format": "env",
            "key": "TARGET_TOKEN",
        })

        assert raw.ok is True
        assert js.ok is True
        assert env.ok is True
        rendered = json.dumps(
            [raw.to_dict(), js.to_dict(), env.to_dict()],
            ensure_ascii=False,
        )
        assert raw_secret not in rendered
        assert "PROJECT-API-KEY-SECRET-123" not in rendered
        assert "ENV-SECRET-456" not in rendered
        assert "secret_values_returned" in rendered

        values = service.secret_broker.values()
        assert values["ACAM_HML_CONNECTION"] == raw_secret
        assert values["ACAM_API_KEY"] == "PROJECT-API-KEY-SECRET-123"
        assert values["ENV_TOKEN"] == "ENV-SECRET-456"

        owned = tmp_path / ".easychange"
        for file in owned.rglob("*"):
            if not file.is_file():
                continue
            payload = file.read_bytes()
            assert raw_secret.encode("utf-8") not in payload, file
            assert b"PROJECT-API-KEY-SECRET-123" not in payload, file
            assert b"ENV-SECRET-456" not in payload, file
    finally:
        service.close()


def test_workspace_secret_source_is_workspace_scoped_and_scalar_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    (tmp_path / "nested.json").write_text(
        '{"credential":{"object":{"not":"scalar"}}}',
        encoding="utf-8",
    )

    service = CommandService(Workspace.open(tmp_path))
    try:
        escaped = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "ESCAPED_SECRET",
            "path": "../outside-secret.txt",
            "source_format": "raw",
        })
        nonscalar = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "OBJECT_SECRET",
            "path": "nested.json",
            "source_format": "json",
            "key": "credential.object",
        })

        assert escaped.ok is False
        assert "Path escapes workspace" in str(escaped.error)
        assert nonscalar.ok is False
        assert "SECRET_SOURCE_VALUE_MUST_BE_SCALAR" in str(nonscalar.error)
        assert "OUTSIDE-SECRET" not in json.dumps(escaped.to_dict())
        assert service.secret_broker.status()["count"] == 0
    finally:
        service.close()


def test_workspace_secret_source_can_feed_runtime_env_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    marker = "REMOTE-LOCAL-SECRET-789"
    (tmp_path / "runtime.env").write_text(
        "ACAM_PASSWORD=" + marker + "\n",
        encoding="utf-8",
    )
    service = CommandService(Workspace.open(tmp_path))
    try:
        imported = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "ACAM_PASSWORD",
            "path": "runtime.env",
            "source_format": "env",
            "key": "ACAM_PASSWORD",
        })
        assert imported.ok is True

        service.runtime.configure_profile(
            "workspace-secret-runtime",
            kind="custom",
            test_argv=[
                sys.executable,
                "-c",
                (
                    "import os;"
                    "print('secret-present=' + "
                    "str(bool(os.environ.get('PASSWORD'))))"
                ),
            ],
            env_refs={"PASSWORD": "MCP_HOST_ENV:ACAM_PASSWORD"},
            inherit_env=False,
        )
        tested = service.runtime.smart_test("workspace-secret-runtime", timeout=30)
        rendered = json.dumps(tested, ensure_ascii=False)

        assert tested["passed"] is True
        assert "secret-present=True" in rendered
        assert marker not in rendered
    finally:
        service.close()



def test_secret_source_path_is_persistently_excluded_from_index(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    secret = "SENSITIVE-INDEX-PAYLOAD-123456"
    source = tmp_path / "private-source.txt"
    source.write_text(secret, encoding="utf-8")

    service = CommandService(Workspace.open(tmp_path))
    try:
        imported = service._execute_structured({
            "op": "operation",
            "type": "secret_broker_import_source",
            "source_name": "PRIVATE_SOURCE",
            "path": "private-source.txt",
            "source_format": "raw",
        })
        assert imported.ok is True
        service.indexer.refresh(force=True)
        assert "private-source.txt" in service.indexer.protected_paths()
        assert all(
            item["path"] != "private-source.txt"
            for item in service.indexer.files(limit=1000)
        )
    finally:
        service.close()

    session = json.loads(
        (tmp_path / ".easychange" / "session.json").read_text(encoding="utf-8")
    )
    assert "private-source.txt" in session["sensitive_source_paths"]

    reopened = CommandService(Workspace.open(tmp_path))
    try:
        reopened.indexer.refresh(force=True)
        assert "private-source.txt" in reopened.indexer.protected_paths()
        assert all(
            item["path"] != "private-source.txt"
            for item in reopened.indexer.files(limit=1000)
        )
        for file in (tmp_path / ".easychange").rglob("*"):
            if file.is_file():
                assert secret.encode("utf-8") not in file.read_bytes()
    finally:
        reopened.close()
