"""Raw Kubernetes API reads must not expose secret objects through kubectl get."""
from __future__ import annotations

import pytest

from easychange.core.ubuntu_service import UbuntuError, UbuntuService


@pytest.mark.parametrize("argv", [
    ["get","--raw","/api/v1/namespaces/hml/secrets/private"],
    ["get","--raw=/api/v1/namespaces/hml/secrets"],
    ["get","--raw","/api/v1/namespaces/hml/%73ecrets/private"],
    ["get","--raw","/api/v1/namespaces/hml/%2573ecrets/private"],
    ["get","--raw","/api/v1/namespaces/hml/secrets%2Fprivate"],
])
def test_raw_secret_api_endpoint_cannot_bypass_value_guard(argv):
    with pytest.raises(UbuntuError,match="K8S_SECRET_VALUE_READ_FORBIDDEN"):
        UbuntuService._guard_secret_read(argv)


@pytest.mark.parametrize("argv", [
    ["get","--raw","/healthz"],
    ["get","--raw=/version"],
    ["get","--raw","/api"],
])
def test_raw_non_secret_health_endpoints_remain_available(argv):
    UbuntuService._guard_secret_read(argv)
