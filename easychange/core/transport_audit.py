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
_LAST_ID=0
SAFE_FIELDS=frozenset(("stage","sequence","direction","medium","mode","command_type",
    "attempt","ok","cause","code","state","result_state","duplicate","duration_ms",
    "elapsed_ms","bytes","sha256","frames","packets","chunks","page","pages",
    "generation","result_complete","payload_length","render_ms","encoding_ms"))

def signature(value):
    raw=value if isinstance(value,bytes) else str(value or "").encode("utf-8","replace")
    return {"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}


def _recent_log_lines(path: Path, limit: int = 2000):
    """Read only the final lines needed to recover a persisted monotonic ID.

    A cold GUI log write used to read each entire rotated file before the
    deque discarded all but 2,000 lines. Read backwards in bounded blocks,
    retaining the same last-N-line semantics, including torn final records.
    """
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            remaining = source.tell()
            blocks = []
            newlines = 0
            while remaining > 0 and newlines <= limit:
                amount = min(64 * 1024, remaining)
                remaining -= amount
                source.seek(remaining)
                block = source.read(amount)
                blocks.append(block)
                newlines += block.count(b"\n")
        return b"".join(reversed(blocks)).splitlines()[-limit:]
    except OSError:
        return ()


def log(stage,sequence="",direction="",**kwargs):
    rec={"at":datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
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
        global _LAST_ID
        with _LOCK:
            ROOT.mkdir(parents=True,exist_ok=True)
            if _LAST_ID == 0:
                for path in (FILE.with_suffix(".jsonl.2"),FILE.with_suffix(".jsonl.1"),FILE):
                    for stored_bytes in _recent_log_lines(path):
                        stored = stored_bytes.decode("utf-8", errors="replace")
                        if "\ufffd" in stored:
                            continue
                        try:
                            stored_id=json.loads(stored).get("id")
                            if type(stored_id) is int and stored_id > _LAST_ID:
                                _LAST_ID=stored_id
                        except (TypeError,ValueError,AttributeError):
                            continue
            _LAST_ID=max(time.time_ns(),_LAST_ID+1)
            rec["id"]=_LAST_ID
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
    # Most sessions have >=2,000 entries in the active file. Read newest
    # first, then older rotations only if needed; preserve the exact last
    # 2,000 physical lines and chronological order used by the original deque.
    rows=deque()
    with _LOCK:
        for p in (FILE,FILE.with_suffix(".jsonl.1"),FILE.with_suffix(".jsonl.2")):
            needed=2000-len(rows)
            if needed<=0:
                break
            recent=_recent_log_lines(p,limit=needed)
            rows.extendleft(
                line.decode("utf-8",errors="replace")
                for line in reversed(recent)
            )
    found=[]
    # First pages return the latest entries, so stop at the requested number
    # of valid matches while reading backwards. Cursor pages retain original
    # oldest-first semantics and read one extra match to compute "more".
    lines = iter(rows) if after else reversed(rows)
    needed = limit + (1 if after else 0)
    for line in lines:
        if "\ufffd" in line:
            continue
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
            if len(found) >= needed:
                break
    chosen=found[:limit]
    if not after:
        chosen.reverse()
    return {"events":chosen,"next_cursor":chosen[-1]["id"] if chosen else after,
            "more":bool(after and len(found)>limit),"count":len(chosen),
            "source":"EASYCHANGE_REMOTE","redacted":True}
