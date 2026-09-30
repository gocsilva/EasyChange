import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from easychange.api.server import create_app


def test_api_shared_command_service(tmp_path):
    (tmp_path / "sample.txt").write_text("Hello", encoding="utf-8")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/state").json()["ok"] is True
        result = client.post("/api/command", json={"command": ":search Hello"})
        assert result.status_code == 200
        assert result.json()["data"]["matches"][0]["file"] == "sample.txt"
        assert client.get("/api/files/sample.txt").json()["lines"] == ["1|Hello"]
        edit = client.put("/api/files/sample.txt", json={"content": "updated"})
        assert edit.status_code == 200
        assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "updated"
