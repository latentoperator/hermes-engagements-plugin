# P3V dashboard receipts (2026-11-08)

Phase 3V acceptance of the Clients dashboard (SPEC 6.7). Four bounded runs on the test desktop, 10:42 to 11:47 (-0600), as user
<user>, against a scratch Hermes source build of `6b58832d8e8d` with `HERMES_HOME`, `HERMES_DESKTOP_USER_DATA_DIR`,
`XDG_DATA_HOME` and (runs 2 to 4) Chromium's `--user-data-dir` under `$HOME/.cache/<scratch>-eng/`. Run 1 set only the
environment override: its first Electron bootstrap briefly started child processes with the default
`--user-data-dir=~/.config/Hermes` before the app applied the scratch directory (`run1-REPORT.md`, cleanup paragraph). A loopback stub LLM
(`spike/stub_llm.py`) answered; no key, no network model. The test desktop was locked throughout (`<desktop-shell> lock status`:
`locked: true` before and after every run) and Desktop was driven over loopback CDP with no unlock. Fictional studio
library only (`tests/fixtures/studio`, `~/clients/` rewritten into the scratch tree; Maple's folder missing on purpose).

Each run's `REPORT.md` was written by the run and names its raw receipts. Those raw files (full CDP event logs, process
listings and host diagnostics from the owner's machine) are kept off the repository; the files here are the ones the
results rest on. The user name, home path, host names and the desktop-shell product name in these receipts were replaced
with placeholders after capture, as was the name of the scratch cache folder (`$HOME/.cache/<scratch>-eng/`). No other recorded value changed; word order moved only where a substitution required it.

| File | What it shows |
|---|---|
| `run1-REPORT.md` | Engagements `61612f7`. Read-only Desktop half loaded; the agent half was skipped because the fork clone lacked upstream tags and reported Hermes `0.15.1` (gate `>=0.22.3`). Found the record pane clipped. |
| `run2-REPORT.md` | `415479f`, clone with the upstream `v0.23.0` tag (`v0.23.0+127`). Agent half loaded; start a chat, chip, privacy pass; pane still clipped (Radix ScrollArea). |
| `run2-start-chat.txt` | New chat with `/client acme-bakery` in the composer, unsent ("Press Enter to send"), then the bound result. |
| `run2-privacy.txt` | Privacy on: initials, ids/paths/record text hidden, chip masked; still on after `Page.reload`; turned off. |
| `run3-REPORT.md` | `34387f1`. Mounted-view refresh and binding event pass; one 23px overflow at 900px. |
| `run3-refresh-poll.jsonl` | 500 ms DOM polls of the open Acme record: marker absent while the delayed stub ran, then present, no click. |
| `run3-change-frames.json` | The two `plugin.engagements.changed` gateway frames the renderer received: `{"what": "library"}` after `engagements_update`, `{"what": "binding"}` after `/client riverbend-dental`. |
| `run3-binding.jsonl` | Chip text before and after `/client riverbend-dental`. |
| `run4-REPORT.md` | `a0ddc24`. Layout measurements for both records at default (937), 800, 900 and 1280 px: nothing past the page edge, detail scroller `scrollWidth == clientWidth`, record-file path visible. |

## Isolation, all runs

Real `~/.hermes/config.yaml` and `state.db` sha256 matched the baseline after every run (`f79772f3...6e0178`,
`9f265734...a86311`). The launcher entry `com.nousresearch.hermes.desktop` and `mimeinfo.cache` were backed up first and
were byte-identical (`cmp`) after every run. The owner's other Electron app kept its PIDs.

`~/.config/Hermes` (the owner's Desktop user data): run 1 did not baseline it, so its contents after run 1 cannot be
claimed unchanged. After run 1, the build owner's own check (not a run receipt) found `DevToolsActivePort` there naming the scratch
CDP port; the directory's mtime had not changed, so the file pre-existed and was overwritten. Runs 2 and 3 compared
`find` metadata for the whole directory and sha256 of its regular files up to 50 MB (run 2: `<=50 MB`, 833 hashes;
run 3: `<50 MB`) before and after: no difference. Files above that size were compared by metadata only. Run 4's baseline inventory came out empty, so its own
comparison proves nothing. A `find -newermt 10:55` check after run 4 found no file modified after 10:55, but modification
time is not a content hash: run 4's profile integrity is **not established**, and the check cannot see a file rewritten
with its mtime preserved.

Run 1 clicked one copy action on a missing folder. The app showed its copied-path notice; an independent clipboard
read could not establish what the clipboard then held (the comparison returned nonzero), and the clipboard was not
restored. Runs 2 to 4 clicked no copy or reveal control.
