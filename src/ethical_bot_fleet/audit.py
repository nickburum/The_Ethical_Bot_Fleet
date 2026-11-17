"""Append-only, hash-chained audit log (JSON Lines).

Every record carries the SHA-256 of the previous record, so editing or
deleting a line breaks the chain and ``verify`` reports where.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # Windows: one writer at a time is then the caller's job
    fcntl = None  # type: ignore[assignment]

GENESIS = "0" * 64


def _tail(handle) -> Optional[Dict[str, Any]]:
    """The last record in an open log file, or None if it is empty."""
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    block = b""
    while end > 0:
        start = max(0, end - 65536)
        handle.seek(start)
        block = handle.read(end - start) + block
        lines = [line for line in block.split(b"\n") if line.strip()]
        if len(lines) > 1 or (lines and start == 0):
            return json.loads(lines[-1].decode("utf-8"))
        end = start
    return None


def _digest(record: Dict[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._prev = GENESIS
        self._seq = 0
        self.records: List[Dict[str, Any]] = []  # only kept when there is no file
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    if line.strip():
                        try:
                            last = json.loads(line)
                            self._prev, self._seq = last["hash"], last["seq"]
                        except (ValueError, KeyError, TypeError):
                            raise ValueError(
                                "%s is not a valid audit log (line %d is damaged); "
                                "move it aside or choose another file" % (path, number)
                            ) from None

    def write(self, event: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            if not self.path:
                record = self._record(event, fields)
                self.records.append(record)
                return record
            # Owner-only: the log is a record of what was visited.
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "r+b") as handle:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX)  # other fleets and processes may share this file
                try:
                    last = _tail(handle)
                    if last is not None:
                        self._prev, self._seq = last["hash"], last["seq"]
                except (ValueError, KeyError, TypeError):
                    raise ValueError("%s is damaged and cannot be appended to" % self.path) from None
                record = self._record(event, fields)
                handle.seek(0, os.SEEK_END)
                handle.write((json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
            return record

    def _record(self, event: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        self._seq += 1
        record: Dict[str, Any] = {"seq": self._seq, "ts": round(self._clock(), 3), "event": event}
        record.update({k: v for k, v in fields.items() if v is not None})
        record["prev"] = self._prev
        record["hash"] = self._prev = _digest(record)
        return record


def read(path: str) -> List[Dict[str, Any]]:
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    record = json.loads(line)
                    record["event"]
                except (ValueError, KeyError, TypeError):
                    raise ValueError("%s is not an audit log (line %d is not an audit record)" % (path, number)) from None
                records.append(record)
    return records


def verify(path: str, head: Optional[str] = None) -> Tuple[bool, int, str]:
    """Check the hash chain. Returns ``(ok, records_checked, message)``.

    A chain proves that no record was edited, removed from the middle, or
    reordered. Records cut off the end leave a shorter but valid chain, so to
    detect that, keep the last hash somewhere else and pass it as ``head``.
    """
    prev = GENESIS
    count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                claimed = record.pop("hash")
            except (ValueError, KeyError, TypeError, AttributeError):
                return False, count, "line %d is not a valid audit record" % number
            if record.get("prev") != prev:
                return False, count, "line %d does not follow the previous record (a line was removed or reordered)" % number
            if _digest(record) != claimed:
                return False, count, "line %d was modified after it was written" % number
            prev = claimed
            count += 1
    if head is not None and not (prev.startswith(head) and len(head) >= 8):
        return False, count, "the log does not end at the expected record %s (records were removed from the end)" % head
    return True, count, "chain intact, last record %s" % prev[:16]
