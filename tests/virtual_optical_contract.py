"""Public ECO1/UBUNTU wire-contract oracle; no private MCP source required.

Only checks the on-screen envelope, correlation token, Base32/Base64 and CRC32.
Never silently repairs a corrupted payload or claims success on OCR text.
"""
from __future__ import annotations
import re
import base64
import binascii
import json


def focus_valid(text: str) -> bool:
    first = str(text or "").casefold().splitlines()
    line = re.sub(r"\s+", " ", first[0] if first else "")
    return bool(
        re.search(r"easy\s*chang(?:e)?\b", line)
        and re.search(r"\bmachine\b", line)
        and re.search(r"\bhid\s*/\s*compac(?:t)?\b", line)
        and re.search(r"\bfocus\s*[:=]\s*c?ommand\b", line)
    )


def decode_lines(lines: list[str], expected_token: str) -> dict | None:
    cleaned=[re.sub(r"^EC[O0]1\b","ECO1",str(x).strip(),flags=re.I)
             for x in lines]
    header=None
    footer=None
    chunks={}
    for line in cleaned:
        match=re.fullmatch(
            r"ECO1 UBUNTU (\S+) (OK|ERR) (\d+)(?: ([B8]32))?",line,re.I)
        if match:
            if header is not None:return None
            header=match.groups()
            continue
        match=re.fullmatch(r"ECO1 END (\S+) ([0-9a-fA-F]{8})",line,re.I)
        if match:
            if footer is not None:return None
            footer=match.groups()
            continue
        match=re.fullmatch(r"ECO1 ([0-9OIl]{2}) ([A-Za-z0-9_-]+)",line,re.I)
        if match:
            index=match[1].upper().replace("O","0").replace("I","1").replace("L","1")
            if not index.isdigit():return None
            index=int(index)
            if index in chunks or not 0<=index<=31:return None
            chunks[index]=match[2]
    if header is None or footer is None:return None
    seq,status,number,codec=header
    if seq != expected_token or footer[0] != expected_token:return None
    number=int(number)
    if not 1<=number<=32 or set(chunks)!=set(range(number)):return None
    wire="".join(chunks[i] for i in range(number))
    try:
        if codec and codec.upper() in {"B32","832"}:
            wire=wire.upper().replace("0","O").replace("1","I").replace("8","B")
            if re.fullmatch(r"[A-Z2-7]+",wire) is None:return None
            raw=base64.b32decode(wire+"="*(-len(wire)%8),casefold=True)
        else:
            raw=base64.urlsafe_b64decode(wire+"="*(-len(wire)%4))
        if f"{binascii.crc32(raw)&0xffffffff:08X}" != footer[1].upper():
            return None
        text=raw.decode("utf8")
    except (binascii.Error,ValueError,UnicodeError):
        return None
    parts=text.split(maxsplit=5)
    if len(parts)<4 or parts[0]!="EC1" or parts[1]!=expected_token:
        return None
    if parts[2] != status.upper():return None
    result={"ok":parts[2]=="OK","sequence":expected_token,"text":text,
            "crc_verified":True,"data":{}}
    data_offset=text.find("{")
    if data_offset>=0:
        try:
            data=json.loads(text[data_offset:])
            if isinstance(data,dict):result["data"]=data
        except ValueError:pass
    return result
