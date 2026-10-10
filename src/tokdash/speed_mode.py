"""Timing readers are opt-in; ordinary accounting and activity never enable them."""
from contextlib import contextmanager
from contextvars import ContextVar

_enabled = ContextVar('tokdash_output_speed_enabled', default=False)


def timing_enabled() -> bool:
    return _enabled.get()


@contextmanager
def collect_timings(enabled: bool = True):
    token = _enabled.set(enabled)
    try:
        yield
    finally:
        _enabled.reset(token)


def canonical_turn_key(source: str, key: str) -> str:
    """The existing usage and rendered-turn identity spellings, without heuristics."""
    if source == 'kimi':
        return 'kimi:' + key
    if source == 'omp' and key.startswith('omp:'):
        return 'omp:' + key.split(':', 2)[-1]
    return key
