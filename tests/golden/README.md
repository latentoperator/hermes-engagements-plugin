# Golden tool cases

The golden fixture set is the test contract for `engagements/tools.py` (docs/TOOLS.md, "Examples and fixtures").
One JSON file per case at `tests/golden/<tool>/<case>.json`, where `<tool>` is the tool name
(`engagements_find`, `engagements_bind`, `engagements_context`, `engagements_create`, `engagements_update`).
`tests/test_golden.py` runs every file through the real handler and compares the full envelope.
All data is fictional (Acme Bakery, Riverbend Dental, `.example` domains).

## Case file

```json
{
  "description": "one line: what this case proves",
  "fixture": "studio",
  "setup": [],
  "clock": "2026-11-07T14:02:00-06:00",
  "profile": "studio-dev",
  "session": {"id": "sess-1", "cwd": null, "surface": "cli", "binding": null},
  "config": {},
  "request": {},
  "expected": {},
  "state_after": null,
  "files_after": {}
}
```

- `fixture`: a directory under `tests/fixtures/` that is copied to a fresh temp library for every case.
  `studio` (library id `22222222-2222-4222-8222-222222222222`) is the rich fixture used for golden cases.
- `setup`: ordered mutations applied to the copy before the call. Paths are relative to the library root, or start
  with `<home>/` to address the temp home directory (used for binding by working directory):
  - `{"op": "write", "path": p, "text": s}` or `{"op": "write", "path": p, "hex": "..."}`; optional `"repeat": n`
    repeats `text` n times. Parent directories are created.
  - `{"op": "append", "path": p, "text": s, "repeat": n}` (`repeat` optional, default 1).
  - `{"op": "replace", "path": p, "old": s, "new": s}`: text replace in the file bytes (UTF-8). `old` must occur
    exactly once, otherwise the harness fails the case.
  - `{"op": "delete", "path": p}`, `{"op": "mkdir", "path": p}`, `{"op": "symlink", "path": p, "target": t}`.
  - `{"op": "snapshot", "path": p}`: read the file through `writer.read_record` at that moment, which stores
    `.engagements/snapshots/<rev>.md`. A stale `base_rev` can only rebase if the base was snapshotted before it
    changed, so a case that edits a record after the agent's "earlier read" snapshots it first.
- `clock`: the injected clock, an aware ISO datetime.
- `profile`: the Hermes profile name (`studio-dev` unless stated).
- `session`: `id` (null means no session id), `cwd`, `surface` (`cli`, `tui`, `desktop`, ...), `binding` (the
  stored binding value, or null for nothing stored). The harness stores `binding` under
  `binding:<library id>:<session id>` in a dict-backed state before the call.
- `config`: optional overrides for `auto_bind` (default true), `banner` (default true), `labels`, `create_default`
  (default false; true lets the call create a missing library) and `library_root` (default: the case's temp root).
- `request`: the tool arguments exactly as the model would send them.
- `expected`: the complete envelope, compared with `==` after placeholder substitution. Errors carry
  `ok: false` and `error: {code, message, details}`; successes carry `ok: true` and `data`. `warnings` is
  always present.
- `state_after`: optional. The stored binding value expected for the session after the call (`null` = unset).
  Omit it to skip the check.
- `files_after`: optional map of relative path to the exact expected full text of that file after the call
  (placeholders allowed). Omit it to skip.

## Placeholders

Strings in `request`, `expected`, `state_after`, `files_after` and `setup` paths may contain:

| Placeholder | Meaning |
|---|---|
| `<rev:PATH>` | rev (first 16 hex of SHA-256) of `PATH`'s bytes after setup, before the call |
| `<fixture_rev:PATH>` | rev of `PATH` as shipped in the fixture, before setup |
| `<rev_after:PATH>` | rev of `PATH`'s bytes after the call |
| `<root>` | the absolute library root (resolved) |
| `<home>` | the absolute temp home directory (HOME is pointed at it for the call) |
| `<message>` | whole-value wildcard for an error or warning `message`: any non-empty single-line string |

Revisions are always computed, never hand-written. `<message>` is only for messages that `tools.py` itself
words; assert the exact string when TOOLS.md prints it or when a library, writer, records or binding module
produces it.

Beyond the comparison, the harness asserts for every case: no `traceback` text anywhere in the output, no
absolute path in any error or warning, and, when `expected.ok` is false, that no record file (non-dot path)
changed from its post-setup bytes.

## Regenerating

Do not generate `expected` from the code under test. Author it from TOOLS.md, then run the case and investigate
any difference as a possible bug before changing either side.
