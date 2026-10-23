import math

import pytest

from easychange.core.maintenance_service import MaintenanceService


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"])
@pytest.mark.parametrize("active", [False, True])
def test_corrupt_nonfinite_receipt_retention_falls_back_to_safe_default(invalid, active):
    fallback = (MaintenanceService.ACTIVE_RESULT_TTL_SECONDS if active
                else MaintenanceService.RESULT_TTL_SECONDS)
    key = "active_retention_ttl_seconds" if active else "retention_ttl_seconds"
    result = MaintenanceService._result_retention_seconds({key: invalid}, active=active)
    assert math.isfinite(result)
    assert result == fallback


def test_valid_receipt_retention_override_still_works():
    assert MaintenanceService._result_retention_seconds({"retention_ttl_seconds": 1234}, active=False) == 1234
