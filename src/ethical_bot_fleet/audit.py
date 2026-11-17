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

GENESIS = "0" * 64


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
            self._seq += 1
            record: Dict[str, Any] = {"seq": self._seq, "ts": round(self._clock(), 3), "event": event}
            record.update({k: v for k, v in fields.items() if v is not None})
            record["prev"] = self._prev
            record["hash"] = self._prev = _digest(record)
            if self.path:
                # Owner-only: the log is a record of what was visited.
                descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            else:
                self.records.append(record)
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
    with open(path, "r", encoding="utf-8") as handle:
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
