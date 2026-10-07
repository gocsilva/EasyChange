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



def test_capabilities_publish_adaptive_optical_transport_contract(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        manifest = service.execute(":capabilities --compact").data["capability_manifest"]
        assert manifest["feature_flags"]["adaptive_optical_dwell"] is True
        assert manifest["feature_flags"]["mcp_optical_tuning"] is True
        assert manifest["optical_transport"]["adaptive_enabled"] is True
        assert manifest["optical_transport"]["consumer_contract"]["mcp_should_tune"] is True
    finally:
        service.close()


def test_structured_optical_transport_configuration_persists(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        configured = service._execute_structured({
            "op": "operation",
            "type": "optical_transport_configure",
            "adaptive_enabled": True,
            "min_ms": 650,
            "max_ms": 1700,
            "current_ms": 1050,
            "step_ms": 125,
            "adjusted_by": "mcp_auto",
        })
        assert configured.ok is True
        assert configured.data["current_ms"] == 1050
        queried = service._execute_structured({
            "op": "operation",
            "type": "optical_transport_get",
        })
        assert queried.ok is True
        assert queried.data["min_ms"] == 650
        assert queried.data["max_ms"] == 1700
        assert queried.data["last_adjusted_by"] == "mcp_auto"
    finally:
        service.close()

    reopened = CommandService(Workspace.open(tmp_path))
    try:
        assert reopened.optical_transport_config()["current_ms"] == 1050
    finally:
        reopened.close()



def test_capabilities_explain_safe_result_rechunking(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        manifest = service.execute(":capabilities --compact").data["capability_manifest"]
        assert manifest["feature_flags"]["adaptive_result_rechunking"] is True
        contract = manifest["result_chunk_recovery_contract"]
        assert contract["adaptive_rechunking"] is True
        assert contract["index_geometry_depends_on_chunk_bytes"] is True
        assert manifest["max_result"]["logical_chunk_bytes_min"] == 256
        assert manifest["max_result"]["logical_chunk_bytes_max"] == 4096
        assert manifest["max_result"]["logical_chunk_rechunkable"] is True
    finally:
        service.close()
