# Engagements tool contract

Status: accepted contract, rev 3, 2026-11-06. Rev 3 records the ten rulings that settled the spec questions raised by the K5 build. Rev 2 added "Parsing, fences and filenames" (spec C6), closing gaps found by the first Phase 1 implementation run. Rev 1 was draft 3 plus one review amendment (review PASS-WITH-AMENDMENTS, amendment applied); draft 1 failed independent review (14 findings) and draft 2 resolved 8 and partly resolved 6, adding 3 new findings that draft 3 addressed. Implements `docs/SPEC.md` rev 5, sections 4 to 6 and 13. C1 to C6 are listed in `docs/SPEC.md` section 13.

**Precedence.** This document is normative for tool schemas, op semantics, section editing, parsing details, id and filename derivation, errors and pagination. Where it is more specific than the spec on those topics, this document wins. On every other topic the spec wins.

All examples are fictional and illustrative. The test contract is the golden fixture set described in "Examples and fixtures" at the end. Revisions in examples are written `<rev>` because they are computed from pinned fixture bytes, not hand-written.

## Decisions this document makes

T-rows fill gaps the spec leaves open. C-rows **amend** the spec (spec section 13). Phase 0 amendments are cited as "spec A1" to "spec A10".

| # | Decision | Why |
|---|---|---|
| T1 | Model-facing schemas are non-strict JSON Schemas with no `oneOf` and no `anyOf`. The complete contract, including per-op rules, is enforced by the handler, and the strict per-op schemas below are the test contract. Provider compatibility is proven through the Tool Search bridge in Phase 2 (`tool_describe` then `tool_call`) on the stock provider adapters used in the acceptance run, before any portability claim. | Several providers reject or mishandle `oneOf`/`anyOf` in tool parameters. |
| T2 | `set_property` takes `value` (string) for `name`, `status`, `client`, and `values` (array of strings) for `aliases`, `locations`. | Removes the only `anyOf` (T1). |
| T3 | Lookup order: an exact id that exists exactly once wins. Otherwise exact normalized name or alias matches, within the requested kind. In `engagements_bind`, a project name is resolved only among the given client's projects when `client` is also given; ids are always global. A duplicated id never selects a record; it returns `ambiguous` with paths. | Deterministic resolution. |
| T4 | `engagements_bind` takes an optional `mode` (`explicit`, `off`, `auto`). Omitted: `explicit` when an id is given, otherwise `off` (the spec rule). | An agent needs a way back to automatic mode. |
| T5 | New error code `no_session`. | Binding needs a conversation to attach to. |
| T6 | `engagements_create` takes an optional `source` (default `conversation`). | The Change log line format needs one. |
| T7 | `engagements_context` requires `rev` when `offset > 0` and checks it whenever it is given. | Pages from different revisions must never be stitched together. |
| T8 | Result `path` values are relative to the library root with `/` separators. `engagements_context` also returns the absolute `library_root`. | Portable results. One absolute root is enough to open a file. |
| T9 | `## Change log` and the `updated` property are plugin-owned. Any create section or op whose section normalizes to `change log`, or whose key is `updated`, is `invalid_op` with `reason: "plugin_owned"`. | The spec says these edits never conflict. That only holds if callers cannot write them. |
| T10 | Change log summaries are value-free (templates below). | Values can be long or sensitive. History snapshots keep the values. |
| T11 | Id derivation transliterates before slugging (NFKD, drop combining marks), then applies the spec 4.4 rules, with the length and fallback rules below. | `Café Rouge` becomes `cafe-rouge` instead of `caf-rouge`. |
| C1 | **Amends spec 6.2 `similar_exists`.** Similarity compares within one kind, and for projects within one client. It includes inactive records. | Two clients can each have a "Website 2026" project. |
| C2 | **Amends spec 4.1 discovery for malformed records.** A file whose frontmatter cannot be parsed as YAML is still recognized as a malformed record when a bounded line scan of its frontmatter finds the marker (rules below). | Otherwise bad YAML makes a record vanish instead of showing up in `doctor`. |
| C3 | **Amends spec 5.2 `set_property`.** The single `value` (string or list) field becomes `value` (string) or `values` (list), per T2. | Removes `anyOf` (T1). |
| C4 | **Amends spec 4.4 id derivation.** Transliteration, length, fallback and suffix rules per T11. | Spec 4.4 does not cover accents, length or empty slugs. |
| C5 | **Amends spec 5.4.** A project with only an `unknown_client` defect accepts a `set_property client` op naming a valid client (see "Malformed records"). | Otherwise the record cannot be repaired through the tools. |

Spec 5.2 allows `aliases` on both clients and projects. This contract keeps that (draft 1 wrongly restricted it).

## Common result envelope

```json
{"ok": true, "data": {}, "warnings": []}
{"ok": false, "error": {"code": "not_found", "message": "No client or project with id 'acme'.", "details": {}}, "warnings": []}
```

- `ok` is always present. `data` appears only when `ok` is true, and `error` only when it is false.
- `warnings` is always an array of `{code, message, path?}`. Warning codes: `snapshot_failed`, `maintenance_warning`, `record_invalid`, `malformed_skipped`, `library_created`, `duplicate_section`, `encoding_invalid`, `record_defects_omitted`, `possible_secret`, `discovery_incomplete`.
- `possible_secret` is attached to a result whose write **succeeded**. A record that looks like it holds a credential is saved, never refused and never redacted: `engagements_create` and `engagements_update` behave exactly as they otherwise would. Each entry is `{code, message, area, section?, line, kind, occurrence}`. The location is structural only: `area` is `property` (frontmatter), `section`, `change_log` or `body`; `section` is the 1-based index of the section in the saved record and appears for `section` and `change_log`; `line` is the 1-based line of the saved record; `occurrence` is the 1-based position of the finding among the findings on that line. `message` is the fixed stern text plus that location. `kind` is one of the fixed words `password or secret assignment`, `private key`, `cloud access key`, `API key`, `access token`, `bearer token`, `URL with embedded credentials`, `webhook URL`. At most five entries are listed, in file order; when there were more findings, one further entry `{code, message, omitted}` carries the count and no location.
- A create judges the record it saved, once the write has returned. An update compares the findings of the saved record with the findings of the record before it, as two sets of `(kind, matched text)`, and warns for a pair that is new: an unchanged secret does not warn again (doctor reports it), a value added and removed in the same call does not warn, a value that only moves or is only re-quoted does not warn, and a credential assembled across two ops does. Positions are never compared. A matched assignment value is trimmed of surrounding quotes and spaces, the same way on its own line and on the next line, before it is compared.
- **The promise.** A secret warning (`possible_secret`) and a doctor secret finding (`secret_shaped`) contain no text taken from the record. Location is structural only: the area (`property`, `section`, `change_log`, `body`), the 1-based index of the section in the record where that applies, and the 1-based line number in the file. Never a property name's value, a section title, a snippet or a label derived from the match. A doctor secret finding also names the record it is in by its relative `path`, the one field in it taken from the record, and its `kind`, `occurrence` and fixed message are the plugin's own; that holds for JSON and text doctor output alike.
- **Nothing else is promised:** fields that identify or quote the record (`data.id`, `data.path`, `data.name`, section titles, `data.change_log_line`, record text returned by `engagements_context`, and other doctor findings that name a heading or path) are the record's own content and are returned as written, so a credential used to name a record or a heading appears wherever that record or heading is identified, including a doctor secret finding's `path`, and a record's id cannot be changed after creation.
- Detection is pattern based and deliberately literal (spec 6.2), so it misses a secret that does not look like one and warns about an assigned value that is only prose (`password: ask Jane`). The warning is an instruction to the model, not a guarantee that it acts on it. Doctor reports every finding in every record, structurally, until the value is gone; two findings on one line are two findings, told apart by `occurrence`.
- The record file, and the history and snapshot copies the plugin keeps of it, hold whatever the record held. Removing a secret from a record does not remove it from those copies, which is one reason the warning says to rotate the credential. The warning does not change how history and snapshots are stored.
- Messages and details carry relative paths, line numbers and reasons. A diagnostic label or message whose text is secret-shaped (a heading, a frontmatter key, an id, an evidence string) has that text replaced with `[redacted]`; path, line and reason stay. That masking is best effort for prose the plugin writes about a record and is not part of the promise above.
- Every field under `error` and `warnings` is diagnostic, at any depth, including dictionary keys. `data` and the conflict footprints' `base_text` and `current_text` are contract body: they are returned verbatim, neither path-rewritten nor redacted.

**Display words.** Every message, notice, banner and command output is rendered with the profile's `labels.client` and `labels.project` settings, which default to `client` and `project`, so a profile that sets Account and Matter reads `No Account or Matter matches 'acme'`. A message that has to name a `kind` value or an argument the model must pass keeps that literal in backticks and uses the label for the prose around it; tool names, argument names, `kind` and reason codes, JSON keys, file and folder names, and text quoted from a record are not display words and keep their own spelling. A sentence a lower layer writes with the default words is re-worded at the tool boundary, where the settings are known, so a refusal raised while reading or writing a record reads the same as one the tool wrote itself; the error code, reason and details are unchanged either way.

### Error codes

| Code | Meaning |
|---|---|
| `not_found` | No record matches. `details.reason`: `no_match`, `not_a_record`, or `excluded` (inside the root but in a dot-directory, behind a symlink component, or in plugin metadata). |
| `ambiguous` | More than one record matches, or an id is duplicated. `details.candidates` lists `{id, kind, name, client, path}`. |
| `unbound` | No id or path, and no applicable (or no still-valid) binding. `details`: `mode`, `state`, `reason`, `evidence`, `candidates`, and `auto` (`{state, reason, evidence, candidates}`, the live automatic resolution). An invalid binding is reported here, as `state: "invalid_binding"` (see `engagements_context`). |
| `invalid_binding` | A project/client mismatch, or the record is inactive, missing or malformed. `engagements_bind` returns this code; a bound read reports the same conditions under `unbound` as `state: "invalid_binding"`. |
| `conflict` | `details.reason`: `footprint_changed`, `base_unavailable`, `changed_during_write`, or `rev_changed`. |
| `malformed` | The target has a defect that blocks this write (see "Malformed records"). `details`: `{path, defects: [{kind, line?, key?, section?}]}`. |
| `invalid_op` | Bad arguments or values. `details`: `{op_index?, field, reason}`. |
| `similar_exists` | A similar record exists (C1). `details.candidates`. |
| `outside_root` | A path or write destination resolves outside the library root. |
| `lock_timeout` | A lock was not acquired within 10 s. |
| `library_missing` | A configured `library_root` is missing or lacks a valid `.engagements/library.yaml`. (The default root is created on first use instead, with warning `library_created`.) |
| `no_session` | No session id on the call (T5). |
| `io_error` | Any other filesystem failure. `details.path` is relative when inside the root. |

### Error precedence

Each call runs these stages in order and returns the first error. Within stage 1, fields are checked in the schema's property order, and ops by index.

1. Argument validation: schema, per-op shape (T1), single-line and length rules. Yields `invalid_op`.
2. Library open. Yields `library_missing` or `io_error`.
3. Target resolution. Yields `not_found`, `ambiguous`, `outside_root` or `unbound`.
4. Kind and value validation against the resolved record, and global defects. Yields `invalid_op`, `malformed` or `not_found` (unknown `client`).
5. Lock acquisition. Yields `lock_timeout`.
6. Revision check (spec 5.3 step 3). Yields `conflict`.
7. In-memory application and post-apply structural check. Yields `invalid_op` or `malformed` (targeted defects).
8. Commit (spec 5.3 steps 5 to 7). Yields `conflict` (`changed_during_write`) or `io_error`. Failures after the replace return `ok: true` with `maintenance_warning`.

Stage 4 runs before the lock (spec 5.3 step 1) against a pre-lock read. After the lock is acquired and the current bytes are read, stage 4 runs again against those locked bytes, before the revision check. A newly observed global defect returns `malformed`, subject only to the C5 repair exception. No pre-lock validation result authorizes writing different locked bytes. If locked-byte validation finds a global defect not covered by the C5 repair exception, `malformed` takes precedence over the identity-change check. Otherwise, if the record's `engagements` kind or `id` differs between the two reads, the result is `conflict` / `footprint_changed`, with that property as the footprint.

Within stage 1, reserved names are recognized before generic schema rejection: an op `key` of `updated`, or a `section` normalizing to `change log` (on create or update), returns `invalid_op` with `reason: "plugin_owned"`, not `invalid_value` or `unexpected_field`.

A lookup reference — `engagements_context.id`, `engagements_bind.client` and `engagements_bind.project` — is one of the stage 1 single-line rules: at most 200 characters, no control characters, `details.field` naming the argument. Resolution repeats the check, but stage 1 decides first, so a reference that is not one line is the same `invalid_op` whether or not a library exists, and a rejected call never opens a library on the way to refusing it. (An `id` or `base_rev` on `engagements_update` is a record id, matched by the schema's own pattern in stage 1.)

### Shared value formats

| Name | Format |
|---|---|
| id | `^[a-z0-9][a-z0-9-]{1,62}$` |
| rev | `^[0-9a-f]{16}$`: the first 16 hex characters of the SHA-256 of the full file bytes |
| normalized text | NFKC, casefold, each Unicode punctuation or symbol character mapped to a space, whitespace collapsed, trimmed |
| section key | NFKC, casefold, whitespace collapsed, trimmed (spec 4.6). Every section selector in every tool uses this key. |
| single-line string | nonblank after trimming, no `\r`, `\n`, or other C0/C1 control characters, no U+2028 or U+2029 (YAML line breaks), no surrogate code points, and no U+FFFE or U+FFFF (YAML cannot carry them) |
| active | client `status: active`; project `status: active`. `paused`, `former` and `historical` are inactive. |
| JSON property values | YAML strings and lists of strings pass through. Dates and datetimes become ISO 8601 strings. Booleans, finite numbers and null pass through. Any other YAML type, including a non-finite float (`.nan`, `.inf`, `-.inf`), becomes `{"unsupported_yaml": "<type name>"}` and is a targeted `unsupported_yaml` defect on that property. |

## Filesystem boundary (all tools)

- Explicit paths (`engagements_context.path`) obey the same exclusions as discovery: no dot-directory components, no symlinked file or directory component below the root, `.md` regular files only. An escape is `outside_root`; an excluded in-root path is `not_found` with `reason: "excluded"`.
- Every record write verifies the resolved destination's parent is inside the resolved root (spec 4.1).
- The root is resolved once, when the library is opened, and is not resolved again. Every later read or write reopens it without following a link and checks that it is still the directory that was opened; a root, or an ancestor of it, replaced since then fails with `io_error`, `reason: "root_changed"`, and nothing is read or written.
- Plugin metadata (`.engagements/` and everything below it) is never a record target, even though snapshots and history files carry record markers. Before writing metadata, the plugin verifies that `.engagements` and the subdirectory it writes are real directories, not symlinks. Otherwise the metadata write fails, with the consequence set by the failure policy below.
- Every directory the plugin reads from or writes to (the record's directory, `locks`, `history/<id>`, `snapshots`) is opened without following a link, one component at a time from the root, and held open for the operation, with every directory above it still held. A directory swapped for a symlink after it was verified cannot redirect a write. On Windows each directory is held by a handle opened without following a reparse point and without delete sharing (so it cannot be renamed or deleted under the operation), and every child open, create, replace, hard link and delete is made relative to the held handle (`NtCreateFile`, `FileRenameInformation`, `FileLinkInformation`, `FileDispositionInformation` with `OBJECT_ATTRIBUTES.RootDirectory`), never through a path rebuilt from it. That is what confines the write: a reparse point set on the held directory cannot redirect a relative operation, because the kernel refuses to resolve the name (`WinError 1921`) and nothing lands outside, which `tests/test_windows_native.py` proves natively. Write sharing is granted, since the file system opens the directory for write itself when a link or a rename adds a name to it (withholding it fails those with `WinError 32`), and a reparse point can only be set on an empty directory (`WinError 145` otherwise), so sharing is not what protects it. New directories are created with `FILE_CREATE | FILE_DIRECTORY_FILE` and their handle is returned directly, so there is no create-then-open window. Windows has no directory fsync: the directory sync is a no-op there and no durability equivalence with POSIX is claimed. A platform with neither mechanism refuses every write with `io_error`, `reason: "unsupported_platform"`; reads still work.
- **Metadata failure policy.** Failure to write the required pre-update history file (spec 5.3 step 5), including a metadata-boundary failure, aborts before the replace with `io_error`, and the record is untouched. A read-snapshot failure returns the read with a `snapshot_failed` warning. Failures after the replace (directory fsync, committed-byte snapshot, history or snapshot pruning) return `ok: true` with a `maintenance_warning`. A metadata entry that was published but could not be re-checked afterwards (the history copy, the read snapshot, the committed-byte snapshot) is a `maintenance_warning` on the successful result, never an error.
- A create whose discovery under the library lock could not read a candidate record or a subtree fails with `io_error`, `details.reason: "incomplete_scan"` and `details.path` naming the first unreadable relative path, before any id is allocated or similarity is decided: an inventory with holes cannot prove an id is free or that nothing similar exists (spec 4.4, C1). Symlinks, oversize files and dot-directories are intentional exclusions, not read failures, and never block a create, even when a symlink's target cannot be checked or an oversize file cannot be opened. `find` and `resolve` still answer from what they could read.
- A create or update is `invalid_op` with `reason: "too_large"`, and nothing is written, when the resulting file would exceed 1 MiB, or when the frontmatter's closing `---` line would not lie entirely within the first 65,536 bytes (BOM included). This keeps records discoverable (spec 4.1 and "Malformed records").
  - **Ordered updates.** The 1 MiB limit applies only to the resulting file. The frontmatter limit also applies to each intermediate state: after each op, and after the plugin's automatic Change log and `updated` edits. An op that would push the closing `---` line past 65,536 bytes is `too_large`, even if a later op in the same request would shrink the frontmatter again. A client that needs to grow one property and shrink another near the limit sends the shrinking op first. This keeps intermediate frontmatter within the discovery window and bounds per-op frontmatter parsing to it.

## Malformed records

**Recognition (C2).** Discovery reads at most the first 64 KiB. If the file starts with a `---` line, it finds the closing `---` line within that window. If the block parses as YAML, the marker is the `engagements` key. If it does not parse, a line scan of the block looks for a line matching `^engagements:[ \t]*(client|project)[ \t]*$`. A match makes the file a malformed record of that kind. Its `id` and `name` come from lines `^id:[ \t]*(\S.*)$` and `^name:[ \t]*(\S.*)$` if present (trimmed, surrounding quotes removed), otherwise `null`.

**Global defects** block every write to the record:
- `encoding` (not UTF-8)
- `mixed_eol`
- `yaml_parse` (also when the frontmatter nests too deeply to parse safely: any `RecursionError` while composing, parsing or constructing it is reported as `yaml_parse`, never raised)
- `missing_field` (`id`, `name`, or a project's `client`)
- `invalid_id` (also when `id` is not a YAML string, for example `0xFF`, `0123` or `true`; the id is never coerced from another type)
- `invalid_type` (a field whose value has a YAML type the field does not accept: `name`, or a project's `client`, that is not a string, for example `client: true` or `name: 123`; the field reads as `null`)
- `duplicate_id` (this id appears on two or more records)
- `unknown_client` (a project whose `client` is unknown or not a client)

The one exception: for `unknown_client`, a `set_property client` op that names a valid client is allowed, so the record can be repaired.

**Targeted defects** block only ops whose footprint touches them:
- `duplicate_key`, `unsupported_yaml` (nested mapping, multiline scalar, anchor or tag) on a property
- `invalid_type` on an optional property whose value has a YAML type it does not accept: `status` that is not a string, or `aliases` that is not a list of strings. An explicit `null` (`~`, or an empty value) is not a value of the wrong type: it stays defect-free, reads as `null`, and doctor reports nothing.
- `duplicate_section` on a section key

**Reading.** `engagements_context` returns a malformed record with `valid: false`, one `record_invalid` warning per defect, and `null` for any identity field it could not read. For an `encoding` defect, `properties` is `null`, `sections` is empty, and `raw_text` carries the bytes decoded as UTF-8 with replacement characters, paginated with the same rules as sections, including the response bound (see **Bounds**); `next_offset` and `view_length` then describe that raw view, not a section view. An encoding defect leaves the id unreadable, so `engagements_update` by id is `not_found` with `reason: "no_match"`. A record with a duplicated id can be read only by `path`; by id it is `ambiguous`.

## Parsing, fences and filenames (rev 2, spec C6)

These rules make spec 4.4 and 4.6 exact. Each needs a fixture or unit test.

**Frontmatter boundaries**
- An optional UTF-8 BOM precedes the first line and is preserved. It counts toward the 64 KiB discovery window and the 65,536-byte frontmatter limit.
- The first line after the BOM is exactly `---`, and the block closes at the next line that is exactly `---` (nothing else before the EOL). A file with no closing line inside the discovery window is not a record: discovery skips it and an explicit `path` read is `not_found` with `reason: "not_a_record"`. The C2 marker scan never reads past that window or into the body.

**Property spans**
- Properties are identified from the composed YAML of the frontmatter block (`yaml.compose`), before any dictionary is built, so every duplicate pair stays visible. The property name is each top-level pair's key as decoded by YAML. The marker scan of C2 is only the fallback for a block that does not parse.
- A pair is canonical when its key is a plain scalar starting at column 0 and immediately followed by `:`. Any other key spelling makes that property `unsupported_yaml` under its decoded key: a quoted key, a space before the colon, an explicit `?` key, or a non-scalar key. Such a property is never treated as absent.
- **Locating spans.** Each pair's value extent is taken from the YAML event stream (`yaml.parse`): the end mark of the value's scalar, sequence-end, mapping-end or alias event. For a block sequence (indented or compact column-0 `- ` items), use instead the end mark of the event that closes its last item (that item's scalar, alias, sequence-end or mapping-end event). PyYAML places a block sequence-end mark after any blank and comment lines that follow the last item, and those lines belong to no property. Comment or blank lines between two items stay inside the span, and the property stays `unsupported_yaml`. Composed nodes are not used for extents, because an alias reuses the anchored node and its marks. Positions come from mark indices (character offsets into the decoded frontmatter text, converted to byte offsets), never from YAML line numbers. YAML also treats U+0085, U+2028 and U+2029 as line breaks, so its line numbers drift from the file's lines. The file's lines are split on its EOL (LF or CRLF).
- A property's span runs from the start of the file line containing the key's start index through the end of the file line containing the value's end index, including that line's EOL. When the end index is at the start of a file line, the span ends with the previous line. A span is supported only if it contains no blank line and no comment-only line, and its value is either a scalar written entirely on the key line (its source text contains no LF, CR, U+0085, U+2028 or U+2029) or a sequence (flow, block, or compact column-0 `- ` items) whose items are single-line scalars. Anything else is `unsupported_yaml`. That includes anchors, aliases, tags, nested collections, block scalars (`|`, `>`), scalars that continue onto another line, and sequences interrupted by a blank or comment line. A trailing comment on a span's line is part of the span and is not kept by a rewrite.
- A span containing a literal U+0085, U+2028 or U+2029 anywhere, including outside a scalar's source text, is `unsupported_yaml`. When two properties' spans share a file line, both are `unsupported_yaml`. No rewrite may then reach a neighbouring property.
- Bytes between spans (blank lines, comments) belong to no property and are never rewritten.
- Two or more pairs with the same decoded key make `duplicate_key` on that key. No write may touch any of its spans.
- **Identity escalation.** `duplicate_key` or `unsupported_yaml` on `engagements`, `id`, `name`, or a project's `client` is a global defect. It is reported under its own code, blocks every write to the record (the C5 repair exception does not apply), and the field reads as `null`.
- **Repeated markers.** Marker occurrences are inspected before duplicates collapse. A file is recognized as a record if any `engagements` pair has the value `client` or `project`. Its kind is that value when every `engagements` occurrence has that same value, including occurrences with unrecognized values; otherwise it is `null`. A repeated marker is a global `duplicate_key`. A record of kind `null` is reported by `doctor` and is readable by `path` with `kind: null`; `engagements_find` and binding never return it.
- **Rewriting.** `set_property` replaces the whole span. An existing flow list stays a flow list, written on one line as `key: [a, b]` with comma-space separators. An existing block list stays a block list, using the indentation and dash prefix of its first item. A new or empty list is written as flow (`key: []` when empty).
- **Quoting.** A scalar is written plain when `yaml.safe_load` of `key: <plain>` returns exactly `{key: value}`. Each list item is decided on its own, in its target style: plain when `yaml.safe_load` of a one-item list (`key: [<plain>]` for flow, or `key:` plus one block item line with the target prefix) returns exactly `{key: [item]}`. Anything that fails its test is written double-quoted using `json.dumps(item, ensure_ascii=False)`. The single-line string rule (Shared value formats) keeps every allowed value representable this way.

**Fences and headings**
- An opening fence is a line matching ``^ {0,3}(`{3,}|~{3,})``. For a backtick fence, the rest of the line must not contain a backtick. The fence closes at the first later line matching `^ {0,3}` followed by at least as many of the same character and then only `[ \t]*`. In a record, an unclosed fence runs to end of file. Lines inside a fence are never headings.
- Inserted `body` or `text` is unbalanced when, parsed alone under these rules, a fence is still open at its end. Heading-looking lines inside a balanced fence are allowed. This is the exact meaning of spec 5.2's "no `## ` heading lines".
- A heading's title is the text after the `#` run, trimmed, with a closing sequence removed. A closing sequence is a run of `#`s at the end of the heading, preceded by an ASCII space (U+0020) or tab (U+0009) and followed by only ASCII spaces or tabs, as in CommonMark. Other Unicode whitespace does not delimit one: with NBSP (U+00A0) or EM SPACE (U+2003) on either side, the `#`s stay in the title, so `## Notes ###` followed by NBSP has the title `Notes ###`. `doctor` warns about such a heading (`heading_closing_sequence`).

**Section geometry**
- A level-1 heading ends the section before it but never starts one. Content between a level-1 heading and the next level-2 heading belongs to no section and is never edited. Insertion points (before `## Change log`, or at end of file) are positional. The plugin never moves a level-1 heading, and the post-apply structural check compares only the ordered section key list and the untouched bytes.
- `replace_section` replaces the whole span, including its trailing blank lines. The following heading and everything after it are untouched.
- Creating `## Change log` in a file without a final EOL: the last line gains the file's EOL, then a blank line is added only if that last line is not blank, then the heading follows, and the plugin's appended line ends without an EOL. The file's final-EOL state is preserved.

**Ids.** The T11 order (NFKC, NFKD, drop combining marks, casefold, then slugging to `[a-z0-9-]`) supersedes the shorter description in spec 4.4. The short-slug fallback applies once, to the base, before any collision suffix. Every candidate must match the id format.

**Filenames (spec 4.4 made exact).** The stem is derived from `name`:
1. NFC-normalize.
2. Replace each control character (Unicode category `Cc`), `/`, `\` and each of `<>:"|?*` with a space.
3. Collapse whitespace runs to one space, and trim.
4. Strip leading and trailing `.` and space characters until stable. A name of `..` becomes empty.
5. Truncate to at most 80 code points and at most 200 UTF-8 bytes, at a code-point boundary, then strip trailing `.` and spaces again.
6. If the stem is empty, use the id.
7. If the part of the stem before its first `.`, with trailing spaces removed, matches a Windows-reserved device name case-insensitively (`CON`, `PRN`, `AUX`, `NUL`, `COM0` to `COM9`, `LPT0` to `LPT9`, `COM¹`, `COM²`, `COM³`, `LPT¹`, `LPT²`, `LPT³`), insert `_` immediately after that part. For example, `CON` becomes `CON_` and `aux.txt` becomes `aux_.txt`. This also covers an id fallback such as `con`.

Placement and collisions:
- A client is published as `<root>/<stem>/Client.md`. A project is published as `<directory of its client's record>/Projects/<stem>.md`.
- A candidate collides when any existing entry in the target directory has the same name after NFC normalization and casefolding. This covers case-insensitive filesystems and sync. On a collision, try `<stem> (2)`, `<stem> (3)` and so on. The suffix is outside the 80-code-point and 200-byte cap. A new client never reuses an existing directory.
- Exclusive create remains the final guard. If it reports that the target exists (a race), move on to the next suffix.
- `Projects` is resolved with the same NFC-plus-casefold comparison against its sibling entries. A single matching entry that is a real directory, not a symlink, is reused with its existing spelling. If there are several matches, or the match is anything else, the create is `io_error` and nothing is written. `Projects` is created only when nothing matches.

## Section editing rules

Terms: the file's EOL is LF or CRLF (spec 4.6). A section's **span** runs from its heading line through the line before the next level-1 or level-2 heading, or end of file. Its **body** is the span minus the heading line. Input `body` and `text` values have CRLF normalized to LF, and trailing blank lines and trailing newlines stripped. Each line is then written with the file's EOL.

- **Heading detection** uses the record parser's rule: a line matching `^ {0,3}#{1,2}(?:[ \t]|$)` outside a fenced block is a level-1 or level-2 heading. Inserted `body` or `text` containing such a line is `invalid_op` with `reason: "heading_in_text"`. Inserted text whose fences (`^ {0,3}(```+|~~~+)`) are unbalanced when parsed alone is `invalid_op` with `reason: "unbalanced_fence"`.
- **Post-apply structural check:** the result is re-parsed. Its frontmatter must parse. Its ordered list of section keys must equal the expected list: the original list plus any sections the ops created, at their insertion points, plus `## Change log` when the plugin created it. Its bytes outside the edited spans must be identical, where the edited spans include the plugin's automatic `updated` property and Change log edits. Otherwise the result is `invalid_op` with `reason: "structure_changed"`, and nothing is written.
- **Existing heading lines are never rewritten.** The selector only finds the section; the file's heading text, spelling and casing stay.
- `replace_section`: the new span is the heading line, then the body lines if the body is non-empty, then one blank line if another heading follows the span. An empty body leaves the heading plus that separator.
- `append_to_section`: `text` must be non-empty after stripping (`invalid_op`, `reason: "empty_text"`). It is inserted after the body's last non-blank line, or directly after the heading when the body has none. The section's trailing blank lines stay after the inserted text.
- `replace_line`: replaces the content of the one matched body line and keeps its EOL. The heading line is never matched.
- `remove_line`: deletes the matched body line and its EOL.
- **Creating a missing section** (`replace_section` or `append_to_section` on an absent key, or a non-template section in `engagements_create`) inserts `## <title as given, trimmed>`, its body lines, and one blank line immediately before the `## Change log` heading.
- **Change log:** if `## Change log` is missing, it is created at end of file first. That means ensuring a final EOL, adding one blank line if the last line is not blank, then `## Change log`. The plugin's line is appended with the `append_to_section` rule.
- **Final newline:** whether the file ends with an EOL is preserved. When an insertion lands at end of file in a file without a final EOL, the inserted block also ends without one.
- **Duplicate headings:** reads return every duplicate section in file order, and the key produces both a `record_invalid` warning (for the `duplicate_section` defect) and a `duplicate_section` warning. Writes targeting a duplicated key are `malformed`.

## Footprints and rebase (spec 5.3 step 3, made exact)

- A **property footprint** is the raw text of the complete property span defined in "Parsing, fences and filenames" (including compact column-0 sequence items), with EOLs normalized to LF. For a duplicated key it is the concatenation of every span of that key, in file order. It is `null` when the key is absent.
- A **section footprint** is the raw text of the section's span, heading line included, with EOLs normalized to LF and trailing blank lines removed. It is `null` when the section is absent.
- Footprint equality is string equality, and `null` equals `null`. A section created by an op protects that section's existence and span only, not the surrounding insertion region.
- All footprints are computed on the untouched base and the untouched current **before any op is applied**. Later ops are never compared against intermediate states. If every footprint is equal, the ops are applied in order to the current text. If any differ, the result is `conflict` / `footprint_changed`, listing every differing footprint, and nothing is written.
- `updated` and the Change log are never footprints (T9).

## Change log line

`- <date> <profile>: <summary> (source: <source>)`

- `<date>` is the host's local date, `YYYY-MM-DD`, from an injected clock.
- `<profile>` is the Hermes profile name, `default` when unknown.
- `<source>` is the single-line source string, with any `)` kept as is.
- `<summary>` is the op summaries joined by `; `. Op summary templates:

| Op | Summary |
|---|---|
| create | `created <kind>` |
| `set_property` | `set <key>` |
| `replace_section` | `replace section <Title>` |
| `append_to_section` | `append to <Title>` |
| `replace_line` | `replace line in <Title>` |
| `remove_line` | `remove line from <Title>` |

`<Title>` is the heading text as it appears in the file, or as given when the op created the section.

## Tool descriptions (model-facing)

Phase 0 showed these tools are reached through Tool Search by default (spec A3). A unit test asserts each first sentence is at most 60 characters.

| Tool | Description |
|---|---|
| `engagements_find` | Find a client or project engagement by name or alias. Returns ids, names, status and file paths, never record contents. Use before creating a record, to avoid duplicates. |
| `engagements_bind` | Bind this chat to a client or project engagement. Returns the client's summary, contacts, locations and active projects. Use mode "off" to unbind or "auto" to resume folder-based binding. |
| `engagements_context` | Read a client or project record and its locations. With no id it reads the bound record. Returns properties, sections and the rev needed for engagements_update. |
| `engagements_create` | Create a new client or project engagement record. Run engagements_find first, show the user the draft, and save only what they confirmed or marked as assumed. |
| `engagements_update` | Update a client or project record with targeted edits. Pass the rev from your last read as base_rev. Never rewrite a whole record to change one line. |

## `engagements_find`

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "maxLength": 200, "description": "Text to match against id, name and aliases. Empty string lists everything."},
    "kind": {"type": "string", "enum": ["client", "project"]},
    "include_inactive": {"type": "boolean", "default": false},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    "offset": {"type": "integer", "minimum": 0, "default": 0}
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Behavior**
- The normalized query is compared to the normalized id, name and each alias. Each field scores a tier: exact (3), prefix (2) or substring (1). A record's tier is its best field tier. `match` reports the field that produced that tier, preferring id, then name, then alias on ties, with the matched value as stored. This tier rule is normative, for ordering and for `match`: a client matched by an exact alias reports `alias`, even when its id also has a weaker prefix match. An empty query matches everything at tier 1, and `match` is then `null`.
- Results are ordered by tier (high first), then clients before projects, then normalized name, then id, then path. That order is the sort key: pages of one query are consecutive windows of it, so with `limit` and `offset` a caller can read every match exactly once while the records say the same thing.
- Malformed records with a non-null id and name are included with `valid: false`. A record whose kind and id are readable but whose `name` is present with a value that is not a string (including a mapping or a list) is included too, with `name: null`. A record with no usable name — no `name` property, or a `null` one — is only counted, whatever other defects it has. Other malformed records are counted in a `malformed_skipped` warning.
- Never returns record bodies.
- A walk that stopped at its traversal budget (the library root holds more than 200,000 directory entries) returns the matches it read and a `discovery_incomplete` warning; the records it did not reach are not listed.

**Data:** `{"results": [{"id", "kind", "name", "status", "client", "path", "valid", "match": {"field", "value"} | null}], "total", "truncated", "next_offset"}`. `client` is `null` for clients. `total` counts all matches before `limit` and `offset`. `truncated` is true when matches remain after this page. `next_offset` is the `offset` of the next page (`offset` plus the number of results returned), or `null` when this page is the last; an `offset` at or past `total` returns no results and `next_offset: null`.

## `engagements_bind`

```json
{
  "type": "object",
  "properties": {
    "client": {"type": "string", "maxLength": 200, "description": "Client id, or exact name or alias."},
    "project": {"type": "string", "maxLength": 200, "description": "Project id, or exact name or alias. Implies its client."},
    "mode": {"type": "string", "enum": ["explicit", "off", "auto"], "description": "Omit to infer: explicit when client or project is given, otherwise off."}
  },
  "additionalProperties": false
}
```

**Behavior**
- `client` and `project` resolve per T3. A project alone binds its own client. A project whose client differs from the given client is `invalid_binding` and changes nothing.
- `mode: "explicit"` with neither argument, or `off`/`auto` with an argument, is `invalid_op`.
- Binding an inactive or malformed record is `invalid_binding`.
- The identity, status, defects and project/client relationship are read again from the same bytes that produce the returned `rev` and content. A record that changed after resolution is `invalid_binding` with `record_missing`, `record_identity_changed`, `record_inactive`, `record_malformed` or `client_changed`, and nothing is stored.
- Without a session id the result is `no_session` (checked after argument validation).
- The binding is stored under `binding:<library_id>:<session_id>` (spec 6.4).
- For `auto`, the data reports the current auto resolution (spec 6.4). `unbound`, `ambiguous` and `cwd_unsupported` are all successful results. When a folder would match exactly one project but the library could not be read in full, the state is still `unbound`, with reason `discovery_incomplete`, and the data lists the records in `unreadable`. A competing project counts as unreadable when its bytes could not be read, or its `locations` did not arrive intact from the same read: the parser recorded a defect against that property, or the value is not a list of strings. A project with no `locations` key, an empty list, or clean locations beside defects elsewhere is still read as it says. A library whose walk stopped at its traversal budget is `discovery_incomplete` too, and `unreadable` then names the root, `.`.
- Section texts are capped at 4,000 characters each, cut at a line boundary, with `partial: true` when cut. Unlike `engagements_context`, these summaries have their trailing blank lines trimmed. The pair is bounded like a summary: a record's `name` and each section title are cut to 200 characters, a project keeps at most 20 `locations` of at most 200 characters each, and `active_projects` holds at most 50 rows whose `name` is cut to 200 characters. A record, section or locations list that was cut carries `truncated: true`; a client with more active projects than that gets the first 50 in the list's own order plus `active_projects_total` and `active_projects_truncated: true`.

**Data**

```json
{
  "mode": "explicit", "state": "bound", "method": "explicit", "evidence": "explicit 14:02",
  "client": {"id": "", "name": "", "status": "", "rev": "", "path": "", "sections": [{"title": "", "text": "", "partial": false}]},
  "project": {"id": "", "name": "", "status": "", "rev": "", "path": "", "locations": []},
  "active_projects": [{"id": "", "name": "", "status": "active"}],
  "candidates": []
}
```

- `mode` is one of `explicit`, `off`, `auto`.
- `state` is one of `bound`, `unbound`, `ambiguous`, `cwd_unsupported`.
- `method` is one of `explicit`, `auto`, `off`.
- `evidence` uses the spec 6.3 `/client` format, from an injected clock.
- `client` and `project` are `null` when not bound.
- `client.sections` holds Summary, Contacts and Locations in that order, omitting any the record lacks.
- `candidates` (`[{id, kind, name, client}]`) is non-empty only when `state` is `ambiguous`.
- `unreadable` (`[path]`, at most 10, library-relative) is present only when a folder match was refused because records could not be read or their `locations` could not be trusted (`state: "unbound"`, reason `discovery_incomplete` in the error path). The full count is in `evidence`.
- `truncated: true` appears on `client` or `project` when its `name` (or the project's `locations`) was cut, and on an `active_projects` row whose `name` was cut. `active_projects_total` and `active_projects_truncated: true` appear together only when the client has more active projects than `active_projects` can hold (more than 50 rows).
- The data has no `reason` field. A resolution reason appears only in the `unbound` error details or in an `invalid_binding` error; in successful data it is carried by `state`, `method`, `evidence` and, when a folder match was refused, `unreadable`.

## `engagements_context`

```json
{
  "type": "object",
  "properties": {
    "id": {"type": "string", "maxLength": 200, "description": "Record id, or exact name or alias."},
    "path": {"type": "string", "maxLength": 1024, "description": "Record path, relative to the library root or absolute inside it."},
    "kind": {"type": "string", "enum": ["client", "project"], "default": "client", "description": "With no id or path, which bound record to read."},
    "sections": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 50, "description": "Section titles to return. Omit for all; empty list for properties only."},
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "rev": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "Required when offset > 0: the rev from the previous page."}
  },
  "additionalProperties": false
}
```

**Behavior**
- Passing both `id` and `path` is `invalid_op`. With neither, the tool reads the bound record of `kind`. If the session has no such binding, the result is `unbound`. A binding without a project, read with `kind: "project"`, is also `unbound`.
- Every read snapshots the full bytes under the returned rev (spec 5.1), including filtered and paginated reads.
- If `rev` is given and differs from the current rev, the result is `conflict` / `rev_changed`. `offset > 0` without `rev` is `invalid_op` with `reason: "rev_required"`. The offset is checked before the revision: an `offset` at or past the end of the view is `invalid_op` with `reason: "offset_out_of_range"` even when the given `rev` is also stale.

**Unbound details.** When no record is read, the `unbound` error carries `details.mode`, `details.state`, `details.reason`, `details.evidence`, `details.candidates`, and `details.auto`, the live automatic resolution as `{state, reason, evidence, candidates}`. `state` is `unbound`, `ambiguous`, `cwd_unsupported`, `invalid_binding`, or `bound` when a client-only binding is read with `kind: "project"` (`reason: "no_project"`). An invalid binding is not a separate error code: it surfaces here as state `invalid_binding`, with the revalidation reason (`record_missing`, `record_identity_changed`, `record_inactive`, `record_malformed` or `client_changed`) and `candidates: []`. A stored binding that cannot be read at all — not the object the plugin wrote, or a mode it does not know — is `unbound` too, with `state: "invalid_binding"`, `reason: "stored_unreadable"` and `candidates: []`, and a `message` that says the stored binding is unreadable, that nothing is bound, and to pick a client with `engagements_bind`; `details.auto` still reports the live automatic resolution. `auto` is reported independently of the stored mode. Messages that name the entity use the configured `labels.client` and `labels.project` display words, which default to `client` and `project`. A folder match refused because the library could not be read in full, or because a matching project's `locations` could not be trusted, is `state: "unbound"` with `reason: "discovery_incomplete"`, and both the details and `details.auto` then carry `unreadable`, up to 10 library-relative paths, with the full count in `evidence`; `candidates` stays empty, because nothing was matched with confidence.

**Pagination**
- **View:** the selected sections in file order. For each, its full span (heading line through the end of the span), decoded, with CRLF converted to LF. One `\n` is added at the end if the last span lacks it. Offsets count Unicode code points into the view. A section's `text` is its body as written, so trailing blank lines inside the span are kept.
- **Page:** up to 12,000 code points starting at `offset`. If the page does not reach the end of the view, it ends just after the last `\n` inside the window, unless the window contains no `\n` (a single line longer than a page), in which case it is cut at 12,000.
- `next_offset` is where the page actually ends: `null` when the page reaches the end of the view, otherwise an offset greater than `offset` that continues it. A page ends early, at a section boundary, when its section index would hold more than 400 rows or when the whole response would exceed its byte bound (see **Bounds**). `next_offset` is then the offset of the first heading the page did not list, so a reader that follows `next_offset` until `null` reads every body exactly once. A page that is a single section too large for the byte bound is cut inside that section's text and its entry is `partial: true`.
- An `offset` greater than or equal to the view length is `invalid_op` with `reason: "offset_out_of_range"`. The one exception is `offset: 0` on an empty view, which returns no sections.
- **Section entries:** each section whose span intersects the page appears once in `sections`. Its `text` is the intersecting part of its body, with the heading line excluded. It is `partial: true` unless its whole span lies inside the page. A heading-only intersection has `text: ""`; `partial` is still determined solely by whole-span coverage, so an empty section whose span is just its heading, fully inside the page, is `partial: false`.
- Requested sections the record lacks are listed in `missing_sections`, each title echoed as it was requested and so no longer than the request's own 200-character bound. A request naming the same section key twice returns it once. Titles the response bound leaves no room for are a prefix of the request in request order, and `missing_sections_omitted` is how many the response did not return.

**Bounds.** A read is bounded as a whole: the envelope the tool returns — data, warnings and truncation markers included, serialized as JSON — stays within 65,536 bytes for any record inside the 1 MiB record limit. Each part is clipped first, so the body page is the only thing that gives up room. Every heading in `sections` is cut to 200 characters, and `sections` lists the first 400 of the headings the page covers. A property value string is cut to 2,000 characters, a list value keeps at most 32 items, a property key longer than 120 characters is omitted, at most 64 keys are returned, and the properties together stay within their own serialized bound. A related row's `name` and each of its `locations` are cut to 200 characters and a row keeps at most 20 locations; `projects` holds at most 50 rows and the rows together stay within their own serialized bound. Diagnostics are cut to 16 plus one count, and each quotes at most 120 characters of the heading or key it names. Markers: a section entry whose heading was cut carries `truncated: true` beside `partial`; a page whose section index was cut carries `sections_total`, the number of headings its page window covers, with `sections_truncated: true`; the keys of properties whose value was cut are listed in `properties_truncated`, and keys dropped for their count, their length or the properties' size bound are counted in `properties_omitted`; requested titles the response could not hold are counted in `missing_sections_omitted`; a related row that was cut carries `truncated: true`, and a client's `projects` that lost rows carries `projects_total` and `projects_truncated: true`; a project's `client` whose name was cut carries `truncated: true`; and when more defects were found than are listed, whether by the 16-entry cap or by the response bound, the last warning has code `record_defects_omitted`, its `omitted` field is how many are missing, and its message says so. A raw page is text rather than sections, and it is bounded the same way: JSON escapes the code points the record holds, so the page is cut until the envelope fits and `next_offset` still advances. Clipping applies to the copy the tool returns: no record on disk is changed and `rev` still describes the file as written.

**Data**

```json
{
  "id": "", "kind": "client", "path": "", "library_root": "", "rev": "", "valid": true,
  "properties": {},
  "sections": [{"title": "", "text": "", "partial": false}],
  "missing_sections": [], "offset": 0, "next_offset": null, "view_length": 0,
  "projects": [{"id": "", "name": "", "status": "", "locations": []}],
  "client": {"id": "", "name": ""},
  "raw_text": null
}
```

- `kind` is `client`, `project`, or `null` for a recognized record with disagreeing markers (C6 "Repeated markers").
- `properties` holds every frontmatter key, converted per "JSON property values". For a `yaml_parse` defect, `properties` is `null`. A duplicated key is omitted from `properties` and reported as a `duplicate_key` defect; one occurrence is never selected silently.
- `projects` is always present on a client read, possibly as an empty list, and lists all of that client's projects, active and inactive. A project read carries `client` instead.
- `projects_total` and `projects_truncated: true` appear together only when the client has more projects than `projects` can hold: more than 50 rows, or a row total over the related-row serialized bound. `projects` then holds the first rows of that list's order and `projects_total` is the number of rows read.
- `sections_total` and `sections_truncated: true` appear together only when one page's section index was cut: `sections` then holds the rows it lists and `sections_total` is the number of headings the page window covers. The page ends at the first heading it did not list, and `next_offset` continues there.
- `sections[].truncated: true` appears only on a section entry whose heading was clipped to 200 characters.
- `properties_truncated` appears only when a property value was clipped, and lists the property keys whose value was cut.
- `properties_omitted` appears only when a property key was dropped from the response: more than 64 keys, a key longer than 120 characters, or the properties' serialized bound reached. It is the number of keys the record holds that the response does not.
- `missing_sections_omitted` appears only when the response bound left no room for every requested title the record lacks; `missing_sections` then holds a prefix of the request, and the field is the number of titles it does not hold.
- A row in `projects` carries `truncated: true` only when its `name` or its `locations` were clipped. A project's `client` carries `truncated: true` only when that name was clipped.
- A warning with code `record_defects_omitted` is the last one in the list, appears only when more record defects were found than are listed, whether by the 16-entry cap or by the response bound, and carries the number not listed in its `omitted` field and in its message.
- Every row in `projects` comes from that project's own bytes at read time, and every row in `engagements_bind`'s `active_projects` comes from the discovery entry for that project, whose file is read again whenever its identity has changed, together with the `locations` that same read produced. A project that is gone, cannot be read, no longer carries its discovered id, or no longer names this client is omitted, never reconstructed from an earlier discovery pass.
- `client` is present only for projects.
- `raw_text` is non-null only for `encoding` defects.

## `engagements_create`

```json
{
  "type": "object",
  "properties": {
    "kind": {"type": "string", "enum": ["client", "project"]},
    "name": {"type": "string", "minLength": 1, "maxLength": 200},
    "client": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$", "description": "Required for projects: the owning client's id."},
    "properties": {
      "type": "object",
      "properties": {
        "aliases": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}, "maxItems": 20},
        "status": {"type": "string", "enum": ["active", "paused", "former", "historical"]},
        "locations": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1024}, "maxItems": 50}
      },
      "additionalProperties": false
    },
    "sections": {
      "type": "array", "maxItems": 30,
      "items": {
        "type": "object",
        "properties": {"section": {"type": "string", "minLength": 1, "maxLength": 200}, "body": {"type": "string", "maxLength": 20000}},
        "required": ["section", "body"],
        "additionalProperties": false
      }
    },
    "source": {"type": "string", "minLength": 1, "maxLength": 200, "default": "conversation"},
    "allow_similar": {"type": "boolean", "default": false}
  },
  "required": ["kind", "name"],
  "additionalProperties": false
}
```

**Behavior**
- `name`, every alias, every location, every section title and `source` must be single-line strings.
- `client` is required for projects and refused for clients. Status must fit the kind: `active|paused|former` for clients, `active|historical` for projects. `locations` is project-only.
- Two `sections` entries with the same section key are `invalid_op` with `reason: "duplicate_section"`.
- Bodies follow the section editing rules (heading and fence checks).
- The record is rendered from the spec template (4.2, 4.3). Templates supply heading order and field structure only, never the illustrative names, aliases, contacts, locations or body text in the spec examples. Unsupplied section bodies are empty; `aliases` and project `locations` default to empty lists; `status` defaults to `active`. Identity fields come from this request; only `updated` and the Change log are generated. A supplied section whose key matches a template section replaces that section's body. Other supplied sections are created before `## Change log`, in request order. The Change log gets `- <date> <profile>: created <kind> (source: <source>)`.
- **Under the library lock:** rescan; check similarity (C1); allocate the id (below) and filename (spec 4.4); publish with exclusive create. An existing file is never overwritten.
- **Similarity (C1):** the set of normalized proposed name and aliases is intersected with the normalized names and aliases of existing records of the same kind, including inactive ones (for projects, only that client's projects). A non-empty intersection is `similar_exists`, with candidates ordered by normalized name then id. With `allow_similar: true`, they are reported in `similar_overridden` instead.
- **Id derivation (T11):** NFKC, NFKD, drop combining marks, casefold. Each run of characters outside `[a-z0-9]` becomes one `-`. Trim `-`. Then:
  - Truncate to 63 characters minus the length of any collision suffix needed, and re-trim `-`.
  - If the result is shorter than 2 characters, prefix it with `<kind>-` (an empty result becomes `<kind>`).
  - On collision with any existing id (clients and projects share one namespace), append `-2`, `-3`, and so on, re-truncating the base so the whole stays at most 63.
- `client` must exist and be a client. Creating a project for an inactive client is allowed, with a `record_invalid` warning naming that client.
- First use of an empty default root creates the library and adds `library_created`.
- A create whose saved record looks like it holds a credential still succeeds and adds a `possible_secret` warning per finding (see "Common result envelope"). The warning is built from the record as written, so a value in the frontmatter, a section body, a heading or the change-log line all report the same way.

**Data:** `{"id", "kind", "path", "rev", "similar_overridden": [candidates]}`

## `engagements_update`

### Model-facing schema (T1, T2)

```json
{
  "type": "object",
  "properties": {
    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"},
    "base_rev": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "The rev from your last read of this record."},
    "source": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Where the fact came from, for the Change log. One line."},
    "ops": {
      "type": "array", "minItems": 1, "maxItems": 50,
      "items": {
        "type": "object",
        "properties": {
          "op": {"type": "string", "enum": ["set_property", "replace_section", "append_to_section", "replace_line", "remove_line"]},
          "key": {"type": "string", "enum": ["name", "aliases", "status", "client", "locations"]},
          "value": {"type": "string", "maxLength": 200, "description": "For key name, status or client."},
          "values": {"type": "array", "items": {"type": "string", "maxLength": 1024}, "maxItems": 50, "description": "For key aliases or locations."},
          "section": {"type": "string", "minLength": 1, "maxLength": 200},
          "body": {"type": "string", "maxLength": 20000},
          "text": {"type": "string", "maxLength": 20000},
          "match": {"type": "string", "minLength": 1, "maxLength": 1000}
        },
        "required": ["op"],
        "additionalProperties": false
      }
    }
  },
  "required": ["id", "base_rev", "source", "ops"],
  "additionalProperties": false
}
```

### Per-op validation schemas (handler and test contract)

```json
{
  "set_property_scalar": {
    "type": "object",
    "properties": {"op": {"const": "set_property"}, "key": {"enum": ["name", "status", "client"]}, "value": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["op", "key", "value"], "additionalProperties": false
  },
  "set_property_list": {
    "type": "object",
    "properties": {"op": {"const": "set_property"}, "key": {"enum": ["aliases", "locations"]}, "values": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1024}, "maxItems": 50}},
    "required": ["op", "key", "values"], "additionalProperties": false
  },
  "replace_section": {
    "type": "object",
    "properties": {"op": {"const": "replace_section"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "body": {"type": "string", "maxLength": 20000}},
    "required": ["op", "section", "body"], "additionalProperties": false
  },
  "append_to_section": {
    "type": "object",
    "properties": {"op": {"const": "append_to_section"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "text": {"type": "string", "minLength": 1, "maxLength": 20000}},
    "required": ["op", "section", "text"], "additionalProperties": false
  },
  "replace_line": {
    "type": "object",
    "properties": {"op": {"const": "replace_line"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "match": {"type": "string", "minLength": 1, "maxLength": 1000}, "text": {"type": "string", "minLength": 1, "maxLength": 1000}},
    "required": ["op", "section", "match", "text"], "additionalProperties": false
  },
  "remove_line": {
    "type": "object",
    "properties": {"op": {"const": "remove_line"}, "section": {"type": "string", "minLength": 1, "maxLength": 200}, "match": {"type": "string", "minLength": 1, "maxLength": 1000}},
    "required": ["op", "section", "match"], "additionalProperties": false
  }
}
```

An op must validate against exactly the schema selected by its `op` (and, for `set_property`, by whether `key` is a scalar or list key). A failure is `invalid_op` with `details.field` naming the first failing field. `details.reason` is `unexpected_field` for a field the op does not allow, `missing_field`, or `invalid_value` (a reason, not an error code). Reserved names are checked first (see "Error precedence").

### Value and text rules (stage 4)

| Key | Kinds | Rule |
|---|---|---|
| `name` | both | single-line |
| `aliases` | both | each single-line; duplicates by normalized text removed, first kept |
| `status` | both | `active|paused|former` (client), `active|historical` (project): otherwise `invalid_op` with `reason: "status_not_allowed_for_kind"` |
| `client` | project | id of an existing client; on a client record, `invalid_op` with `reason: "key_not_allowed_for_kind"` |
| `locations` | project | each single-line; on a client record, `key_not_allowed_for_kind` |

- `source`, `section` and `replace_line.text` are single-line. These are stage 1 argument checks (see "Error precedence"), so a multiline `source` is `invalid_op` before the library opens or the target resolves, and it can never surface as `not_found`.
- `body` and `text` follow the section editing rules.
- A `section` normalizing to `change log` is `plugin_owned` (T9).

### Behavior

- Spec 5.3 write protocol, with footprints and rebase exactly as in "Footprints and rebase".
- `match` is an exact, case-sensitive substring that must hit exactly one body line. Zero or several hits are `invalid_op` with `reason: "match_count"` and `details.count`.
- `replace_section` and `append_to_section` create a missing section. `replace_line` and `remove_line` on a missing section are `invalid_op` with `reason: "section_missing"`.
- Ops apply in order. Any op failing means nothing is written. The frontmatter size limit is checked after each op ("Filesystem boundary", ordered updates).
- `possible_secret` is judged on the record this update saved, not on the ops or their `match` strings (see "Common result envelope"): an entry appears only for a `(kind, matched text)` pair the saved record holds and the record before the update did not. An update that supplies no credential does not warn again for one already sitting in the record, and doctor reports that one; a credential assembled across two ops in one update does warn, and replacing the line that held a value with one that does not removes it silently.

**Data:** `{"id", "path", "new_rev", "rebased", "applied", "change_log_line"}`

**Conflict details**

```json
{"reason": "footprint_changed", "current_rev": "<rev>",
 "footprints": [{"op_index": 0, "type": "section", "key": "contacts", "base_text": "## Contacts\n- ...", "current_text": "## Contacts\n- ...\n- ..."}]}
```

- `reason` is one of `footprint_changed`, `base_unavailable`, `changed_during_write`.
- `type` is `property` or `section`.
- `base_text` and `current_text` are full footprints as defined above, or `null` when absent.
- `footprints` is empty unless `reason` is `footprint_changed`.

## Examples and fixtures

TOOLS.md examples show shapes. The **golden fixtures are the test contract**, and the test suite builds them:

- `tests/fixtures/library/` holds hand-authored records. Their ids are chosen, not derived, so project `acme-website-2026` with name `Website 2026` is valid. Derivation only runs at creation.
- `tests/golden/<tool>/<case>.json`: each case holds `fixture` (a library directory name), `setup` (an ordered list of mutations: write file, replace bytes, delete), `clock` (`2026-11-07T14:02:00-06:00`), `profile` (`studio-dev` unless stated), `session` (`id`, `cwd`, `surface`, stored binding), `request`, and `expected`. Expected revs are computed by the test from the post-setup bytes, never hand-written.
- Required golden cases: one success and one error for each tool, and for each op (both `set_property` shapes). Also the spec section 9 step 6 trio: Contacts conflict, Communication preferences rebase, and stale `status`. Plus `rev_changed` mid-pagination, `base_unavailable`, and each malformed-record defect kind.

Illustrative shapes:

```json
// engagements_find {"query": "acme"}
{"ok": true, "data": {"results": [
  {"id": "acme-bakery", "kind": "client", "name": "Acme Bakery", "status": "active", "client": null, "path": "Acme Bakery/Client.md", "valid": true, "match": {"field": "alias", "value": "Acme"}},
  {"id": "acme-website-2026", "kind": "project", "name": "Website 2026", "status": "active", "client": "acme-bakery", "path": "Acme Bakery/Projects/Website 2026.md", "valid": true, "match": {"field": "id", "value": "acme-website-2026"}}
], "total": 2, "truncated": false, "next_offset": null}, "warnings": []}
```

```json
// engagements_bind {"client": "riverbend-dental", "project": "acme-website-2026"}
{"ok": false, "error": {"code": "invalid_binding", "message": "Project acme-website-2026 belongs to acme-bakery, not riverbend-dental. Nothing was bound.", "details": {"project": "acme-website-2026", "project_client": "acme-bakery", "requested_client": "riverbend-dental"}}, "warnings": []}
```

```json
// engagements_create {"kind": "project", "name": "Holiday Postcards 2026", "client": "riverbend-dental", "properties": {"locations": ["~/clients/riverbend/postcards-2026"]}, "source": "kickoff call 2026-11-06"}
{"ok": true, "data": {"id": "holiday-postcards-2026", "kind": "project", "path": "Riverbend Dental/Projects/Holiday Postcards 2026.md", "rev": "<rev>", "similar_overridden": []}, "warnings": []}
```

```json
// engagements_create {"kind": "client", "name": "ACME bakery"}
{"ok": false, "error": {"code": "similar_exists", "message": "A client named like 'ACME bakery' already exists. Use it, or pass allow_similar to create another.", "details": {"candidates": [{"id": "acme-bakery", "kind": "client", "name": "Acme Bakery", "client": null, "path": "Acme Bakery/Client.md"}]}}, "warnings": []}
```

```json
// engagements_update {"id": "acme-bakery", "base_rev": "<rev of fixture>", "source": "owner email 2026-11-07", "ops": [{"op": "set_property", "key": "status", "value": "paused"}]}
{"ok": true, "data": {"id": "acme-bakery", "path": "Acme Bakery/Client.md", "new_rev": "<rev>", "rebased": false, "applied": 1, "change_log_line": "- 2026-11-07 studio-dev: set status (source: owner email 2026-11-07)"}, "warnings": []}
```

```json
// engagements_update, same base_rev, after setup appended "- Sam Ortiz | manager, Elm St | sam@acme-bakery.example" to Contacts
// ops: [{"op": "replace_line", "section": "Contacts", "match": "jane@acme-bakery.example", "text": "- Jane Rivera | owner | jane@acmebakery.example"}]
{"ok": false, "error": {"code": "conflict", "message": "Contacts changed since your read. Nothing was written. Show the user both versions.",
  "details": {"reason": "footprint_changed", "current_rev": "<rev>", "footprints": [{"op_index": 0, "type": "section", "key": "contacts",
    "base_text": "## Contacts\n- Jane Rivera | owner, final approver | jane@acme-bakery.example | email preferred, no calls before 10am",
    "current_text": "## Contacts\n- Jane Rivera | owner, final approver | jane@acme-bakery.example | email preferred, no calls before 10am\n- Sam Ortiz | manager, Elm St | sam@acme-bakery.example"}]}}, "warnings": []}
```

## Test hooks for implementers

- `writer.update` takes `before_final_rehash`, called after the temp file is fsynced and before the target is re-hashed. A target edit injected there must produce `conflict` / `changed_during_write`, leave the target untouched, and remove the temp file.
- `engagements.fsutil._before_child_mutation(directory, name)` is a module-level seam, `None` in production, called after the held directory has been verified and immediately before a mutating child operation (create, write, link, replace, delete, mtime) on either platform. The native Windows tests use it to set a real mount-point reparse point on a directory the plugin is holding, in the middle of the operation, and assert that nothing lands outside the root; that window cannot be reached from outside the hold.
- A separate `after_final_rehash` hook may demonstrate the window spec 5.5 admits (an edit landing between the re-hash and `os.replace` is overwritten). That test asserts the documented overwrite and that history holds the bytes the plugin read. It must not expect conflict detection.
- Tool handlers are pure functions of `(args, session)`. `session` is a small dataclass (`session_id`, `cwd`, `surface`, `profile`, `config`, `state_get`, `state_set`, `clock`). That keeps `engagements/tools.py` free of Hermes imports (AGENTS.md).
