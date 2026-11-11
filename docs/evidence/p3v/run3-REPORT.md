# Phase 3V Desktop acceptance — run 3

**Result: not accepted.** The mounted-record refresh worked without further user action, but the 900 px layout still has a right-edge overflow. The test used fictional fixtures, a loopback stub, and scratch Desktop and Hermes data behind the test desktop's lock; no copy or reveal control was clicked.

## Version and checklist

Scratch full fork clone at `6b58832d8e8d87c1e380fbaa81a1299f88128057`, upstream `v0.23.0` tag fetched: CLI reported **Hermes Agent v0.23.0+127.g6b58832**, and `git describe --tags` returned `v0.23.0-127-g6b58832d8e`. Source build succeeded. Engagements was installed from the supplied 34387f1 archive. See `version-and-stub.txt`.

| Check | Result | Evidence / limitation |
|---|---|---|
| A. Version and agent plugin | **PASS** | `version-and-stub.txt`, `gateway.txt`, `context.txt`: version above; API and agent context tool worked; gateway/agent Engagements search found no skipped-plugin line. |
| B. Layout | **FAIL** | `layout.jsonl`: default page 937 px, detail and max descendant right 937; 900 px, detail right 900 but **Copy path button right 922.703125**, scroll width 311 versus client width 288; 1280 px, detail and max right 1280. Paths show ellipsis. List and record occupy adjacent columns; grid uses two label/value columns; project row has `pl-7` versus client's `px-2`. Maple project showed `not found here` with max right 1280 on the 1280 px view. Requested default screenshot was attempted via `Page.captureScreenshot` but did not return before the 30-second command timeout; no screenshot is claimed. |
| C. Mounted live refresh | **PASS for observable no-action refresh; refetch instrumentation incomplete** | `refresh.jsonl`, `events-filtered.json`, `gateway.txt`: record rendered without the marker before tool execution; marker appeared while the same view remained mounted, with no further click. Network domain was enabled throughout the interaction, but no `/api/plugins/engagements/` request was observed through renderer CDP; exact `/record` and `/overview` refetch times therefore **NOT EXERCISED/observed**. Event presence and subsequent DOM update do not establish those individual request times. |
| D. Binding chip | **PASS** | `binding.jsonl`, `events-filtered.json`: fresh chat chip absent, `/client riverbend-dental` returned Binding updated and chip became `Riverbend Dental`; a `plugin.engagements.changed` binding frame arrived. |
| E. Console | **NOT EXERCISED for whole-launch coverage** | `events.jsonl`: listener enabled after initial launch, before the deliberate reload and all tested interactions. In the collected interval, no Engagements/plugin warning or error and no runtime exception were recorded; two Electron insecure-CSP development-build warnings were unrelated. Initial startup before attachment is not covered. |

## Mounted refresh timeline (local time on the test desktop, CST)

- 11:30:50.408: update send initiated; 11:30:50.469: Clients navigation.
- 11:30:51.493: selected Website 2026; 11:30:51.995: record rendered, marker absent.
- 11:30:58.887: renderer received WebSocket `plugin.engagements.changed`, payload `what: library`; gateway logged `engagements_update` completed at 11:30:58.888.
- `/overview` and `/record` request timestamps: **not observed** in the enabled renderer Network domain.
- 11:30:59.000: marker first observed in polling, without another click. Poll interval 500 ms; the recorded time is the first observation, not necessarily the exact paint time.

`version-and-stub.txt` records the scratch-only 8-second `STUB_DELAY` harness diff. `context.txt` records the actual base rev. Event listener attached after launch, so this is interaction coverage, not whole-startup coverage.

## Remaining layout defect

Expected every descendant at or inside the 900 px page. Observed the record file's `Copy path` button ending at **922.703125 px**, with 23 px of detail scroll overflow; default and 1280 px passed this measurement. Suspected interaction is the fixed 8rem label column at `desktop/plugin.js:287` combined with the non-shrinking controls in `FolderRow` at lines 143–162; this is a hypothesis, not a verified root cause. The pane-level Radix overflow seen in run 2 is no longer reproduced at default width.

## Isolation and cleanup

`cleanup.txt`: test units inactive, scratch process list and listeners on 18767/18768 empty, scratch directory removed. Original launcher and MIME cache matched their backups byte-for-byte. `~/.config/Hermes` metadata and <50 MB file-hash before/after diffs empty; real Hermes config and state hashes match requested baselines. All recorded ChatGPT/Codex PIDs remained present. Lock readback `locked:true`; idle readback `stayAwake:false`. No real model or key used, no clipboard controls clicked.
