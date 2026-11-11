# P3D Desktop isolation receipts

One bounded run on the test desktop, 2026-11-06 22:56 to 23:14 (-0600), as user <user>, against a scratch Hermes `6b58832d8e8d`
(`<build-host>/prod-image-preview-2026-11-06`) with `HERMES_HOME`, `HERMES_DESKTOP_USER_DATA_DIR` and `HERMES_DESKTOP_APP_NAME`
under `$HOME/.cache/<scratch>-eng/`. A loopback stub LLM replied `ok (stub)`; no key, no network model. The screen was
locked the whole time (`<desktop-shell> lock status`: `locked: true`, before and after); Desktop launched and was driven over
CDP (`--remote-debugging-port=9333`, loopback) with no unlock. Fictional library only (Riverbend Dental fixtures); the Recall
Campaign location was set to the scratch user's home so the Desktop default workdir would bind (the SPEC 6.4 control).

The user name, home path, host names and the desktop-shell product name in these receipts were replaced with placeholders
after capture, as was the name of the scratch cache folder (`$HOME/.cache/<scratch>-eng/`). No other recorded value changed; word order moved only where a substitution required it.

| File | What it shows |
|---|---|
| `state-db-shm-and-holders.txt` | Why `state.db-shm` moves (a five-minute read-only collector tick, before, during and after the run) and what could not be captured. |
| `desktop-startup-log.txt` | (c) Desktop's own `desktop.log`: `Using Hermes source at /home/<user>/.cache/<scratch>-eng/src`, gateway ready. Second launch starts at 23:07:41. |
| `banner-api-content.txt` | Fresh receipt: the scratch `state.db` `messages` rows and `api_content`, plus what the stub (the model side) received. |
| `cleanup-readback.txt` | Teardown and readback. |

## Removed before publication

Five raw listings are not in the repository: `real-home-before.txt`, `real-home-after.txt`, `real-home-diff.txt`,
`processes-and-fds-during-turn-2.txt` and `processes-and-fds-after-turns.txt`. They were removed because they listed a
private home folder. What they showed, in summary:

- The real `config.yaml` and `state.db` had the same sha256 before (22:56:28) and after (23:13:03) the run, with the same
  sizes and mtimes. The only differences were the `state.db-shm` mtime (below) and the launcher-entry mtime.
- At two point-in-time probes, 2 s after the second send (23:08:33) and after both turns (23:08:50), a `pstree -p -T` of the
  Desktop unit plus the gateway and per-pid `ls -l /proc/<pid>/fd` counts showed 0 real-home fds in all 11 processes probed
  (main, zygotes, GPU, utility x2, renderer, crashpad, bash wrapper, gateway), the gateway holding 20 scratch-home fds, and
  the `HERMES_HOME` of the processes that carry one pointing at the scratch home. Samples cannot see an open and close
  between probes.

## Results

- **Real-home fds observed: 0, at two point-in-time probes (23:08:33, 23:08:50).** Every Electron process (main, zygotes, GPU, utility, renderer, crashpad), the bash wrapper and the
  Desktop-spawned gateway (`python -m hermes_cli.main --profile default gateway run`, in its own `app-hermes-<pid>.scope`)
  reported `real_home_fds=0`. The gateway held 20 scratch-home fds and carried `HERMES_HOME=/home/<user>/.cache/<scratch>-eng/home`
  in its environment. No <user> process at all, spike or not, had an fd under `/home/<user>/.hermes/` at the probes. Samples cannot
  see an open and close between probes.
- **Real-home content unchanged.** `config.yaml` sha256 `f79772f3973b03ae7d580f5229d38cb856c7bad9f0188afc2ccc7168186e0178`, `state.db` sha256
  `9f26573491fb2dd9ef3a7ae91df0d7fc9bccc82b87a935e13e5fa46189a86311`, before and after; `state.db` mtime 2026-10-14, `state.db-wal` size 0 unchanged.
- **`state.db-shm` moved; the cause is inferred, not captured.** It changed on a cadence consistent with the read-only
  collector, including before launch and after teardown. See `state-db-shm-and-holders.txt`. No pid held the real `state.db` at
  any probe, so the expected holder (the bridge) was not observed holding it either.
- **Banner.** The visible `messages.content` of the user row is the user's text only. `messages.api_content` of that
  row (id 1) holds the text plus the banner (`[Engagements] Client: Riverbend Dental (riverbend-dental), Project: Recall Campaign. Bound automatically from cwd ~...`).
  The stub's request log shows the model received the banner appended to the first user message, and again replayed with the
  history on turn two.

## Things that went wrong or were not exercised

- The first Desktop launch (23:05) ran with the plugin unloaded: the scratch venv lacked PyYAML, so the host logged `Failed to load plugin 'engagements': No module named 'yaml'`
  and no banner was produced. PyYAML was installed into the scratch venv only, the state database was deleted, and the run was
  repeated from 23:07:41; every receipt in the table above except the shm timeline is from that second launch, and the removed
  snapshots cover the whole run.
- `/client` was not run in this session; this run covers isolation and the banner row only.
- **Launcher entry.** The source build rewrote the owner's existing `~/.local/share/applications/com.nousresearch.hermes.desktop`
  at 22:59:13 (Exec pointed at the scratch venv; size 267 to 292 bytes). It was put back to `Exec=/home/<user>/.local/bin/hermes desktop --skip-build`
  (267 bytes again, matching the original size and the Exec recorded in an earlier session) and its mtime restored. `mimeinfo.cache` in the same directory was regenerated by the build (`update-desktop-database`) at 22:59:13; it is a derived index and was not otherwise edited.
- **The legacy `hermes.desktop` is still missing.** It existed on 2026-11-06 at 10:55 with the same Exec, and the previous P3D run's cleanup deleted it.
  Its full content and mtime were not recorded anywhere, so it was not recreated from a guess.
