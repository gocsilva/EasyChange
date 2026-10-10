"""Module metadata polling should not repeatedly deserialize unchanged TOML."""
from __future__ import annotations

import os
from unittest.mock import patch

import mcp_adapter.integration as integration


def test_module_status_reuses_version_for_same_file_and_invalidates_on_change(tmp_path):
    manifest=tmp_path/"pyproject.toml"
    manifest.write_text('[project]\nversion = "1.0.0"\n',encoding="utf-8")
    try:
        with patch.object(integration,"ROOT",tmp_path):
            integration._project_version.cache_clear()
            with patch.object(integration.tomllib,"load",
                              wraps=integration.tomllib.load) as decoder:
                first=integration.module_status(esp_enabled=False)
                second=integration.module_status(esp_enabled=True)
                assert first["version"]==second["version"]=="1.0.0"
                assert first["esp_selected"] is False
                assert second["esp_selected"] is True
                assert decoder.call_count==1

                before=manifest.stat().st_mtime_ns
                manifest.write_text('[project]\nversion = "2.0.0"\n',
                                    encoding="utf-8")
                os.utime(manifest,ns=(before+2_000_000_000,
                                      before+2_000_000_000))
                changed=integration.module_status()
                assert changed["version"]=="2.0.0"
                assert decoder.call_count==2
    finally:
        integration._project_version.cache_clear()


def test_status_metadata_cache_is_bounded():
    assert integration._project_version.cache_info().maxsize==8


def test_missing_source_pyproject_keeps_packaged_module_status_readable(tmp_path):
    with patch.object(integration,"ROOT",tmp_path):
        result=integration.module_status(esp_enabled=True)
    assert result["installed"] is True
    assert result["version"]=="unknown"
    assert result["esp_selected"] is True


def test_oversized_project_toml_never_reaches_parser(tmp_path):
    manifest=tmp_path/"pyproject.toml"
    manifest.write_bytes(b"x"*(integration.MAX_PROJECT_METADATA_BYTES+1))
    try:
        integration._project_version.cache_clear()
        with patch.object(integration,"ROOT",tmp_path), \
             patch.object(integration.tomllib,"load",
                          side_effect=AssertionError("TOML_PARSE_TOO_LARGE")) as parse:
            try:
                integration.module_status()
            except ValueError as exc:
                assert str(exc)=="MODULE_PROJECT_METADATA_TOO_LARGE"
            else:
                raise AssertionError("Oversized project metadata accepted")
            parse.assert_not_called()
    finally:
        integration._project_version.cache_clear()
