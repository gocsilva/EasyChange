"""HTTP readiness has a total deadline, not a new deadline per candidate."""
from __future__ import annotations

from unittest.mock import patch

from easychange.core.runtime_service import ProjectRuntimeService


def test_http_readiness_stops_between_candidates_after_total_deadline():
    service=object.__new__(ProjectRuntimeService)
    current=[0.0]
    observed=[]

    def unavailable(url, *, timeout):
        observed.append((url,timeout))
        current[0]+=0.6
        return {"ok":False,"url":url,"error":"TimeoutError"}

    service._http_get=unavailable
    urls=[f"http://127.0.0.1:{5000+i}/swagger" for i in range(20)]
    with patch("easychange.core.runtime_service.time.monotonic",
               side_effect=lambda:current[0]), \
         patch("easychange.core.runtime_service.time.sleep") as sleep:
        result=service.wait_for_http(urls,timeout=1.0)

    assert result["error"]=="HTTP_STARTUP_TIMEOUT"
    assert len(observed)==2
    assert observed[0][1] == 1.0
    assert 0 < observed[1][1] <= 0.4
    sleep.assert_not_called()


def test_http_readiness_returns_successful_probe_without_waiting_for_more():
    service=object.__new__(ProjectRuntimeService)
    seen=[]
    def healthy(url, *, timeout):
        seen.append((url,timeout))
        return {"ok":True,"status":200,"url":url}
    service._http_get=healthy
    with patch("easychange.core.runtime_service.time.sleep") as sleep:
        result=service.wait_for_http(
            ["http://127.0.0.1:5000/health","http://127.0.0.1:5001/health"],
            timeout=3.0)
    assert result["status"]==200
    assert len(seen)==1
    sleep.assert_not_called()
