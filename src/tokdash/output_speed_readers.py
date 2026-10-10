"""Verified per-call readers shared by usage accounting and session detail.

No reader changes which usage record is billed. Ownership and exclusions travel
with that record, including when a fork or a final settlement replaces it.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from typing import Any
import math

try:
    from .output_speed import (
        BASIS_INCLUDING, KIND_POST_FIRST_TOKEN, KIND_REQUEST_WINDOW,
        KIND_RESPONSE_WINDOW, STATUS_AMBIGUOUS_PAIR, STATUS_INCOMPLETE,
        STATUS_INVALID_TIMING, STATUS_MISSING_TIMING, measured, unmeasured,
    )
except ImportError:  # coding_tools.py also supports direct script execution
    from output_speed import (
        BASIS_INCLUDING, KIND_POST_FIRST_TOKEN, KIND_REQUEST_WINDOW,
        KIND_RESPONSE_WINDOW, STATUS_AMBIGUOUS_PAIR, STATUS_INCOMPLETE,
        STATUS_INVALID_TIMING, STATUS_MISSING_TIMING, measured, unmeasured,
    )


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and n >= 0 else None


def request_window_timing(output, reasoning, created, completed, error=None, finish=None, *, ensemble=False):
    """OpenCode-family output excludes reasoning; the two disjoint buckets add.

    Created-to-completed includes request overhead AND any awaited tools/retries.
    Completion is written even on failure, so an error/abnormal finish wins over
    the presence of a closing clock. No duration floor or rate ceiling.
    """
    if ensemble:
        # MiMo max mode replays a winning candidate after multiple requests and
        # a judge. Its assistant-message clock is not a single-call window.
        return unmeasured(STATUS_AMBIGUOUS_PAIR)
    if error is not None or str(finish or '').lower() in {
        'error', 'aborted', 'cancelled', 'canceled', 'length', 'content-filter',
    }:
        return unmeasured(STATUS_INCOMPLETE)
    if completed is None:
        return unmeasured(STATUS_INCOMPLETE)
    start, end = number(created), number(completed)
    out, think = number(output), number(reasoning if reasoning is not None else 0)
    if (start is None or end is None or out is None or think is None
            or not out.is_integer() or not think.is_integer()):
        return unmeasured(STATUS_INVALID_TIMING)
    return measured(tokens=out + think, duration_ms=end - start,
                    measurement_kind=KIND_REQUEST_WINDOW, token_basis=BASIS_INCLUDING)


def native_message_timing(data, source=None):
    tokens = data.get('tokens') or {}
    clock = data.get('time') or {}
    return request_window_timing(tokens.get('output'), tokens.get('reasoning'),
                                 clock.get('created'), clock.get('completed'),
                                 data.get('error'), data.get('finish'),
                                 ensemble=source == 'mimo' and (data.get('agent') == 'max' or data.get('mode') == 'max'))


class QwenTimingAssociation:
    """Direct assistant.parentUuid -> API telemetry UUID, never a time join.

    The recorder appends telemetry synchronously before recording the assistant
    settlement. Only direct, unique links in the same session/agent are accepted;
    interleaving or historical records without telemetry are ordinary absence.
    Token/model comparisons validate the linked record, never establish its owner.
    """
    def __init__(self):
        self.telemetry = {}
        self.rows = []
        self.conflicts = set()

    def observe(self, rec, row=None):
        payload = rec.get('systemPayload')
        event = payload.get('uiEvent') if isinstance(payload, dict) else None
        if rec.get('type') == 'system' and isinstance(event, dict):
            if event.get('event.name') in {'qwen-code.api_response', 'api_response'}:
                identity = rec.get('uuid')
                if identity:
                    old = self.telemetry.get(identity)
                    if old is not None and old != rec:
                        self.conflicts.add(identity)
                    self.telemetry[identity] = rec
        if row is not None:
            self.rows.append((rec, row))

    def resolve(self):
        claims = defaultdict(set)
        for rec, _row in self.rows:
            claims[rec.get('parentUuid')].add(rec.get('uuid') or id(rec))
        for rec, row in self.rows:
            parent = rec.get('parentUuid')
            owner = self.telemetry.get(parent)
            speed = unmeasured(STATUS_MISSING_TIMING)
            if parent in self.conflicts or (owner and len(claims[parent]) != 1):
                speed = unmeasured(STATUS_AMBIGUOUS_PAIR)
            elif owner:
                event = owner['systemPayload']['uiEvent']
                scope = all(rec.get(k) == owner.get(k) for k in ('sessionId', 'agentId', 'isSidechain'))
                model = str(event.get('model') or '').rsplit('/', 1)[-1].casefold()
                usage = rec.get('usageMetadata') or {}
                counters = [('promptTokenCount', 'input_token_count'),
                            ('candidatesTokenCount', 'output_token_count'),
                            ('thoughtsTokenCount', 'thoughts_token_count'),
                            ('cachedContentTokenCount', 'cached_content_token_count')]
                valid = scope and bool(event.get('response_id')) and model == str(row['model']).rsplit('/', 1)[-1].casefold()
                valid = valid and all(isinstance(usage.get(a, 0), int) and not isinstance(usage.get(a, 0), bool)
                                      and usage.get(a, 0) >= 0 and usage.get(a, 0) == event.get(b, 0) for a, b in counters)
                # OpenAI-compatible Qwen adapters can put reasoning INSIDE
                # candidatesTokenCount, while Gemini adapters keep it separate.
                # Resolve this from the recorded total identity, never from
                # the billing row's two counters or an assumed provider default.
                candidate, think = usage.get('candidatesTokenCount',0), usage.get('thoughtsTokenCount',0)
                total = usage.get('totalTokenCount')
                speed_tokens = candidate if think == 0 and total is None else None
                if valid and isinstance(total,int) and not isinstance(total,bool) and total == event.get('total_token_count'):
                    prompt = usage.get('promptTokenCount',0)
                    if total == prompt + candidate and think <= candidate:
                        speed_tokens = candidate
                    elif total == prompt + candidate + think:
                        speed_tokens = candidate + think
                if not valid or speed_tokens is None:
                    speed = unmeasured(STATUS_AMBIGUOUS_PAIR)
                elif event.get('status_code', 200) != 200:
                    speed = unmeasured(STATUS_INCOMPLETE)
                elif event.get('duration_ms') is not None and event.get('ttft_ms') is not None:
                    duration, ttft = number(event['duration_ms']), number(event['ttft_ms'])
                    speed = (unmeasured(STATUS_INVALID_TIMING) if duration is None or ttft is None else
                             measured(tokens=speed_tokens, duration_ms=duration - ttft,
                                      measurement_kind=KIND_POST_FIRST_TOKEN, token_basis=BASIS_INCLUDING))
            row['_speed'] = speed
        return any(rec.get('parentUuid') not in self.telemetry for rec, _ in self.rows)


def dsh_stream_timing(stream, usage):
    """First explicit assistant chunk -> unique usage chunk in that exact stream.

    Coalesced runs only supply time0 in newer formats. Untimestamped legacy runs
    remain unavailable. Settlement tokens must agree with the stream's usage.
    """
    if not isinstance(stream, list) or not stream:
        return unmeasured(STATUS_MISSING_TIMING)
    if not isinstance(usage, dict) or number(usage.get('outputTokens')) is None:
        return unmeasured(STATUS_AMBIGUOUS_PAIR)
    first = None
    latest = None
    ends = []
    for record in stream:
        if not isinstance(record, dict):
            return unmeasured(STATUS_AMBIGUOUS_PAIR)
        kind = record.get('type')
        chunk = record.get('chunk') or {}
        if not isinstance(chunk, dict):
            return unmeasured(STATUS_AMBIGUOUS_PAIR)
        clock = record.get('time') if kind == 'chunk' else record.get('time0')
        stamp = number(clock)
        if kind in {'text-chunks', 'reasoning-chunks', 'tool-call-chunks'} and stamp is None:
            return unmeasured(STATUS_MISSING_TIMING)
        if kind == 'chunk' and chunk.get('type') == 'usage':
            ends.append((stamp, chunk.get('usage')))
        elif kind == 'chunk' and chunk.get('type') == 'finish':
            reason = chunk.get('reason') or {}
            if not isinstance(reason, dict) or reason.get('kind') in {'error', 'abort', 'aborted', 'cancelled', 'canceled'}:
                return unmeasured(STATUS_INCOMPLETE)
        elif kind == 'chunk' and chunk.get('type') in {'error', 'abort', 'aborted'}:
            return unmeasured(STATUS_INCOMPLETE)
        elif kind == 'chunk' or kind in {'text-chunks', 'reasoning-chunks', 'tool-call-chunks'}:
            if stamp is None:
                return unmeasured(STATUS_MISSING_TIMING)
            if first is None:
                first = stamp
            if ends or (first is not None and stamp < first):
                return unmeasured(STATUS_INVALID_TIMING)
            # Coalesced records do not establish a monotone producer clock.
            # Every observed output bound must precede its settlement, even
            # when a later record carries an earlier timestamp.
            latest = stamp if latest is None else max(latest, stamp)
        else:
            return unmeasured(STATUS_AMBIGUOUS_PAIR)
    if len(ends) != 1:
        return unmeasured(STATUS_AMBIGUOUS_PAIR if ends else STATUS_MISSING_TIMING)
    end, counted = ends[0]
    if counted != usage:
        return unmeasured(STATUS_AMBIGUOUS_PAIR)
    if first is None or end is None:
        return unmeasured(STATUS_MISSING_TIMING)
    if end < latest:
        return unmeasured(STATUS_INVALID_TIMING)
    return measured(tokens=usage.get('outputTokens'), duration_ms=end - first,
                    measurement_kind=KIND_RESPONSE_WINDOW, token_basis=BASIS_INCLUDING)


_CODEX_COUNTERS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                   'output_tokens', 'reasoning_output_tokens', 'total_tokens')

def _usage_signature(usage):
    if not isinstance(usage, dict):
        return None
    return tuple(usage.get(k, 0) for k in _CODEX_COUNTERS)


def clipped_union_ms(intervals, start, end):
    """Clip before union: overlaps and out-of-bracket tails count only once."""
    total, cursor = 0.0, start
    for lo, hi in sorted((max(start, a), min(end, b)) for a, b in intervals):
        if hi > max(cursor, lo):
            total += hi - max(cursor, lo)
            cursor = hi
    return total


class CodexResponseAssociation:
    """Explicit response marker owns timed model items and one counted snapshot.

    token_usage_record closes a response, naming response_id + turn_id and its
    thread usage state. The following token_count must validate that SAME state.
    Without that marker ownership is unproven. No token-event gap is a duration.
    Collapsed Reasoning, unknown items, compaction, and tool-call-only output are
    unavailable. Tool execution intervals are indexed per turn and clipped into
    each verified first/last model-item bracket, including late parallel tools.
    """
    MODEL_ITEMS = frozenset({'AgentMessage', 'Reasoning'})
    TOOL_ITEMS = frozenset({'CommandExecution', 'FileChange', 'McpToolCall', 'WebSearch', 'DynamicToolCall'})

    def __init__(self):
        self.turn = None
        self.models = []
        self.model_item_ids = []
        self.status = None
        self.pending = None
        self.calls = {}
        self.tools = defaultdict(list)
        self.seen_items = {}
        self.seen_responses = set()
        self.tool_call_output = False
        self.reasoning_items = False
        self.invalid_tool_turns = set()
        self.conflicting_items = set()

    def _reset(self):
        self.models = []
        self.model_item_ids = []
        self.status = None
        self.pending = None
        self.tool_call_output = False
        self.reasoning_items = False

    def _exclude(self, status):
        self.status = status
        if self.pending:
            self.pending['status'] = status

    def observe(self, obj):
        p = obj.get('payload') or {}
        kind = p.get('type') if obj.get('type') == 'event_msg' else obj.get('type')
        if kind in {'task_started', 'turn_context', 'session_meta'}:
            new_turn = p.get('turn_id')
            if new_turn != self.turn or kind in {'task_started', 'session_meta'}:
                self._reset()
            self.turn = new_turn
        elif kind in {'turn_aborted', 'compacted'}:
            self.status = STATUS_INCOMPLETE
            self.models = []
            if self.pending:
                self.pending['status'] = STATUS_INCOMPLETE
        elif kind == 'response_item':
            if p.get('type') in {'function_call', 'custom_tool_call', 'tool_search_call', 'web_search_call'}:
                self.tool_call_output = True
        elif kind == 'item_completed':
            item = p.get('item') or {}
            item_kind, item_id = item.get('type'), item.get('id')
            turn = p.get('turn_id')
            interval = (number(p.get('started_at_ms')), number(p.get('completed_at_ms')))
            old = self.seen_items.get((turn, item_id)) if item_id else None
            if old is not None:
                if old != (item_kind, interval):
                    self.conflicting_items.add((turn, item_id))
                    # Model IDs belong to the response that first recorded
                    # them. A contradiction may arrive after registration or
                    # after the next response starts; do not poison that next
                    # response's pending association instead of the owner.
                    if old[0] not in self.MODEL_ITEMS:
                        self._exclude(STATUS_AMBIGUOUS_PAIR)
                    if item_kind in self.TOOL_ITEMS or old[0] in self.TOOL_ITEMS:
                        self.invalid_tool_turns.add(turn)
                return
            if item_id:
                self.seen_items[(turn, item_id)] = (item_kind, interval)
            if item_kind == 'UserMessage':
                return
            if item_kind == 'ContextCompaction':
                self._exclude(STATUS_INCOMPLETE)
                self.models = []
                return
            if item_kind not in self.MODEL_ITEMS | self.TOOL_ITEMS:
                self._exclude(STATUS_AMBIGUOUS_PAIR)
                return
            if item_id and item_kind in self.MODEL_ITEMS:
                self.model_item_ids.append((turn, item_id))
            if None in interval or interval[1] < interval[0]:
                self._exclude(STATUS_INVALID_TIMING)
                if item_kind in self.TOOL_ITEMS:
                    self.invalid_tool_turns.add(turn)
                return
            if item_kind in self.TOOL_ITEMS:
                # A collapsed clock cannot bound a tool that reports real work.
                duration = item.get('duration_ms', item.get('duration'))
                duration_nonzero = (any(number(duration.get(k)) not in (None, 0) for k in ('secs','nanos'))
                                    if isinstance(duration, dict) else number(duration) not in (None, 0))
                if interval[0] == interval[1] and duration_nonzero:
                    self.invalid_tool_turns.add(turn)
                self.tools[turn].append(interval)
            elif not turn or turn != self.turn:
                self.status = STATUS_AMBIGUOUS_PAIR
            else:
                if interval[0] == interval[1]:
                    self.status = STATUS_INVALID_TIMING
                if item_kind == 'Reasoning':
                    self.reasoning_items = True
                self.models.append(interval)
        elif kind == 'token_usage_record':
            rid = p.get('response_id')
            status = self.status
            if not rid or rid in self.seen_responses or p.get('turn_id') != self.turn:
                status = STATUS_AMBIGUOUS_PAIR
            if self.tool_call_output:
                # Arguments have output tokens but no complete timed model item.
                status = status or STATUS_MISSING_TIMING
            usage = p.get('usage')
            if not isinstance(usage, dict) or any(
                not isinstance(usage.get(k, 0), int) or isinstance(usage.get(k, 0), bool) or usage.get(k, 0) < 0
                for k in _CODEX_COUNTERS
            ) or usage.get('total_tokens') != usage.get('input_tokens', 0) + usage.get('output_tokens', 0) or usage.get('reasoning_output_tokens', 0) > usage.get('output_tokens', 0):
                # The total identity proves output includes reasoning; never add
                # a reasoning bucket to a counter whose inclusion is unverified.
                status = STATUS_AMBIGUOUS_PAIR
            if isinstance(usage, dict) and number(usage.get('reasoning_output_tokens')) not in (None, 0) and not self.reasoning_items:
                status = status or STATUS_MISSING_TIMING
            self.seen_responses.add(rid)
            self.pending = {'turn': self.turn, 'models': self.models,
                            'items': tuple(self.model_item_ids),
                            'status': status, 'usage': p.get('usage'),
                            'total': p.get('thread_token_usage')}
            self.models = []
            self.model_item_ids = []
            self.status = None
            self.tool_call_output = False
            self.reasoning_items = False

    def register(self, identity, info):
        owner = self.pending
        self.pending = None
        if identity in self.calls:
            return
        status = STATUS_MISSING_TIMING
        if owner:
            valid = (_usage_signature(info.get('last_token_usage')) == _usage_signature(owner['usage'])
                     and _usage_signature(info.get('total_token_usage')) == _usage_signature(owner['total']))
            status = owner['status'] if valid else STATUS_AMBIGUOUS_PAIR
        self.calls[identity] = (owner, status)
        # Invalid/duplicate snapshots must not donate items to another response.
        self.models = []
        self.model_item_ids = []
        self.status = None
        self.tool_call_output = False
        self.reasoning_items = False

    def resolve(self):
        out = {}
        tool_index = {}
        for turn, intervals in self.tools.items():
            merged = []
            for lo, hi in sorted(intervals):
                if merged and lo <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                else:
                    merged.append((lo, hi))
            tool_index[turn] = (merged, [lo for lo, _ in merged], [hi for _, hi in merged])
        for identity, (owner, status) in self.calls.items():
            if owner and any(item in self.conflicting_items for item in owner['items']):
                status = STATUS_AMBIGUOUS_PAIR
            if owner and owner['turn'] in self.invalid_tool_turns:
                status = STATUS_INVALID_TIMING
            if status or not owner or not owner['models']:
                out[identity] = unmeasured(status or STATUS_MISSING_TIMING)
                continue
            start = min(a for a, _ in owner['models'])
            end = max(b for _, b in owner['models'])
            intervals, starts, ends = tool_index.get(owner['turn'], ([], [], []))
            low = bisect_left(ends, start)
            high = bisect_left(starts, end)
            intervals = intervals[low:high]
            window = end - start - clipped_union_ms(intervals, start, end)
            out[identity] = measured(tokens=(owner['usage'] or {}).get('output_tokens'), duration_ms=window,
                                       measurement_kind=KIND_RESPONSE_WINDOW, token_basis=BASIS_INCLUDING)
        return out
