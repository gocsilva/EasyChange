"""Self-test and runtime must agree on the standalone service port contract."""
from __future__ import annotations
from unittest.mock import patch
import pytest
import mcp_adapter.module_service as service


@pytest.mark.parametrize("port",["0","-1","65536","1000000"])
@pytest.mark.parametrize("selftest",[False,True])
def test_invalid_port_fails_before_hardware_or_server(port,selftest):
    argv=["--port",port]+(["--self-test"] if selftest else [])
    with patch.object(service,"ThreadingHTTPServer",
                      side_effect=AssertionError("INVALID_SOCKET_BIND")):
        with pytest.raises(ValueError,match="INVALID_PORT"):
            service.main(argv)


def test_default_standalone_port_reports_valid_selftest(capsys):
    with patch.object(service,"module_status",return_value={"ok":True,"id":"service"}), \
         patch.object(service,"ThreadingHTTPServer",
                      side_effect=AssertionError("SELF_TEST_BOUND_PORT")):
        assert service.main(["--port","8784","--self-test"])==0
    assert capsys.readouterr().out.strip()
