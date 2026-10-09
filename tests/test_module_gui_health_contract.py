"""Validate actual standalone GUI health function without importing tkinter."""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from urllib.request import HTTPRedirectHandler


def _source_and_status():
    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    tree=ast.parse(source)
    method=next(node for node in tree.body
                if isinstance(node,ast.FunctionDef) and node.name=="status")
    name=next(node.value.value for node in tree.body
              if isinstance(node,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=="MODULE_ID"
                      for t in node.targets))
    port=next(node.value.value for node in tree.body
              if isinstance(node,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=="PORT"
                      for t in node.targets))
    return source,method,name,port


def _probe(method, name, port, payload):
    reads=[]
    class FakeResponse(io.BytesIO):
        def read(self,size=-1):
            reads.append(size)
            return super().read(size)
    response=FakeResponse(payload)
    namespace={"json":json,"MODULE_ID":name,"PORT":port,
               "urlopen":lambda url,timeout: response}
    exec(compile(ast.Module(body=[method],type_ignores=[]),
                 "<standalone-gui-health>","exec"),namespace)
    return namespace["status"](), reads


def test_health_uses_bounded_read_and_requires_own_module_identity():
    _src, method, name, port=_source_and_status()
    for payload,expected in (
        ({"ok":True,"id":name},True),
        ({"ok":True,"id":"unrelated"},False),
        ({"ok":1,"id":name},False),
        ([{"ok":True,"id":name}],False),
    ):
        result,reads=_probe(method,name,port,json.dumps(payload).encode())
        assert result["running"] is expected
        assert reads==[2049]
    large=b'{"ok":true,"id":"' + name.encode() + b'","pad":"' + b"x"*2100 + b'"}'
    result,reads=_probe(method,name,port,large)
    assert result["running"] is False
    assert reads==[2049]


def test_gui_local_opener_disables_proxy_and_redirects():
    source,method,name,port=_source_and_status()
    assert "build_opener(ProxyHandler({}), _NoStandaloneRedirect()).open" in source
    node=next(item for item in ast.parse(source).body
              if isinstance(item,ast.ClassDef)
              and item.name=="_NoStandaloneRedirect")
    namespace={"HTTPRedirectHandler":HTTPRedirectHandler}
    exec(compile(ast.Module(body=[node],type_ignores=[]),
                 "<no-redirect>","exec"),namespace)
    no_redirect=namespace["_NoStandaloneRedirect"]()
    assert no_redirect.redirect_request(None,None,None,None,None,None) is None
