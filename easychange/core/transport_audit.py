"""Durable EasyChange optical command/receipt journal (redacted, bounded).
Never store commands, source code, secrets or decoded result payloads in logs.
Stored on the REMOTE_PC; may be read via :transport_log or structured type
transport_audit, both through existing EC1/HDMI without added network services.
"""
from __future__ import annotations
from collections import deque
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,math,os,threading,time

ROOT=Path(os.environ.get("LOCALAPPDATA") or Path.home())/"EasyChange"/"logs"
FILE=ROOT/"transport_audit.jsonl"
_LOCK=threading.RLock()
_MAX_FILE=2*1024*1024
SAFE_FIELDS=frozenset(("stage","sequence","direction","medium","mode","command_type",
    "attempt","ok","cause","code","state","result_state","duplicate","duration_ms",
    "elapsed_ms","bytes","sha256","frames","packets","chunks","page","pages",
    "generation","result_complete","payload_length","render_ms","encoding_ms"))

def signature(value):
    raw=value if isinstance(value,bytes) else str(value or "").encode("utf-8","replace")
    return {"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}

def log(stage,sequence="",direction="",**kwargs):
    rec={"id":time.time_ns(),"at":datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
         "stage":str(stage)[:64],"sequence":str(sequence or "")[:64],
         "direction":str(direction or "")[:40]}
    for k,v in kwargs.items():
        if k not in SAFE_FIELDS or k in {"stage","sequence","direction"}:continue
        if isinstance(v,float) and not math.isfinite(v):
            continue
        if v is None or isinstance(v,(bool,int,float)):rec[k]=v
        elif isinstance(v,str):
            if k=="sha256" and len(v)==64 and all(ch in "0123456789abcdef" for ch in v.lower()):
                rec[k]=v.lower()
            elif k in {"cause","code"}:rec[k]="".join(c if c.isalnum() or c in "._-" else "_" for c in v)[:80]
            elif k in {"medium","mode","command_type","state","result_state"}:rec[k]=v[:65]
    try:
        with _LOCK:
            ROOT.mkdir(parents=True,exist_ok=True)
            line=json.dumps(rec,ensure_ascii=True,separators=(",",":"),allow_nan=False)
            if len(line)>1800:return
            if FILE.exists() and FILE.stat().st_size+len(line)>_MAX_FILE:
                old2=FILE.with_suffix(".jsonl.2")
                if old2.exists():old2.unlink()
                old1=FILE.with_suffix(".jsonl.1")
                if old1.exists():old1.replace(old2)
                FILE.replace(old1)
            with FILE.open("a",encoding="utf-8") as out:out.write(line+"\n")
    except OSError:pass

def _safe_snapshot_entry(entry):
    """Validate persisted event types before exposing data to diagnostics."""
    text_limits = {
        "at": 40, "stage": 64, "sequence": 64, "direction": 40,
        "medium": 65, "mode": 65, "command_type": 65,
        "state": 65, "result_state": 65, "cause": 80, "code": 80,
    }
    if (type(entry.get("id")) is not int or entry["id"] <= 0
            or any(type(entry.get(key)) is not str
                   or len(entry[key]) > maximum
                   for key, maximum in (("at", 40), ("stage", 64),
                                        ("sequence", 64), ("direction", 40)))):
        return None

    clean = {}
    for key, value in entry.items():
        if key not in SAFE_FIELDS and key not in {"id", "at", "stage", "sequence", "direction"}:
            continue
        if key == "id":
            clean[key] = entry["id"]
        elif key in text_limits:
            if type(value) is str and len(value) <= text_limits[key]:
                if key in {"cause", "code"}:
                    value = "".join(c if c.isalnum() or c in "._-" else "_" for c in value)
                clean[key] = value
        elif key in {"ok", "duplicate", "result_complete"}:
            if type(value) is bool:
                clean[key] = value
        elif key == "sha256":
            if (type(value) is str and len(value) == 64
                    and all(ch in "0123456789abcdef" for ch in value.lower())):
                clean[key] = value.lower()
        elif key in {"duration_ms", "elapsed_ms", "bytes", "frames", "packets",
                     "chunks", "page", "pages", "generation", "payload_length",
                     "render_ms", "encoding_ms", "attempt"}:
            if (type(value) in (int, float) and math.isfinite(value)
                    and abs(value) <= 1_000_000_000_000):
                clean[key] = value
    return clean


def snapshot(after=0,limit=8,sequence="",direction=""):
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=16:
        raise ValueError("INVALID_TRANSPORT_LOG_CURSOR_OR_LIMIT")
    if not isinstance(sequence,str) or len(sequence)>64 or not isinstance(direction,str) or len(direction)>40:
        raise ValueError("INVALID_TRANSPORT_LOG_FILTER")
    rows=deque(maxlen=2000)
    with _LOCK:
        for p in (FILE.with_suffix(".jsonl.2"),FILE.with_suffix(".jsonl.1"),FILE):
            try:
                with p.open("r",encoding="utf-8") as source:rows.extend(source)
            except OSError:pass
    found=[]
    for line in rows:
        try:entry=json.loads(line)
        except (TypeError,ValueError):continue
        if not isinstance(entry,dict):
            continue
        identifier=entry.get("id")
        record_sequence=entry.get("sequence","")
        record_direction=entry.get("direction","")
        if (type(identifier) is not int or identifier<=0
                or not isinstance(record_sequence,str)
                or not isinstance(record_direction,str)):
            continue
        if (identifier<=after or
            (sequence and record_sequence.upper()!=sequence.upper()) or
            (direction and record_direction.lower()!=direction.lower())):
            continue
        # Disk content may originate from an older release or be malformed.
        # The EC2 query is a security boundary: never forward arbitrary
        # persisted keys such as command bodies, tokens or source code.
        safe_entry = _safe_snapshot_entry(entry)
        if safe_entry is not None:
            found.append(safe_entry)
    chosen=found[:limit] if after else found[-limit:]
    return {"events":chosen,"next_cursor":chosen[-1]["id"] if chosen else after,
            "more":bool(after and len(found)>limit),"count":len(chosen),
            "source":"EASYCHANGE_REMOTE","redacted":True}
