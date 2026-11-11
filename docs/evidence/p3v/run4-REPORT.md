# Phase 3V Desktop acceptance — run 4

**Layout measurements pass; screenshot and full isolation verification are incomplete.** Fictional fixtures, scratch Desktop/Hermes, the test desktop locked, no agent turns and no copy or reveal clicks. The full non-shallow fork clone was at `6b58832d8e8d87c1e380fbaa81a1299f88128057`, with upstream `v0.23.0` tag; source build completed, CLI and Desktop showed `v0.23.0+127`. The supplied a0ddc24 tarball was used. Scratch bootstrap required a launcher symlink to its venv; no real installation was used.

| Check | Result | Evidence |
|---|---|---|
| A. Version and Clients | PASS | `setup.txt`, `clients-observed.txt`; Desktop showed `v0.23.0+127` and Clients displayed `3 clients, 4 projects, 3 active`. |
| B. Layout | PASS for measured criteria | `layout.jsonl`: both records at all four widths had no descendant beyond page, detail scroller widths equal, record-file span >=60 px. Maple showed `not found here`; Acme URL was under Links and references; side-by-side measured, project rows have `pl-7` vs client `px-2`. Copy aria-labels: Acme `Copy path` x3, `Copy link` x1; Maple `Copy path` x2. Path and link spans including computed text-overflow are in evidence. |
| C. Screenshot | NOT EXERCISED | `screenshot.txt`: default CDP screenshot and one smaller 500x400 clip retry each timed out after 15s; no image captured. |
| D. Console | NOT EXERCISED for whole launch | `console.txt`: after attachment, no Engagements/plugin warning or error observed; four unrelated Electron insecure-CSP development warnings. Initial startup before listener attachment not covered. |

## Measurements (pixels)

List values are list-row widths; detail scroller is scrollWidth/clientWidth. Default is 937px; 800px file-path width is informational.

| Record | View | Page | Detail right | Max descendant right | File span clientWidth | List row | Scroller |
|---|---|---:|---:|---:|---:|---:|---:|
| Acme | default | 937 | 937 | 937 | 166 | 219.94 | 440/440 |
| Acme | 800 | 800 | 800 | 800 | 74 | 175 | 348/348 |
| Acme | 900 | 900 | 900 | 900 | 142 | 207 | 416/416 |
| Acme | 1280 | 1280 | 1280 | 1280 | 389 | 340 | 663/663 |
| Maple | default | 937 | 937 | 937 | 166 | 219.94 | 440/440 |
| Maple | 800 | 800 | 800 | 800 | 74 | 175 | 348/348 |
| Maple | 900 | 900 | 900 | 900 | 142 | 207 | 416/416 |
| Maple | 1280 | 1280 | 1280 | 1280 | 389 | 340 | 663/663 |

## Cleanup and limitation

`cleanup-readback.txt`: scratch removed; test units inactive, ports 18767/18768 unoccupied, no scratch processes; lock `locked:true`, idle `stayAwake:false`. Initial lock/idle had the same values. Real config and state SHA-256 matched the requested baselines; launcher and MIME files matched backups byte-for-byte and their filename list matched. All genuine recorded ChatGPT/Codex PIDs were still present; one missing PID was the short-lived baseline inventory Python itself (identified in readback). The initial `~/.config/Hermes` SHA baseline was accidentally empty because `find` inherited inaccessible `/home/<another-user>` as its working directory. Consequently **before/after profile integrity is NOT EXERCISED**, not a clean comparison; no claim that the real profile was unchanged. Baseline and post-run receipts were retained in private scratch on the build host, not committed.
