# Phase 3V — Desktop acceptance on the test desktop (partial)

Desktop source commit `6b58832d8e8d87c1e380fbaa81a1299f88128057`; Engagements archive `61612f7`. `hermes desktop --source --build-only` succeeded with a scratch venv and PyYAML. Launched scratch Electron source build on Wayland behind the lock, loopback CDP 18768 and stub 18767. Enabled desktop half using scratch localStorage `hermes.desktop.pluginDecisions.v2 = {"engagements":true}` and reloaded. No packaged Desktop was used. `setup-build.txt` documents build. First launch's Wayland handoff collided for CDP; its successor eventually exposed the app, escaped its transient unit and was stopped by exact PID. An attempted second launch exited because the scratch instance held its single-instance lock.

Critical qualification: the fresh fork clone identified the runtime as `0.15.1+52930`, and gateway log says `Plugin 'engagements' skipped: requires hermes >=0.22.3, running 0.15.1`. Consequently the desktop read-only half loaded, but agent commands and tools did not. Fork clone's nearest tags differ from prior run; root cause is not fully proven. This run does not establish an Engagements command defect. See `plugin-diagnostics.txt`, `update.txt`, `chat.txt`.

| # | Result | Evidence and limitation |
|---|---|---|
| 1 | PASS | Sidebar and command palette opened Clients; counts and healthy status: `acme-layout.txt`, `palette.txt`. |
| 2 | FAIL | Grid measurements show side-by-side 352px/325px, indentation 8px vs 28px, labels and values two columns. But detail grid extends to x=1206 while page ends x=937: actual content is clipped, as `acme.png` shows. `acme-layout.txt`. |
| 3 | PARTIAL | Id, client, file, two local folder rows and URL under links, and non-warning 2 folders badge observed in `acme-layout.txt`; found folders use status dots, not explicit found badges. Paths visually clipped. |
| 4 | PASS | Maple missing folder shows not found here: `maple.txt`. |
| 5 | PARTIAL | Missing-folder reveal generated copied-path notice: `reveal-privacy.txt`. Independent clipboard read could not establish contents (comparison returned nonzero); clipboard restoration was not performed. Found-folder reveal not attempted. |
| 6 | PARTIAL | Names masked, ids, paths and record text hidden; switch remained on through Page.reload and was turned off: `reveal-privacy.txt`. Bound chip masking could not be tested. |
| 7 | PARTIAL | New chat opened with `/client acme-bakery` unsent, no user message and no stub log yet. Enter produced `/client · error: unsupported_command` because agent plugin was skipped. A later ordinary chat returned `echo: hello`. `chat.txt`. |
| 8 | NOT EXERCISED | No client chip observed, but binding after slash command unavailable due plugin version gate: `chat.txt`, `plugin-diagnostics.txt`. |
| 9 | NOT EXERCISED | Context tool call returned unknown tool because plugin was skipped; no update attempted. `update.txt`, `plugin-diagnostics.txt`. |
| 10 | PARTIAL | Runtime/Log enabled after main exercise; eight-second reload collection found no Engagements/plugin warning or error events; not continuous monitoring: `console-reload.jsonl`. General Electron unsafe-eval CSP development warning and stale-index warnings present. Backend skip warning separately recorded in `plugin-diagnostics.txt`. |
| 11 | PASS (file-list scope) | Scratch library and plugin-data file lists compared before/after read-only browsing, no agent turns intervened; no names added: `readonly.txt`, `inventories-stub.txt`. This is not a byte-content comparison. |

Likely clipping cause: `desktop/plugin.js` lines 288 and 404, intrinsic width of details grid / outer grid's `1fr` and missing minimum-width constraints. This is a hypothesis; no fix or causal mutation test was performed.

Cleanup: `cleanup-final.txt` shows both units inactive, no scratch process or 18767/18768 listener, scratch tree removed, launcher and mimeinfo caches byte-identical to backups, real config and state hashes unchanged, lock true and stayAwake false. All 20 captured ChatGPT/Codex PIDs still existed under the same process names. The clipboard was not restored. The first Electron bootstrap briefly started child processes with a default `--user-data-dir=/home/<user>/.config/Hermes` before the actual app's scratch user-data override; that directory pre-existed and its directory stat predates this run, but its contents were not baselined, so unchanged contents cannot be claimed. Real Hermes config and DB hashes are unchanged; no claim of complete real-home non-access is made.
