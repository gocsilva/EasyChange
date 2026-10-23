"""Kubectl compact -oFORMAT must not bypass secret value-read protection."""
from __future__ import annotations

import pytest

from easychange.core.ubuntu_service import UbuntuError, UbuntuService


@pytest.mark.parametrize("output", [
    "-ojson", "-oyaml", "-ojsonpath={.data}", "-ogo-template={{.data}}",
    "-otemplate={{.data}}", "-ocustom-columns=DATA:.data",
])
def test_compact_kubectl_secret_output_is_denied(output):
    with pytest.raises(UbuntuError,match="K8S_SECRET_VALUE_READ_FORBIDDEN"):
        UbuntuService._guard_secret_read(
            ["get", "secret", "private", output]
        )


@pytest.mark.parametrize("output", [
    "-owide", "-oname",
])
def test_compact_kubectl_secret_metadata_output_still_allowed(output):
    UbuntuService._guard_secret_read(["get","secret","private",output])
