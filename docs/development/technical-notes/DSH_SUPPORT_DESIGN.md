# DeepSeek Harness token and session support design

**Status:** implemented. Parser, session, cache, API, and frontend integration have landed;
this document remains the design reference.

**Researched:** 2026-08-14, against DeepSeek Harness (`dsh`) 0.1.0-rc.6 and upstream
`deepseek-harness` commit `47f9438` (2026-08-13). Local observations came from WSL2 and a native
macOS arm64 install. Windows path behavior was verified from upstream source; the native Windows
Python zstandard wheel was tested, but dsh itself was not installed on the Windows host.

## Goal and non-goals

Add DeepSeek Harness as a local Tokdash source with:

- provider-reported token usage in Overview and Stats;
- per-session rows and drill-down in Session Explorer;
- model attribution and pricing through the existing pricing database;
- native macOS, Linux, and Windows path discovery;
- persistent-store behavior consistent with the other file-backed clients.

This design intentionally does not:

- read dsh's web API or require the web server to be running;
- read or store DeepSeek credentials;
- add quota tracking;
- infer token counts with a tokenizer or character heuristic;
- decode or reconstruct the full dsh conversation surface.

The source key is `dsh`. The display label is `DeepSeek Harness`.

## Source of truth

dsh persists one append-only logical JSONL event log per session. The default location is:

```text
$DSH_HOME/sessions/<project-key>/<session-id>/session.jsonl.zstd
```

Uncompressed deployments can instead write:

```text
$DSH_HOME/sessions/<project-key>/<session-id>/session.jsonl
```

`DSH_HOME` is dsh's native environment override. Tokdash should resolve it first and otherwise
use `Path.home() / ".dsh"`, matching Node's `os.homedir()` behavior on supported platforms. Expand
a leading `~`, resolve the result to an absolute path, and treat an empty or whitespace-only
`DSH_HOME` as unset. The resolver belongs in `clientpaths.py`; parser modules must not inline
another copy.

V1 follows the ambient `$DSH_HOME` and default home only. If a dsh deployment overrides its home
through launcher-specific configuration without exporting `DSH_HOME`, Tokdash will not discover
that root; set `DSH_HOME` for the Tokdash process or run a native Tokdash server beside dsh.

The project directory name is lossy by design. dsh maps `/`, `\`, and `:` to `-` and wraps the
result in `--...--`. Tokdash must discover files recursively and must not decode the project from
the directory name. The first JSONL row carries the authoritative session header, including the
native `cwd`.

| Platform | Default dsh home | Example project directory |
|---|---|---|
| Linux / WSL | `/home/<user>/.dsh` | `--home-howard-project--` |
| macOS | `/Users/<user>/.dsh` | `--private-tmp--` |
| Windows | `C:\Users\<user>\.dsh` | `--C-Users-H1937-project--` |

A Tokdash process discovers logs only in its own native filesystem by default. A WSL Tokdash
service does not automatically monitor a native Windows dsh installation under
`C:\Users\<user>\.dsh`; that topology needs an explicit mounted path or a native Windows Tokdash
server registered through multi-server mode. The same boundary applies to a remote macOS machine.

## Event format

The first line is a session header:

```json
{
  "type": "session",
  "version": 0,
  "id": "session-...",
  "createdAt": 1786735098528,
  "cwd": "/home/howard",
  "parentSession": "session-parent",
  "seedLength": 104,
  "delegationDepth": 0
}
```

`parentSession` and `seedLength` are optional. Event rows have:

```json
{
  "type": "assistant/message",
  "seq": 104,
  "time": 1786735109133,
  "data": {}
}
```

`seq` is monotonic inside the logical session and `time` is Unix epoch milliseconds.

Version 0 is a developer-preview format with no upstream compatibility promise. Accept only
`header.version == 0`. A different version is unsupported, not corrupt: skip that file and expose
a count through parser diagnostics if one is added. Bump the explicit DSH parser version whenever
extraction or accounting semantics change.

### Zstandard decoding

`.jsonl.zstd` is not one plain zstd stream. dsh writes a separately checksummed zstd frame for
the header and each durable append batch, then concatenates those frames. Decoding only the first
stream or making a one-shot decompressor call is incorrect.

Add `zstandard>=0.23` to core dependencies and read with:

```python
ZstdDecompressor().stream_reader(data, read_across_frames=True).read()
```

This is a required runtime dependency, not an optional import found opportunistically in the
current development environment. Windows `cp313-win_amd64` and macOS
`cp314-macosx_11_0_arm64` wheels were both verified during research.

dsh can append while Tokdash reads. Decode inside the per-file exception boundary, accept only
complete newline-terminated JSON rows, and discard a torn final line. If decoding raises, skip
that file for this parse rather than failing all DSH usage. File replacement is driven by the
normal `(path, mtime_ns, size)` signature.

Ignore unknown event types. Tokdash does not reconstruct the DSH session and therefore does not
need dsh's strict required-event refusal semantics. This may change if a future event version
changes usage accounting.

## Usage accounting

Provider usage appears in two event shapes:

1. `assistant/chunk` with `data.chunk.type == "usage"`, an early sample that can survive a later
   request failure.
2. `assistant/message` with `data.usage`, the finalized sample for a successful provider call.

Both carry a `(turn, step)` identity. dsh's token-meter projection relies on the ordering
invariant that samples for one `(turn, step)` are adjacent: once a later step reports usage, a
legal log does not report the earlier step again. Tokdash must implement the same
replace-not-add fold:

1. Keep the most recent accepted sample as `last = (turn, step, buckets)`.
2. When a new sample has the same key as `last`, replace the pending usage row.
3. Otherwise append a new row and replace `last`.
4. An early chunk with no final assistant message remains counted.
5. A final message replaces its earlier chunk instead of double-counting it.

Use a persistent usage entry id stable across that in-file replacement:

```text
dsh:<session-id>:<turn>:<step>
```

Do not use the physical line number, because the finalized event follows the chunk under a
different `seq`.

### Field mapping

dsh's canonical `TokenUsage` buckets are disjoint:

| dsh field | Usage parser field | Session billing field | Notes |
|---|---|---|---|
| `inputTokens` | `input` | fresh input in `_bill` | Uncached input only. |
| `outputTokens` | `output` | `tokens_out` | Includes reasoning tokens. |
| `cacheReadTokens` | `cacheRead` | `cache_read` | Optional; missing means zero. |
| `cacheWriteTokens` | `cacheWrite` | `cache_write` | Optional; missing means zero. |
| `reasoningTokens` | `0` | `tokens_reasoning = 0` | Already included in output. |

Tokdash's aggregate adds a separate `reasoning` field to input, cache, and output. Copying dsh's
`reasoningTokens` there would count those tokens twice. Preserve dsh reasoning only if a future
API shape adds a non-overlapping display field.

For a usage entry:

```text
input = inputTokens
output = outputTokens
cacheRead = cacheReadTokens or 0
cacheWrite = cacheWriteTokens or 0
reasoning = 0
cost = pricing.get_cost(model, input, output, cacheRead, cacheWrite)
# get_cost positional args: (model, input_tokens, output_tokens, cache_read=0, cache_write=0)
```

For a Session Explorer turn, use the `split-cache-write` billing rule (the existing Pi parser is
the precedent; Claude/Kimi/OpenCode use `input-plus-cache-write`). The public `tokens_in`
total may fold cache writes as billable prompt input (`inputTokens + cacheWriteTokens`) as it does
for other sources, while `_bill` keeps fresh input and cache write separate so later rate edits can
reprice exactly.

Skip an all-zero sample. Do not turn an absent usage object into zero usage.

### Model attribution

A finalized `assistant/message` carries the authoritative model in:

```text
data.message.source.model
```

The provider route is available from `source.provider` and preceding `request/context` events. For
a usage-only chunk with no final message, use the latest `request/context.model`; if that is
absent, use `unknown` rather than guessing from the session preset or timestamp. The provider is
descriptive and does not replace model-name normalization or pricing lookup.

The current pricing database already includes `deepseek-v4-flash`, `deepseek-v4-pro`, and older
DeepSeek aliases. Unknown models still count tokens and cost zero until the pricing database learns
them.

## Fork and subagent behavior

`parentSession` alone must not cause skipping. A fresh one-shot subagent can have a parent and
still make genuine, independently billable calls.

A forked child can clone a completed parent prefix into its own durable log. Its header marks that
prefix with `seedLength`. Skip usage events with:

```text
event.seq < header.seedLength
```

Those events remain visible in the child transcript but are already owned by the parent log.
Counting both would double-count tokens and cost across Session Explorer and Overview.

Fresh child sessions without a seed boundary count normally. This also prevents a resumed fork
from counting inherited parent history again.

## Session Explorer mapping

Each accepted usage sample becomes one internal Tokdash turn:

- `timestamp_ms`: event `time`;
- `model`: model attribution above;
- `tokens_in`: `inputTokens + cacheWriteTokens`;
- `tokens_cache`: `cacheReadTokens`;
- `tokens_out`: `outputTokens`;
- `tokens_reasoning`: `0`;
- `_event_key`: stable DSH entry id;
- `_bill`: model, fresh input, output, cache read, cache write, `split-cache-write`.

Session metadata comes from:

- `session_id`: header `id`, not the filesystem directory name;
- `project`: `_project_from_repo_or_path(..., header.cwd)`;
- `display_name`: latest `session/title`, else the first user-message preview, else the existing
  project/id fallback;
- `is_review_session`: `false`.

Active time follows the existing capped-inter-event-gap estimate from usage-event timestamps. Each
physical DSH session is one stream; unlike Kimi and Claude, do not synthesize concurrent agent
streams inside one file. Parent and child sessions remain separate rows.

## Parser and cache integration

### Overview usage

Add `DSHParser` to `sources/coding_tools.py` and register it as `dsh` in
`CodingToolsUsageTracker.parsers`.

Declare:

```python
SourceSyncCapability(
    mode="file_replace",
    append_jsonl=False,
    session_store=True,
    reason=(
        "DSH append batches are concatenated zstd frames, and a final usage message replaces an "
        "earlier same-step chunk; changed files are reparsed whole."
    ),
)
```

`append_jsonl` stays false even though dsh logically appends. A physical append is another zstd
frame, and same-step replacement means line-local accumulation is not sufficient for correctness.
Full-file replacement keeps failure handling simple and matches the existing parser cache.

Discover both suffixes in one recursive pass and sort the result. Avoid a second recursive scan for
the alternate suffix. Include the effective pricing signature in the normal in-memory cache, as
`BaseParser` already does.

### Session cache

Add `_parse_dsh_session_file`, `_dsh_session_signatures`, `_load_dsh_sessions`, and a DSH branch
to `_raw_sessions_for_tool`. Add the parser to `SESSION_TOOLS` and `TOOL_LABELS`.

For the persistent session store:

- add `_parse_dsh_session_file` to `_SESSION_FILE_PARSER_VERSIONS` at version 1;
- include the decoder and accounting-rule version in the parser signature, not merely the Python
  module hash;
- use `_session_signature_compatible`;
- store price-neutral billing inputs like the other post-1.7.0 parsers;
- on persistent-store failure, log and fall back to source files.

The shared decoder should be a small helper rather than duplicated code between the usage and
session parsers. It should return metadata, event rows, and a structured skip reason without
performing token accounting itself.

### API and frontend

`/api/sessions` and `/api/session` require no format-specific routes once `dsh` is in the session
registry.

Update:

- `SESSION_TOOL_KEYS` and `lastSessionsResponses` initialization in `src/tokdash/static/index.html`;
- the hardcoded per-tool `updateSessionPanel(...)` calls in the session panel bootstrap (add `dsh`);
- `formatToolName`: `dsh -> DeepSeek Harness`;
- `TOOL_BRAND_META`: initial fallback `D` and the DeepSeek brand color until a suitable asset is
  added;
- session fan-out and active-time rollups through the same registry lists.

Avoid maintaining a frontend-only source list that can drift from the backend registry.

## Error and edge-case policy

- Missing `DSH_HOME` directory: empty source, no error.
- Missing header, invalid JSON header, or unsupported version: skip that file.
- Duplicate physical files for one header id: deduplicate by session id and stable event key.
- Missing title: use the first user preview and then the existing fallback.
- Missing model: use `unknown`; never infer by timestamp.
- Missing token fields: treat optional cache and reasoning fields as absent; require an explicit
  numeric input/output object before emitting a row.
- Empty credential-failure sessions: no token rows and no Session Explorer row.
- Torn zstd tail or trailing partial JSON line: keep complete rows only.
- One malformed file: never blank the whole DSH source.

Auxiliary model calls are a known undercount, not an implementation bug. Upstream records the
session-title request but does not attach finalized usage to the durable session surface, and its
compaction path makes a direct LLM call without appending usage of its own. Tokdash can count
durable conversation calls accurately but cannot recover these auxiliary calls from the current
format. Revisit if upstream persists auxiliary usage.

## Test plan

Generate binary fixtures in tests instead of committing blobs:

1. raw `session.jsonl`;
2. multi-frame `session.jsonl.zstd`, made by concatenating independently compressed frames;
3. a torn final frame or trailing partial JSON line;
4. an early usage chunk followed by final usage at the same `(turn, step)`;
5. an early usage chunk with no final message;
6. reasoning included in output, asserting `reasoning == 0` in the emitted row;
7. cache read and cache write mappings plus `split-cache-write` repricing;
8. a fork child with `seedLength`, asserting inherited prefix events are skipped;
9. a fresh child with `parentSession` but no `seedLength`, asserting usage is kept;
10. title, project, fallback title, model, and date-window behavior;
11. unsupported header version and malformed-file isolation;
12. `$DSH_HOME`, blank `$DSH_HOME`, macOS-style home, and Windows-style home/project-directory
    discovery through `clientpaths`;
13. usage-store sync when a chunk row is replaced by its final row;
14. session-cache compatibility and pricing-edit repricing without a source reparse;
15. `/api/sessions?tool=dsh`, `/api/session?tool=dsh`, `/api/active-time`, and frontend registry
    synchronization.

Run focused DSH tests and the release-safe pricing tests during development. Run the full suite
before release.

## Implementation sequence

1. Add the dependency and `clientpaths.dsh_sessions_dir()`.
2. Implement and test the shared header/event decoder, including multi-frame zstd.
3. Land `DSHParser` with usage-fold tests and register the source.
4. Land live Session Explorer mapping and API tests.
5. Add persistent usage/session synchronization and replacement tests.
6. Add frontend registry and branding.
7. Update `docs/reference/SUPPORTED_CLIENTS.md`, both READMEs, the changelog, and
   supported-client assets only after behavior works.

This keeps token accounting observable before cache and UI work can obscure extraction errors.

## References

- Upstream repository: <https://github.com/deepseek-ai/deepseek-harness>
- Session event catalog: <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/persistence-catalog.md>
- JSONL/zstd backend: <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/README.md>
- Token-meter projection: <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/token-meter/README.md>
- Token usage semantics: <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/llm-streaming.md>
- Home-path resolution: <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/util/home-paths/README.md>
