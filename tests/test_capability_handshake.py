from __future__ import annotations

from easychange.core.command_service import CommandService
from easychange.core.operation_registry import structured_capabilities
from easychange.core.workspace import Workspace


def test_capability_manifest_matches_actual_structured_registry(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(":capabilities --compact")
        assert result.ok is True
        manifest = result.data["capability_manifest"]
        expected = set(structured_capabilities()["operations"])
        assert set(manifest["supported_operations"]) == expected
        assert manifest["unsupported_operations"] == []
        assert manifest["capability_version"] >= 2
        assert manifest["protocol_version"] == "EC2"
        assert manifest["structured_protocol_version"] == "easychange.structured/2"
        assert manifest["long_text_support"] is True
        assert manifest["structured_payload_support"] is True
        assert manifest["max_payload"]["decoded_command_bytes"] == 262144
        assert len(manifest["capabilities_sha256"]) == 64
    finally:
        service.close()


def test_health_reuses_same_capability_fingerprint(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        caps = service.execute(":capabilities --compact").data["capability_manifest"]
        health = service.execute(":health").data["capability_manifest"]
        assert caps["capabilities_sha256"] == health["capabilities_sha256"]
        assert caps["supported_operations"] == health["supported_operations"]
    finally:
        service.close()



def test_capability_manifest_publishes_operation_safety_profiles(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(":capabilities --compact")
        assert result.ok is True
        manifest = result.data["capability_manifest"]
        profiles = manifest["operation_profiles"]
        assert manifest["feature_flags"]["operation_profiles"] is True
        assert profiles["read"]["family"] == "read"
        assert profiles["read"]["side_effect"] is False
        assert profiles["write_file"]["mutation"] is True
        assert profiles["browser_navigate"]["side_effect"] is True
        assert profiles["db_query"]["conditional_side_effect"] is True
    finally:
        service.close()
