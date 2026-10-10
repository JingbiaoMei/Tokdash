"""Bounded, single-flight memoization of complete published speed aggregates."""
from collections import OrderedDict
from copy import deepcopy
from threading import Event, Lock
import json

MAX_ENTRIES = 32
MAX_BYTES = 8 * 1024 * 1024
_lock = Lock()
_values = OrderedDict()
_pending = {}
_bytes = 0


def read(key, build):
    global _bytes
    if key is None:
        return build()
    while True:
        with _lock:
            if key in _values:
                value, size = _values.pop(key)
                _values[key] = (value, size)
                return deepcopy(value)
            event = _pending.get(key)
            if event is None:
                event = _pending[key] = Event()
                break
        event.wait()  # Only identical reads coalesce; unrelated ranges stay independent.
    try:
        value = build()
        size = len(json.dumps(value, separators=(',', ':')).encode())
        if size <= MAX_BYTES:
            with _lock:
                while _values and (len(_values) >= MAX_ENTRIES or _bytes + size > MAX_BYTES):
                    _, (_, old_size) = _values.popitem(last=False)
                    _bytes -= old_size
                _values[key] = (deepcopy(value), size)
                _bytes += size
        return value
    finally:
        with _lock:
            _pending.pop(key).set()  # Failed builds are never cached.


def clear():
    global _bytes
    with _lock:
        _values.clear()
        _bytes = 0
