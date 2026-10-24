"""Remote-main freshness and fail-closed guards for the virtual lab launcher."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_MODULE=Path(__file__).with_name("fresh_main_virtual_lab.py")
_SPEC=importlib.util.spec_from_file_location("fresh_virtual_lab",_MODULE)
assert _SPEC and _SPEC.loader
lab=importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lab)


def test_verified_remote_main_needs_matching_fetched_commit(tmp_path,monkeypatch):
    recorded=[]
    def mockgit(path,*args,**kwargs):
        recorded.append(args)
        if args[0:3]==("remote","get-url","origin"):
            return "https://github.com/gocsilva/EasyChange.git"
        if args[0]=="fetch":return ""
        if args[0]=="rev-parse":return "a"*40
        if args[0]=="ls-remote":return "a"*40+"  refs/heads/main"
        raise AssertionError(args)
    monkeypatch.setattr(lab,"git",mockgit)
    assert lab.remote_main(tmp_path,"gocsilva/EasyChange")=="a"*40
    assert any(parts[0]=="fetch" for parts in recorded)
    assert any(parts[0]=="ls-remote" for parts in recorded)


def test_changed_remote_main_fails_closed(tmp_path,monkeypatch):
    def mockgit(path,*args,**kwargs):
        if args[0]=="remote":return "https://github.com/gocsilva/EasyChange.git"
        if args[0]=="fetch":return ""
        if args[0]=="rev-parse":return "a"*40
        if args[0]=="ls-remote":return "b"*40+"  refs/heads/main"
        raise AssertionError(args)
    monkeypatch.setattr(lab,"git",mockgit)
    with pytest.raises(RuntimeError,match="REMOTE_MAIN_MOVED"):
        lab.remote_main(tmp_path,"gocsilva/EasyChange")


def test_foreign_origin_is_refused_before_fetch(tmp_path,monkeypatch):
    calls=[]
    def mockgit(path,*args,**kwargs):
        calls.append(args)
        return "https://github.com/unknown/Other.git"
    monkeypatch.setattr(lab,"git",mockgit)
    with pytest.raises(RuntimeError,match="UNEXPECTED_REMOTE_ORIGIN"):
        lab.remote_main(tmp_path,"gocsilva/EasyChange")
    assert calls==[("remote","get-url","origin")]


def test_manual_source_overrides_are_forbidden(monkeypatch):
    monkeypatch.setattr(sys,"argv",["lab","--mcp-source","C:\\stale-copy"])
    with pytest.raises(RuntimeError,match="Pinned sources are mandatory"):
        lab.launch()


def test_all_required_repositories_are_tracked():
    assert set(lab.REPOSITORIES)=={
        "EasyChange","MCP","ESP","BMO_LLM_CONTROLLER"
    }
