# Runtime evidence

Runtime claims about Hermes behavior live here, each with the surface it was observed on.
Source inspection is not a runtime result; where a row cites source, it says so.

**Banner format:** the banner text quoted in this file was recorded before record names, project names and folder paths were rendered as quoted data. That quoting is a later change: it is covered by unit tests, not by a runtime run, so the quoted format has not been runtime-exercised.

**`spike/`:** the folder is kept in the repository as the historical harness for the runs recorded here. It is not part of the plugin and is not loaded by it.

## Phase 0: runtime spike (2026-11-04)

**Build under test:** Hermes Agent v0.22.5+4316.g7a25fa9 (git install, Linux).
**Harness:** isolated `HOME`/`HERMES_HOME` with a scrubbed environment (`spike/hx`), a deterministic
OpenAI-compatible stub model (`spike/stub_llm.py`, no network, no credentials), and a throwaway probe
plugin (`spike/engprobe/`) that appends one JSON line per seam event. Sanitized log:
`spike/evidence/phase0-probe.jsonl` (147 events). Profiles: `default`, plus `studio-dev` and
`studio-ops` cloned from it. Fictional clients only.

**Surfaces exercised:** classic CLI one-shot (`hermes chat -q`, with `--resume`), classic CLI
interactive (`hermes --cli` in tmux), TUI (`hermes --tui` in tmux).
**Not exercised:** Desktop, messaging gateway platforms, `plugins.isolation: host`. Those surfaces carry no
support claim until they are run.

### Headline finding: one local gateway process serves every local session

Every CLI and TUI turn, on every profile, ran inside a single per-`HERMES_HOME` process
(`hermes_cli.main --profile default gateway run --quiet --idle-exit`, auto-spawned). Its own
`os.getcwd()` is the Hermes install directory. Observed: one pid served CLI one-shot, CLI interactive,
TUI, and the `default`, `studio-dev` and `studio-ops` profiles, including concurrently.

Consequence for the build: nothing in the plugin may rely on process-global state for the session,
profile, cwd or library. Use handler kwargs (`session_id`), `ctx.session_cwd()`, `ctx.session_surface()`,
`ctx.get_config()` and `ctx.state` / `plugin_data_dir()`, which all resolved per session and per
profile inside the shared process (rows S1, S5, S6). Module-level caches must be keyed by library root.

### Seam results

| Seam | Result | Surfaces | Evidence |
|---|---|---|---|
| S1 tool handler | **Pass.** Handler kwargs carry `session_id` and `task_id` (equal values). `ctx.session_cwd()` is the session's launch directory, not the gateway's cwd. `ctx.session_surface()` returns `cli` / `tui`. Holds when the call arrives through the Tool Search bridge (`tool_call`). | CLI one-shot, CLI interactive, TUI | `S1.tool.start` events; labels `cli-oneshot-2`, `cli-interactive-2`, `tui-1` |
| S2 `pre_llm_call` | **Pass with amendment.** Fires every turn with the same `session_id` as S1; returned context reaches the model appended to the user message and reflects a binding made on the previous turn. **Not ephemeral:** the injected text is stored in the user row's `api_content` sidecar in `state.db` and replayed verbatim on every later request (prompt-cache stability). It is absent from the visible `content` column and from FTS (`messages_fts`, `messages_fts_trigram` match 0). | CLI one-shot, CLI interactive, TUI | `S2.pre_llm_call` events; stub request bodies for turn 3 show turn-1 and turn-2 banners replayed; `state.db` query |
| S3 slash command | **Pass.** `/engprobe` handler receives `session_id` and `session_key` matching S1/S2, `platform='local'`, correct per-session cwd. A binding set by the command appears in the next turn's banner. In the TUI the reply renders in a panel, not inline. | CLI interactive, TUI | `S3.slash` events |
| S4 identity | **Pass with amendments.** (a) Session id does **not** rotate on `/compress` or on auto-compaction for local sessions; `post_compaction` fires with `kind='summarize'`, `old_session_id=''`, same `session_id`. Bindings keyed by session id survive with no migration. The rotating path exists in source (`agent/conversation_compression.py`, `agent.session_id = new_session_id`) but was not reached on these surfaces. (b) `/compress` runs `post_compaction` outside the session context (`session_cwd` falls back to the profile terminal cwd); use kwargs only. (c) `/new` gives a new id and fires `on_session_reset` with `old_session_id` set and `new_session_id` empty. (d) `/branch` creates a child session (`parent_session_id` recorded in `state.db`), but the `parent_session_id` kwarg to `pre_llm_call` on the branch's first turn is **empty**, so the parent is not observable through public hook data. (e) `--resume` reuses the id and its stored binding. | CLI one-shot (auto-compaction, resume), CLI interactive (`/compress`, `/branch`, `/new`) | `S4.*` events; `sessions` table |
| S5 concurrency | **Pass.** Two concurrent sessions in one profile, served by the same pid on different threads, each with a tool call sleeping 6 s so the calls overlap: each kept its own `session_id`, session env and `session_cwd` before and after the sleep. Concurrent binds from both sessions both persisted. | CLI one-shot x2 concurrent | `spike/s5_concurrency.sh`; `S1.tool.start/end` for `conc-A`, `conc-B` |
| S6 storage | **Pass.** `ctx.state.data_dir` and `plugin_data_dir()` follow the profile inside the shared process (`profiles/<name>/plugin-data/...`). `ctx.get_config()` is profile-scoped in the shared process (changing `studio-ops`'s `library_root` affected only `studio-ops`). Two profiles on one `library_root` see each other's writes. `fcntl.flock` contends between two profiles' sessions **inside one process** (waited 2.53 s) and between the gateway and an external process (waited 2.12 s). | CLI one-shot | `spike/s6_profiles.sh`, `spike/s6_crossproc.sh`; `S6.lock` events |
| S7 skills | **Pass; docs are wrong, source is right.** A `register_skill` skill appears in `<available_skills>` as `engprobe:probe-skill` (the plugin guide says plugin skills are not listed; the `register_skill` docstring says they are). It was listed **without a description**. `skill_view("engprobe:probe-skill")` returns the body with a bundle-context banner. | CLI one-shot | stub request system prompt; `skill_view` result |
| S8 restart | **Pass.** After the gateway process was stopped and a new one started (new pid), `--resume` kept the session id and the stored explicit binding. Record rediscovery is a file scan and is proven in the Phase 3 acceptance run. | CLI one-shot | `S2.pre_llm_call` after restart, `bound='acme-bakery'` |
| S9 validate | **Pass.** `hermes plugins doctor . --ci` exit 0; `hermes plugins validate .` passed, including `no core override` and no private-import warning for `gateway.session_context`, `agent.runtime_cwd`, `plugins.plugin_storage`. Note: under `plugins.isolation: host`, `register_cli_command` is skipped. | n/a | command output |

### Additional finding: plugin tools are deferred behind Tool Search

With stock defaults (`tools.tool_search.enabled: auto`), every non-core plugin tool is removed from the
model-visible tool array and reachable only through `tool_search` / `tool_describe` / `tool_call`. A
direct call to `engprobe_where` returned "does not exist"; the same call through `tool_call` worked and
carried full session identity (S1). Setting `tools.tool_search.enabled: off` makes everything eager. Source:
`tools/tool_search.py::is_deferrable_tool_name`; docs: `website/docs/user-guide/features/tool-search.md`.

### Support matrix after Phase 0

| Surface | Tools | Banner | `/client` | cwd auto-bind |
|---|---|---|---|---|
| Classic CLI (one-shot, interactive) | yes | yes | interactive only | yes (launch directory) |
| TUI | yes | yes | yes | yes (launch directory) |
| Desktop | pass (Phase 3D) | pass (Phase 3D) | pass, surface `local` in slash commands (Phase 3D) | cwd binding enabled (`desktop`); `/client` uses the session's turn-verified surface |
| Messaging gateway | unverified | unverified | unverified | `cwd_unsupported` |

### Reproduce

```bash
python3 -I spike/stub_llm.py 18555 &          # STUB_LOG=<path> to capture request bodies
spike/hx config path                         # creates the isolated home under .scratch/spike/
# write config.yaml: model.provider custom, base_url http://127.0.0.1:18555/v1, plugins.entries.engprobe
cp -r spike/engprobe .scratch/spike/home/.hermes/plugins/ && spike/hx plugins enable engprobe
spike/hx chat -q 'CALL engprobe_where {"label":"x"}' -Q
bash spike/s5_concurrency.sh; bash spike/s6_profiles.sh; bash spike/s6_crossproc.sh
python3 spike/show.py .scratch/spike/evidence/probe.jsonl
```

## Phase 2: plugin wiring (2026-11-06)

**Build under test:** Hermes Agent v0.23.0+127.g6b58832 (git install, Linux), plugin at branch
`p2/plugin-wiring`. **Harness:** the Phase 0 harness again (`spike/hx`, `spike/stub_llm.py`, scrubbed
environment, no network, no credentials) with a fresh isolated home at `.scratch/p2/home`, the real
plugin copied to `home/.hermes/plugins/engagements`, and a copy of the fictional `tests/fixtures/studio`
library as `library_root`. The real `~/.hermes` home and every live profile were untouched; the scratch
gateway, plugin host and stub were stopped afterwards.

**Surfaces exercised:** classic CLI one-shot (`hermes chat -q`, with `--resume`) and the `hermes
engagements` subcommands, in-process; then CLI one-shot again under `plugins.isolation: host`.
**Not exercised in Phase 2:** the TUI, CLI interactive, Desktop, messaging gateway platforms, and the
`post_compaction`, `on_session_reset` and `on_session_delete` hooks at runtime. Those hooks are covered by
unit tests against a fake `ctx` built from the Phase 0 findings (S4), not by a runtime result, and carry no
runtime claim here.

| Check | Result | Surface | Evidence |
|---|---|---|---|
| Tool call through the Tool Search bridge | **Pass.** With `tools.tool_search` at its default, no `engagements_*` tool is in the model's tool array; the stub's `CALL engagements_context {"kind":"project"}` went through `tool_call` and returned the correct envelope (`ok: true`, record `riverbend-recall-campaign`, `valid: true`). | CLI one-shot | stub request log: request 0 decision `tool_call`, request 1 carries the tool result |
| Auto-bind on cwd | **Pass.** Launched from `~/clients/riverbend/recall`, the session bound to `riverbend-recall-campaign` with no tool call or command. | CLI one-shot | banner text below |
| Banner reaches the next request | **Pass.** The request after the binding carries `[Engagements] Client: Riverbend Dental (riverbend-dental), Project: Recall Campaign. Bound automatically from cwd ~/clients/riverbend/recall. Tools: engagements_context, engagements_update (via tool_call if not listed). ...` appended to the user message (under 400 characters). | CLI one-shot | stub request bodies |
| Banner is on change only | **Pass.** After `/client acme-bakery`, the next turn carried one banner; on the turn after that the request held the banner once, in the earlier user message replayed with the history, and none in the new message (banner count per user message: 1, 0). | CLI one-shot with `--resume` | request 2 of the second stub log |
| `/client` | **Pass.** `/client list` printed the active clients and projects. `/client acme-bakery` returned "Binding updated." with mode, binding and `Evidence: explicit 18:11`. `/client` in the resumed session showed the same binding (state lives in plugin state, keyed by session id). `/engagements doctor` printed the doctor report. | CLI one-shot | command output |
| `hermes engagements` | **Pass in-process.** `list`, `show acme-bakery` and `doctor` printed their reports with exit 0; `init --root <path>` created the library and saved `plugins.entries.engagements.settings.library_root` through `ctx.set_config`. | CLI | command output; the `config.yaml` setting after `init` |
| `hermes plugins doctor . --ci` | **Pass, exit 0.** `OK: runtime discovery, manifest parsing, import, and registration passed`, `registrations: 5 tool(s), 4 hook(s)`. | n/a | `.scratch/p2/evidence/doctor.txt` |
| `hermes plugins validate .` | **Pass, exit 0.** 15 checks passed, including `declared tools`, `declared hooks`, `built-in tool collisions`, `security scan` and `no core override`; no private-import warning. | n/a | `.scratch/p2/evidence/validate.txt` |
| `plugins.isolation: host`, S1-S3 on the real plugin | **Works, except the CLI subcommand.** Tool via the bridge returned the correct envelope; `ctx.session_cwd()` and `ctx.session_surface()` resolved to the launch directory and `cli` inside the plugin host process (its own cwd is `HERMES_HOME`), so cwd auto-bind and the banner worked; `/client`, `/client <name>` and `/engagements doctor` worked, and the next turn's banner followed the command. `hermes engagements ...` is not available: `hermes: 'engagements' is not a hermes command.` (`register_cli_command` is skipped in the host, as Phase 0 S9 noted.) | CLI one-shot | stub request log; command output |

### Re-run: banner-off revalidation and CLI exit codes (same build and harness, code head `8b3d6d9`)

Re-ran the isolated check from a pristine `git archive` export of the fix head, then again from a fresh
clone of the pushed branch; the results matched. The table above still holds (bridge tool call,
auto-bind, banner on change, `/client`, CLI healthy paths, doctor exit 0 with 5 tools/4 hooks, validate
exit 0 with 15 checks). Two facts changed and were observed directly:

| Check | Result | Surface | Evidence |
|---|---|---|---|
| `banner: false` retains and revalidates the auto binding | **Pass.** With `banner: false`, one turn from `~/clients/riverbend/recall` stored `{"mode":"auto","client":"riverbend-dental","project":"riverbend-recall-campaign"}` and sent no banner. After reparenting that project to Acme in the scratch library, the resumed session returned `ok: false`, `code: unbound`, `details.state invalid_binding`, `reason client_changed` (never Acme data), and `/client` printed `Not bound: the stored binding is no longer valid.` / `Evidence: invalid: client_changed`. | CLI one-shot with `--resume` | `evidence/banner-off-*`, plugin `state.json` |
| CLI failed open exits nonzero | **Pass.** With a configured root that does not exist, `hermes engagements list` printed `No library exists yet at ...` and exited **1**; `show` and `doctor` also exited 1. The healthy `list`, `show` and `doctor` paths still exit 0. | CLI | `evidence/cli-failure-*.txt`, `evidence/inprocess-cli-*.txt` |

Banner text in the passing run was 302 characters at most. Scratch gateway, plugin-host and stub
processes were stopped after each mode (`remaining: []`); the real `~/.hermes` home and live profiles
were not used.

### Findings that changed the build

1. **`hermes plugins doctor` cannot run on a repo that holds its own scratch home.** The doctor stages a
   copy of the plugin under `TMPDIR` and refuses ("so the copy would recurse into itself") when `TMPDIR`
   resolves inside the plugin. `HERMES_HOME/cache/scratch` is under `.scratch/` here, so the first run
   failed. `spike/hx` now passes `P2_TMP` through as `TMPDIR`; set it to a directory outside the repo.
2. **`hermes plugins validate .` does not finish inside a repo that holds a 1.6 GB scratch home.** It
   walks every file under the plugin directory (92,000 files here) and timed out at 170 s with no output.
   Run it on a clean export (`git archive HEAD`) or on a repo without `.scratch/`; on the export it takes
   about 3 s and passes.
3. **The local gateway outlives one command.** A scratch gateway started by the first `hermes chat` stayed up
   (`--idle-exit`) and a debug line added to the plugin copy never ran until that gateway was stopped, so it
   appears to keep the loaded module. Stop it before re-testing a changed plugin.
4. **State keys have a stricter alphabet than the spec assumed.** Hermes limits plugin state keys to 128
   characters of `[A-Za-z0-9_.:-]`. `binding:<library_id>:<session_id>` fits for ordinary session ids; the
   plugin maps any other key to a stable digest instead of failing (see `SPEC-QUESTION` in
   `engagements/runtime.py`).

### Reproduce (Phase 2)

```bash
export SPIKE=$PWD/.scratch/p2 P2_TMP=<a directory outside the repo>
mkdir -p $SPIKE/home/.hermes/plugins/engagements $SPIKE/home/clients/riverbend/recall
cp -r __init__.py plugin.yaml engagements $SPIKE/home/.hermes/plugins/engagements/
cp -r tests/fixtures/studio $SPIKE/library
# config.yaml as in Phase 0 (provider custom, stub base_url), plus:
#   plugins: {enabled: [engagements], entries: {engagements: {settings: {library_root: $SPIKE/library}}}}
STUB_LOG=$SPIKE/evidence/stub.jsonl python3 -I spike/stub_llm.py 18556   # separate shell
cd $SPIKE/home/clients/riverbend/recall
SPIKE=$SPIKE spike/hx chat -q 'CALL engagements_context {"kind":"project"}' -Q
SPIKE=$SPIKE spike/hx chat -q '/client list' -Q
SPIKE=$SPIKE spike/hx engagements list
# git archive HEAD into a clean directory, then:
SPIKE=$SPIKE spike/hx plugins doctor . --ci; SPIKE=$SPIKE spike/hx plugins validate .
# for the host run add `plugins.isolation: host` to the scratch config.yaml
```

## Phase 3 acceptance (2026-11-06)

**Build under test:** Hermes Agent v0.23.0+127.g6b58832 (git install, Linux), plugin at branch
`p3/skills-acceptance`. **Model:** a real one, `deepseek-v4-pro` from DeepSeek. **Harness:** a fresh
isolated home under `.scratch/p3` with a scrubbed environment (`env -i`; a copy of the `spike/hx` wrapper that
passes only `HOME`, `HERMES_HOME`, `PATH`, `TMPDIR`, plus an optional `DEEPSEEK_API_KEY` pass-through when
the operator supplies one — see Credential handling), the real plugin copied into each profile's `plugins/`
and enabled, fictional data only (Acme Bakery, Riverbend Dental, Harbor Print Shop, `.example`). The real
`~/.hermes` home and every live profile were untouched; the live gateway was not restarted.

**Surfaces exercised:** classic CLI one-shot (`hermes chat -q/--query-file -Q`, with `--resume`), classic CLI
interactive (`hermes --cli` in tmux, a pseudo-terminal), and the TUI (`hermes --tui` in tmux). Desktop and
messaging gateway platforms: **not exercised**, no support claim. Desktop is covered in the Phase 3D sections at the end of this file; this run says nothing about Desktop behaviour.

**Not claimed:** `/client` cwd auto-resolution from an interactive CLI or TUI slash command. Inside such a
handler the surface is `local`, which SPEC 6.4 does not admit, so the status is read-only there and the
per-turn hook maintains the binding (see "Surface gate for /client on the CLI and the TUI").

**Credential handling.** The DeepSeek key came from the `DEEPSEEK_API_KEY` environment variable only. The
default profile read it directly from its process environment. Hermes resolves a secondary profile's
credentials from that profile's own `.env` and does not read the launcher's environment (config
`${env:VAR}` refs resolve inside the same profile scope), so `studio-dev` and `studio-ops` could not take it
by reference. They used a custom provider pointed at a loopback forwarder (`dsproxy.py`, stdlib, an
unlogged process that adds the key from its own environment). No key value was written to any config, `.env`,
log or commit: `grep -rlF "$DEEPSEEK_API_KEY"` over the scratch home, the worktree and the export listed no
files (exit 1).

**Interaction.** Each step was driven the way a user would type it, with no tool names in the prompts. The
model chose the skill (`skill_view("engagements:onboard-client")`) and reached the tools through
`tool_search` / `tool_describe` / `tool_call`. "Edit in a text editor" was a direct file write.

### Results by step

| Step | Classic CLI | TUI | Evidence |
|---|---|---|---|
| 0 single default profile, zero config: onboard, add a project, bind, update, restart | **Pass** (one-shot; read-only `/client` in an interactive session after the `/client` surface fix) | not exercised | Sessions `local-ae834339...ce9` (draft shown with an Unconfirmed item, correction applied, saved; project added), `local-30d07b4c...6d7` (auto-bound from the project folder, then a targeted `replace_line` update). First `engagements_find` returned `warnings: library_created` with no CLI setup. After stopping the gateway, a new session found both records and `doctor` was clean (2 records). Record file: frontmatter, sections, Change log. |
| 1 `studio-dev` onboards Acme Bakery from pasted notes; draft has an Unconfirmed item; correct one fact | **Pass** (one-shot) | not exercised | `local-2587f006...fab`: draft held the registrar under Unconfirmed; the user corrected "Mondays" to "Tuesdays"; saved as `acme-bakery` (rev `105e9fc77dda7f48`). Saved file has "Tuesdays". |
| 2 `studio-ops` onboards Riverbend Dental conversationally | **Pass** (one-shot) | not exercised | `local-cb048c32...771`: the model asked for the essentials, then saved `riverbend-dental` into the shared library root. Both profiles wrote to one `library_root` (`hermes engagements init --root`: created, then joined; same library id). |
| 3 projects: `acme-website-2026`, `riverbend-recall-campaign`, `riverbend-brand-assets` (the brand project shares a folder with Acme's) | **Pass** (one-shot) | not exercised | `local-47d2e847...e2d`, `local-525734c3...f184`. Acme lists two absolute folders (its own and `shared-assets`); Riverbend Brand Assets lists `shared-assets`. |
| 4 auto-bind: Acme folder binds Acme with contacts; Riverbend folder binds Riverbend; shared folder `ambiguous`, nothing bound; parent folder unbound; two concurrent sessions in one profile | **Pass** (one-shot, `studio-ops`) | **Pass** (`studio-ops`; a model turn and the banner per folder; `/client` status is read-only after the `/client` surface fix) | CLI: `local-edb122b0...c33` (Acme + both contacts), `local-4196a81c...a8fda16` (Riverbend + both contacts), `local-0568ea39...9d` (`engagements_context` -> `unbound`, `details.state ambiguous`, candidates `acme-website-2026`, `riverbend-brand-assets`), `local-dd0facbf...966` (`unbound`, `reason no_match`). The Acme and Riverbend sessions overlapped (messages 19:37:36.97 to :46.17 and :36.97 to :44.99, one profile, one gateway). TUI: each folder was bound by the per-turn hook (the banner and the model's answers named the right client and contacts); `/client` then reported the stored binding read-only (`Last binding from a CLI/TUI session: ...`, `Evidence: auto: cwd is not used on surface 'local'`), and in the shared and parent folders reported `Not bound: automatic binding from the folder is not available on this surface.` The ambiguity itself came from `engagements_context` (`details.state ambiguous`, candidates `acme-website-2026`, `riverbend-brand-assets`), not from the slash command. The Acme and Riverbend TUIs ran at the same time in one profile and each answered with its own contacts. |
| 4 banner | **Pass** (one-shot for auto-bind; interactive CLI for the explicit-bind banner, step 7) | **Pass** | Stored `api_content` banners: `[Engagements] Client: Acme Bakery (acme-bakery), Project: Acme Website 2026. Bound automatically from cwd ~/studio/acme/website-2026. Tools: engagements_context, engagements_update (via tool_call if not listed). Earlier turns may contain other clients' context.` (and the Riverbend one); for the shared folder `[Engagements] Folder binding is ambiguous: cwd ~/studio/shared-assets is inside acme-website-2026, riverbend-brand-assets. Nothing is bound. Ask the user which one, then call engagements_bind.` None for the unbound parent folder. |
| 5 `studio-dev` updates a contact line; `studio-ops` reads the new rev | **Pass** (one-shot) | not exercised | `local-1f474dc2...2fb` (`replace_line`, `new_rev d88230afc8569d12`), then `local-d49b7477...968` read the same line and rev `d88230afc8569d12`. |
| 6 editor edit then stale-rev update: Contacts conflict; Communication preferences rebases; stale `set_property status` conflicts; nothing written on conflict; Change log intact | **Pass** (one-shot, one session) | not exercised | `local-b057e9fe...b97b`. (a) After a direct file edit of Jane's line: `ok: false, error.code conflict, reason footprint_changed`, `footprints[0]` with `base_text` and `current_text`; the file was byte-identical to the editor's version afterwards. (b) Stale append to Communication preferences: `ok: true, rebased: true, new_rev d2eac40579b51fc0`; the editor's Contacts change survived. (c) After an editor set `status: paused`: `conflict` on `status` with `base_text "status: active"` and `current_text "status: paused"`; file unchanged. The Change log kept every earlier line. This session did not list `.engagements/history/<id>/`, so no runtime claim is made about that directory here; history retention is covered by the library tests, not by this run. |
| 7 explicit bind, forced compaction, restart | **Pass** (interactive CLI, `studio-ops`) | **Pass** (`studio-ops`) | CLI `local-a7457ffd...d8d`: `/client riverbend-dental` -> `Binding updated.` with `Mode: explicit` (read-only: `Bound (stored). Client: Riverbend Dental ...` after the `/client` surface fix); `/compress` -> `Compressed: 16 -> 12 messages`; the session id did not change; `/client` still `explicit`; a tool call with no id returned Riverbend; the banner was emitted again on the first turn after compaction. Gateway stopped, `--resume` in a new process: `/client` unchanged, `/client list` printed both clients and all three projects, `engagements_find` returned the ids, `/engagements doctor` printed `Doctor: clean (2 notices)`. TUI `local-91d68f00...152`: `/client acme-bakery`, `Compressed: 12 -> 10 messages`, binding survived and the banner returned on the next turn, same results after stop and `--resume`. The two notices are the intended `shared_location` hotspots, no errors or warnings. |
| 8 plain-text editor | **Pass** | not applicable | Records are plain Markdown with flat YAML frontmatter, read and edited with ordinary file writes in step 6, and the plugin treated the edits correctly (rev changed, footprint conflict). The optional Obsidian check was skipped. |

### Surface gate for /client on the CLI and the TUI

The first attempt to make `/client` read the working folder on the CLI and the TUI treated the command's
`local` surface as `cli`. An independent review rejected that: Hermes shares the local-source path with Desktop, which
is unverified (spec 11), so a Desktop command could have resolved cwd and stored an automatic binding. The
mapping is removed. `/client` resolves the working directory only when the reported surface is `cli` or
`tui`; on every other surface it reports the stored state read-only, writes nothing, and says so. Explicit
`/client <name-or-id>`, `/client off` and `/client auto` are unchanged, and the per-turn hook path is
untouched, so a CLI/TUI session still maintains its binding every turn.

Re-run on the real surfaces with the same isolated harness and the deterministic stub model (no real model,
no key, no network), at the fix head (after `1afffa6`):

| Check | Result | Surface | Evidence |
|---|---|---|---|
| `/client` before any turn, launched from `~/clients/riverbend/recall` | **Read-only, nothing written.** `Mode: auto` / `Not bound: automatic binding from the folder is not available on this surface.` / `Evidence: auto: cwd is not used on surface 'local'`. The plugin state store held 8 keys before the command and 8 after it: no binding or banner key was created for that session. | classic CLI interactive | transcript below; `state.json` key count |
| surface value reported to the command | **`local`**, quoted verbatim in the evidence line (`cwd is not used on surface 'local'`). The tool and hook paths still report `cli` / `tui` (Phase 0 S1). | classic CLI interactive, TUI | transcripts below |
| `/client` after one turn in the same folder | **Reports the binding the hook stored, labelled read-only.** `Last binding from a CLI/TUI session: client riverbend-dental, project riverbend-recall-campaign.` with the same "not available" line; no `Bound.` claim. | classic CLI interactive, TUI | transcripts below |
| `/client acme-bakery`, then `/client auto` | **Explicit modes unchanged.** `Binding updated.` / `Mode: explicit` / `Bound (stored). Client: Acme Bakery (acme-bakery)` / `Evidence: explicit 20:27`; `auto` returns `Mode: auto` with the read-only note and no identity. | classic CLI interactive | transcript below |

Transcript excerpts (classic CLI interactive, surface `local`; the TUI shows the same lines in a panel):

```text
⚙️   /client
Mode: auto
Not bound: automatic binding from the folder is not available on this surface.
Evidence: auto: cwd is not used on surface 'local'
Use /client <name-or-id> to bind, /client list to see what exists.

⚙️   /client          # after one turn, the hook has stored the automatic identity
Mode: auto
Not bound: automatic binding from the folder is not available on this surface.
Last binding from a CLI/TUI session: client riverbend-dental, project riverbend-recall-campaign.
Evidence: auto: cwd is not used on surface 'local'

⚙️   /client acme-bakery
Binding updated.
Mode: explicit
Bound (stored). Client: Acme Bakery (acme-bakery)
Evidence: explicit 20:27
Automatic binding from the folder is not available on this surface.
```

**Limitation recorded.** A `/client` status on the interactive CLI or TUI cannot resolve the working folder
from the command path, because that command's surface value is `local` and `local` is not a verified CLI/TUI
discriminator. The per-turn banner and hook still bind normally, and the status reports that stored binding.
Desktop remains unverified and is **pending the Desktop spike (Phase 3D)**, which runs the real Desktop app before
any cwd binding or `/client` status claim is made there.

### Checks outside a session

`hermes plugins doctor . --ci` exit 0 (`registrations: 5 tool(s), 4 hook(s)`) and `hermes plugins validate .`
(15 checks passed, `Validation passed.`) were run in the scratch home on a clean `git archive` export, for the
reasons in Phase 2 findings 1 and 2.

### Defects the run found and their disposition

1. **`/client` cannot see the working directory from a slash command (recorded limitation).** Inside a slash
   handler `ctx.session_surface()` answered `local` on the interactive CLI and the TUI (tools and hooks see
   `cli` / `tui`), so an earlier change on this branch read `local` as `cli` for the command path. An independent review
   found that Hermes also uses the local-source path for Desktop, so that mapping could resolve cwd and store
   an automatic binding on an unverified surface. The mapping is removed: `local` stays unsupported for cwd
   resolution, `/client` status is read-only on it, and explicit modes still work. The per-turn hook still
   binds on CLI and TUI, so the status reports the stored binding
   (`Last binding from a CLI/TUI session: ...`). This is recorded as a limitation, not a pass. Desktop is
   pending the Desktop spike; no Desktop behaviour is claimed here.
2. **`/client` in a CLI one-shot cannot see the launch directory.** `hermes chat -q '/client'` reported
   `no project location contains cwd ~` (the profile terminal cwd, not the launch folder). This matches the
   Phase 0 matrix (`/client` is interactive only on the classic CLI), so it is recorded, not fixed.
3. **Skill wording leaked into drafts (fixed).** The onboarding skill's sample contact line (`owner, final
   approver`) appeared as a fact in a real draft. The examples are now placeholders.
4. **The model sent a questionnaire when onboarding started from nothing (skill tightened; one criterion
   unresolved).** It listed six sections; the skill now says to ask at most three follow-up questions in total,
   in one short message. The next run (`local-cb048c32...771`) asked three numbered questions and then, in the
   same message, a fourth: whether "Riverbend Dental" is the name they go by. No draft existed at that point,
   so this was not a draft confirmation but an extra follow-up, over the limit. The three-question criterion is
   therefore **unresolved** for that run; the skill text itself is correct and the excess is model behavior,
   not code.
5. **Observation, no code change.** For "run the library health check" the model ran a Python snippet through
   the terminal instead of telling the user about `/engagements doctor`, and in two TUI turns it read
   `Client.md` with `read_file` beside the plugin tool. The spec already says agents may use general file tools.
   Neither skill mentions `/engagements doctor` (spec 6.5 does not ask for it), which is a candidate wording
   change for a later phase, not a defect.

### Reproduce (Phase 3)

```bash
export P3=$PWD/.scratch/p3   # hxk: the spike/hx wrapper with an optional DEEPSEEK_API_KEY pass-through
# fresh home, plugin copied to home/.hermes/plugins/engagements and enabled; profile create --clone studio-dev|studio-ops
# default profile: model.provider deepseek; studio profiles: provider custom -> loopback forwarder holding the key
hxk -p studio-dev engagements init --root $P3/home/shared-library   # same for studio-ops
hxk -p studio-ops chat --query-file q.txt -Q                        # steps 1 to 6, from the folder under test
tmux new-session -d "hxk -p studio-ops --cli"   # or: --tui ; send keys, capture panes
```

## Phase 3D: Hermes Desktop spike (2026-11-06)

**Surface under test: Hermes Desktop**, the Electron app, run from source on the test desktop (a Linux mini PC with a
Hyprland/Wayland session, driven over SSH). **Build:** Hermes Agent v0.23.0+127 (`6b58832d8`), the same commit
as the Phase 3 CLI and TUI runs, built from a fresh clone under `$HOME/.cache/<scratch>-eng/src` with its own
`HERMES_HOME`, `HERMES_DESKTOP_USER_DATA_DIR` and `HERMES_DESKTOP_APP_NAME`, so it shared no state and no
single-instance lock with the owner's Hermes. The app window was a real window on the real display; the page was
driven through Chrome DevTools Protocol (`--remote-debugging-port`, loopback) for typing and clicks, and checked
with screenshots. **Model:** a deterministic stub on loopback (no real model, no key, no network) that calls the
`engprobe_where` probe tool through the Tool Search bridge and then echoes. **Plugin under test:** the Phase 0
`engprobe` probe plugin (`spike/engprobe`), which logs what `ctx.session_surface()`, `ctx.session_cwd()` and the
hook and handler kwargs return at each seam. Fictional folders only.

**Version correction.** The plan expected the owner's installed Hermes (v0.22.5) to predate the
`session_surface()`/`session_cwd()` plugin APIs. It does not: `v0.22.5+canary.20261027` contains
`hermes_cli/plugin_session.py` (read-only check; nothing there was changed). In this repository's tags the
module first appears in `v0.22.3` (2026-10-21) and is absent from `v0.22.2`. Only `6b58832d8` was exercised.

### Seam results on Desktop

| Seam | Result | Evidence |
|---|---|---|
| S1 tool handler (through the Tool Search bridge) | **Pass** | The probe tool was not in the model's direct tool list (21 tools: `tool_search`, `tool_call`, no `engprobe_*`); the stub called it through `tool_call`. The handler received `session_id` and `task_id` (kwargs `session_id`, `task_id`, `user_task`), and `ctx.session_cwd()` inside it equalled the session's workspace in both runs below. |
| S2 `pre_llm_call` banner | **Pass** | The hook fired each turn with the same `session_id` as S1, `platform` `desktop`, `is_first_turn` true then false. The banner text was appended to the user message in the requests the model received on later turns. The visible transcript (`messages.content`) holds no banner; the banner is persisted in the API sidecar, `messages.api_content`, as SPEC 6.4 and A4 expect. Fresh receipt: `docs/evidence/p3d/banner-api-content.txt` (user row 1: `content` is the user's text only, `api_content` is the text plus `[Engagements] Client: Riverbend Dental ...`; the model's request carried it). |
| S3 slash command (`/engprobe`) | **Pass for identity; the surface is `local`** | The handler received `session_id` equal to the S1/S2 session id (full 70-character string compared) and `session_key` `agent:main:local:dm:<session id>`. Handler kwargs were `platform`, `session_id`, `session_key`, `source`. **`ctx.session_surface()` inside the slash command returned `local`**, and `platform` was `local`, while the same session's tool and hook calls reported `desktop`. The slash handler's `ctx.session_cwd()` was nevertheless the correct workspace (below). |
| `session_surface()` inside a turn | **`desktop`** | S1 tool, S2 hook, `on_session_start` and `on_session_end` all returned `desktop`, with and without a Project. |

### cwd with and without a Project

| Case | `ctx.session_cwd()` (tool, hook and slash command) | How the session was created |
|---|---|---|
| No Project (a new session in the **Home** group) | The Desktop default workdir: here `/home/<user>`, because the launcher passed `HERMES_DESKTOP_CWD`; with none set Desktop falls back to the user's configured default project directory, then the launch directory, then the home directory (`resolveHermesCwd` in `apps/desktop/electron/main.ts`). | Sidebar "New session" |
| A Project selected | The Project's folder, exactly: `.../fixtures/acme-site`. | Project "Acme Website" created with `hermes project create "Acme Website" <folder>` (the app reads `projects.db`), then the sidebar's "New session in Acme Website" button |

The Desktop folder picker (`Add context`, "New project" dialogs) is a native GTK portal dialog; it was not driven.
Creating the Project from the CLI against the same `HERMES_HOME` is an equivalent route to the same record the
dialog writes. **Not exercised:** moving an existing session to another workspace, and Desktop's SSH remote mode
(`~/.hermes/desktop-ssh`): where tools and `session_cwd()` run in that mode is unknown, so the plugin must not
claim remote Desktop sessions.

### Plugin on Desktop and the `/client` decision

Code head under test: the commit "Let Desktop resolve cwd and let /client act on its session's turn-verified
surface" on `p3d/desktop`. Design chosen: **option (a)**, bounded. The per-turn hook records the last
turn surface that is in `CWD_SURFACES` under `surface:<session_id>`; `/client` replaces `local` with that record
for the same session id only (never `local` -> `cli`). With no recorded turn `/client` stays read-only. This
changes CLI and TUI too: `/client` status can resolve cwd there once a turn has run. The CLI/TUI behaviour is
covered by unit tests (`tests/test_plugin_desktop_surface.py`) and was **not** re-run on the CLI or TUI in this
phase. Minimum Hermes: `plugin_session.py` first appears in v0.22.3; `plugin.yaml` carries
`requires_hermes: ">=0.22.3"`.

## Phase 3D acceptance on Hermes Desktop (2026-11-06)

**Surface: Hermes Desktop**, same build, window and driving method as the spike above, plugin `engagements`
enabled in a scratch home, real model DeepSeek V4 Pro through the provider key in process environment only. One
default profile; fictional data (Acme Bakery, Riverbend Dental); folders `studio/acme/website-2026`,
`studio/riverbend/recall`, `studio/shared-assets` and the parent `studio`, each created as a Desktop Project with
`hermes project create` (the folder picker dialog was not driven).

| SPEC 9 step | Result on Desktop | Evidence |
|---|---|---|
| 0 single-profile baseline: onboard, add a project, bind, update, restart, all in chat | **Pass** | Onboarded Acme Bakery from pasted notes in a session in the Acme Project; project `acme-website-2026` added; the session auto-bound; a project location was updated by `engagements_update`; the app was restarted and the records and bindings were still there. No CLI setup beyond creating the Desktop Projects. |
| 1 onboard Acme from pasted notes, an Unconfirmed item, correct one fact | **Pass** (single profile only) | The draft showed `Domain registrar: Namecheap` under Unconfirmed; check-in corrected from Monday to Tuesday before saving; the file has the corrected line and the Unconfirmed item. |
| 2 onboard Riverbend conversationally | **Pass** (single profile only) | The model asked its questions through Desktop's question panel, drafted, then saved on confirmation. |
| 3 projects, shared folder | **Pass** | `acme-website-2026`, `riverbend-recall-campaign`, `riverbend-brand-assets`; the shared folder was added to Acme by `engagements_update` and the doctor reports two `shared_location` notices. |
| 4 auto-binding by folder | **Pass** | A turn in the Acme Project bound Acme; in the Riverbend Project bound Riverbend; the shared folder was `ambiguous` with nothing bound (`/client` and the model both said so); the parent folder was `Not bound` with evidence "no project location contains cwd". Two sessions in the same profile held different bindings (`/client` run in each: Acme and Riverbend). The banner reached the model on the second turn (it named client and project with no tool call). The visible `state.db` transcript carries no banner; `messages.api_content` does (receipt above). The earlier "no banner text in `state.db`" reading looked only at `messages.content`. A fresh session with no turn yet showed `/client` read-only ("not available on this surface"), as designed. |
| 5 cross-profile update visible | **Not exercised** | Needs two profiles; Desktop was run with one. Cross-profile behaviour was covered on the CLI only in Phase 3 (the TUI was not run for this step). |
| 6 stale-rev conflicts | **Not exercised on Desktop** | Needs an editor save between tool calls, not driven through Desktop. Covered on the CLI only in Phase 3 (not on the TUI) and in unit tests. |
| 7 explicit bind, compaction, restart (mandatory) | **Pass** | In the shared-folder session `/client riverbend-brand-assets` replaced `ambiguous` with `Mode: explicit`. In the Acme session `/client riverbend-dental` (explicit, overriding the folder), then `/compress` -> `Compressed: 36 -> 13 messages`; the session id did not change, `/client` still reported `explicit`, and the model, with no tool call, still named Riverbend Dental. The app was stopped and relaunched (new process start time), the session reopened from the sidebar: `/client` still `explicit`; `/client list` printed both clients and all three projects; `engagements_find` returned the three project ids; `/engagements doctor` printed `Doctor: clean (2 notices)`. |
| 8 plain text editor | **Pass** | Library files read on disk as plain Markdown with front matter (`Acme Bakery/Client.md` etc.). The Obsidian check was not run. |

**Surface values observed.** Turn (tool, hook, session start/end): `desktop`. Slash command: `local` (also
`platform` `local`), with the correct `session_id`, `session_key` and `session_cwd()`.

**Reproduce.** Build the commit with `hermes desktop --source --build-only` under a scratch `HERMES_DESKTOP_*`
set (it also writes a launcher entry under `~/.local/share/applications`; see the cleanup note), launch the
Electron binary with `--remote-debugging-port` on loopback, create Projects with `hermes project create`,
drive the page over CDP.

### Key hygiene and cleanup (Desktop run)

The provider key was read on the build host, piped over SSH stdin into the launching process environment only, and never
written to a file on the test desktop. Before cleanup `grep -rlF` found **0** hits in the scratch tree, `/tmp`, `/dev/shm`,
`/run/user/1000`, `~/.cache/<scratch>-oa`, and 0 matches in the user journal and shell history. Cleanup, read back:
Desktop and its gateway stopped, scratch install and home removed, shell lock removed, idle restored
(`<desktop-shell> idle status`: `enabled: true`, `stayAwake: false`; lock `locked: false`). **Corrected by the isolation
run below:** the claim that the real `~/.hermes` was untouched rested on modification times alone, and the launcher-entry
account here was wrong. The source build did not create `com.nousresearch.hermes.desktop`; it rewrote the owner's existing
entry, and the legacy `hermes.desktop` that this run's cleanup deleted also pre-existed (both carried
`Exec=/home/<user>/.local/bin/hermes desktop --skip-build` earlier on 2026-11-06). The packaged Desktop
(`/opt/hermes-desktop`, `/usr/share/applications/hermes-desktop.desktop`) was not touched.

### Real-home isolation run (2026-11-06)

Receipts: `docs/evidence/p3d/` (index and caveats in its `README.md`). One bounded run on the test desktop, **behind the lock screen**
(`locked: true` before, during and after; nothing was unlocked or changed), scratch Hermes `6b58832d8e8d`, stub LLM, Desktop
driven over CDP, two turns in one session, with the plugin loaded.

| Claim | Result | Receipt |
|---|---|---|
| Desktop can start and be driven while the test desktop is locked | **Yes.** Hyprland runs behind the lock; the window loaded, the gateway came up and both turns completed over CDP. | `desktop-startup-log.txt` |
| No real-home descriptors observed in spike processes | **0 fds under `/home/<user>/.hermes/`** in all 11 processes probed (main, zygotes, GPU, utility x2, renderer, crashpad, bash wrapper, the gateway), and in every other <user> process, at both probes (23:08:33 and 23:08:50). These are point-in-time samples: an open and close between probes would not be seen. | `docs/evidence/p3d/README.md` (summary of the removed raw listings) |
| The spike used the scratch home | **Yes.** Desktop logged `Using Hermes source at /home/<user>/.cache/<scratch>-eng/src`; the gateway carried `HERMES_HOME=/home/<user>/.cache/<scratch>-eng/home` and held 20 scratch-home fds; its `state.db`, logs and locks are there. | `desktop-startup-log.txt`, plus the summary in `docs/evidence/p3d/README.md` |
| Real `config.yaml` and `state.db` unchanged | **Yes.** sha256 identical before (22:56) and after (23:13): `f79772f3...6e0178` and `9f265734...a86311`; sizes and mtimes unchanged; `state.db-wal` still 0 bytes. | `docs/evidence/p3d/README.md` (summary of the removed snapshots) |
| `state.db-shm` | **Moved; attribution inferred, not proven.** It moves on a five-minute cadence (22:52:23, 22:57:23, 23:02:22, 23:07:22, 23:12:21), including before the build started and after everything was stopped, consistent with the desktop shell's agents collector, which opens `state.db` read-only on that cadence. No holder was captured at any probe (neither the bridge nor the collector), so the attribution rests on cadence and source reading. | `state-db-shm-and-holders.txt` |
| Banner is persisted in `messages.api_content`, not the visible transcript | **Yes.** User row 1: `content` is the user's text only; `api_content` carries it plus the `[Engagements]` banner; the stub shows the model received it, and again replayed in the history on turn two. | `banner-api-content.txt` |

Scoped conclusion: for this build, launch path and two-turn session, **no real-home descriptors were observed in the sampled
Desktop and gateway processes at either probe, and the recorded real `config.yaml` and `state.db` hashes match before and
after**. `state.db-shm` changed on a cadence consistent with the read-only collector, including before launch and after
teardown; that attribution is inferred, and no holder was captured. This is sampled evidence, not proof that no access ever happened. It does not cover
Desktop actions this run did not take (updating, the folder picker, remote mode, installing plugins from the UI), and it is
not a statement about the launcher entry, which the build did touch (below). The banner appeared on the first turn
here because the Desktop default workdir (the scratch user's home) was inside the Recall Campaign location; no Project was
selected, which is also the SPEC 6.4 control that a location containing the default workdir binds.

**Things the run got wrong.** The first launch ran without PyYAML in the scratch venv, so the plugin did not load; that was
fixed in the scratch venv and the run repeated, and all receipts above are from the second launch. The source build
rewrote the owner's launcher entry `com.nousresearch.hermes.desktop` at 22:59:13 (and regenerated `mimeinfo.cache`).
It was restored by inference, not from a backup: `Exec=` was put back to `/home/<user>/.local/bin/hermes desktop --skip-build`, which reproduces the original 267-byte size (not verified byte-for-byte) and the Exec recorded in an earlier session and
its mtime was set back to 22:42:48.931319872; `cleanup-readback.txt` shows that. The legacy `hermes.desktop` that the earlier
P3D cleanup deleted is intentionally not recreated: the operator dispositioned it (current Hermes keeps
it only as a hidden alias for old pins, and this machine's Desktop install does not use it). Cleanup of the scratch tree is read back in
`cleanup-readback.txt`; the shared uv and npm caches under the owner's `~/.cache` and `~/.npm` were left alone.

Wording note: the Phase 3 transcripts above that quote `Last binding from a CLI/TUI session:` show the text as it
was in that run. The line now reads `Last stored automatic binding:` (it asserted a provenance the plugin cannot
authenticate), and stored ids are shown only when they have the library's id form.

## Phase 3V: Clients dashboard acceptance (2026-11-08)

SPEC 6.7 on both front ends, fictional studio library (`tests/fixtures/studio`: Acme Bakery, Riverbend Dental, Maple
Street Florist; three project folders present, Maple's missing on purpose), stub LLM only, no provider key. Agent writes
went through real `engagements_update` tool calls, not file edits.

**Web dashboard, the build host.** Scratch home via `spike/hx dashboard --port 9171 --host 127.0.0.1 --no-open --skip-build`,
Hermes `v0.23.0+127.g6b58832`, driven in a real browser.

| Check | Result |
|---|---|
| Clients tab registered; plugin API needs the dashboard session | **Pass.** `/api/dashboard/plugins` lists it; `/api/plugins/engagements/overview` refused without the session token. |
| List and record view (`acme-website-2026`, `maple-spring-window-2025`) | **Pass after two fixes.** The run found the Acme repo URL listed as a folder badged "not found here" (SPEC 4: a URL is a reference), and the two-pane layout collapsed to one column because utility classes the plugin used (`grid-cols-[...]`, `pl-6`) are not in the host's compiled CSS. Both fixed in `61612f7`: a `local` flag from `dashboard_data._location` via `binding.is_local_path`, folder counts and badges from local paths only, URLs under "Links and references", layout in inline style. Rechecked at `415479f`: no element past the page edge at 1042 px or 900 px. |
| Privacy mode | **Pass.** Initials for names; ids, paths and record text hidden; still on after reload (`hermes.plugin.engagements.privacy` in localStorage, the storage at that head; the tab now keeps the flag in sessionStorage so it is per window, SPEC 6.7); turned off. |
| Refresh after an agent update | **Pass.** With the record already open, `engagements_update` appended a note through the agent; the open record did not show it until **Refresh**, then did. (A first attempt was discarded: selecting the record refetches it, which hid the stale state.) |

**Hermes Desktop, the test desktop.** Receipts: `docs/evidence/p3v/` (index, isolation account and caveats in its `README.md`). Four
bounded runs behind the lock screen, scratch source build of `6b58832d8e8d`, Desktop driven over loopback CDP.

| Check | Result | Run |
|---|---|---|
| Plugin loads (agent and Desktop halves) | **Pass** at `v0.23.0+127`. Run 1's clone of the fork lacked upstream tags and reported `0.15.1`, so the gateway skipped the agent half (`requires hermes >=0.22.3`); later runs fetched the upstream `v0.23.0` tag. A harness defect, not a plugin one. | 2, 3, 4 |
| Sidebar entry, **Open Clients** palette command, header counts and health | **Pass.** `3 clients, 4 projects, 3 active`, `library healthy`. | 1, 4 |
| Layout | **Pass at `a0ddc24`.** Runs 1 to 3 found the record pane past the window: first the grid's `1fr` tracks (fixed `415479f`), then the SDK `ScrollArea`, whose Radix viewport wraps content in a `display: table` box that grows to its content (`34387f1`, plain overflow box), then a labelled Copy button 23 px past a 900 px window and the record-file path squeezed to 0 px (`a0ddc24`: icon copy buttons, list column at most 35%). Run 4 measured both records at 800, 900, 937 and 1280 px: nothing past the page edge, record-file path 74 to 389 px wide. | 1 to 4 |
| Record content, folders and references | **Pass.** Acme: id, client, record file, two folders, the URL under "Links and references", list badge `2 folders` without warning style. Maple: `not found here`. | 2, 4 |
| Reveal on a missing folder | **Partial.** The copy action produced the copied-path notice, but no independent read confirmed the clipboard's contents (the comparison returned nonzero) and the clipboard was not restored. This is a notice, not a verified copy. Reveal of a found folder not exercised. | 1 |
| Start a chat | **Pass.** New chat with `/client acme-bakery` in the composer, nothing sent (no user message, stub log unchanged) until Enter. | 2 |
| Binding chip | **Pass on the bound states; the fresh-chat state was not observed.** The chip was **absent** on a fresh chat (`chip:null` in `run2-start-chat.txt` and `run3-binding.jsonl`), so `No client` is not a runtime result. It read `Acme Bakery` about 0.9 s after sending `/client acme-bakery`, no reload, and `Riverbend Dental` after `/client riverbend-dental`. | 2, 3 |
| Privacy mode | **Pass.** Includes the chip; persisted through `Page.reload`. | 2 |
| Live refresh of an open view | **Pass.** The stub was delayed 8 s so the update landed while Clients was open on the Acme record with the marker absent. The renderer received `plugin.engagements.changed` `{"what": "library"}` at 11:30:58.887 and the marker was in the DOM at the next poll (11:30:59.000), with no click. `/client` produced `{"what": "binding"}`. The individual refetch requests were not captured. | 3 |
| Read-only browsing | **Pass** by file list and library Markdown sha256 before and after a stretch of viewing with no agent turns. | 2 |
| Console | No Engagements or plugin warnings or errors in the collected intervals; collection started after renderer startup, so startup itself is not covered. Electron's development CSP warning only. | 2 to 4 |

Not exercised: the chip reading `No client` on a fresh chat (the chip was absent, `chip:null`), an independently confirmed copy of a missing
folder's path (the run saw the notice only), reveal of a found folder, Desktop screenshots after run 2 (CDP capture timed out), the web
dashboard on a non-loopback bind, and Desktop's own Plugins-settings switch (the runs enabled the Desktop half through
`hermes.desktop.pluginDecisions.v2`).

The empty-page starter prompts (D12) were added after these runs and are not runtime-exercised on either front end. Their
behaviour is covered only by `tests/test_dashboard_frontends.py`, which loads the real `desktop/plugin.js` and
`dashboard/dist/index.js` under Node against a mocked SDK and drives the buttons; no Desktop or browser click-through was performed
for them. "Add a client" on a populated page and "Add a project" on a client record (D13) were added later still. They are covered by
the same front-end tests, and both halves were clicked through on 2026-11-11 against a scratch home holding the demo library, with
real mouse events sent over the Chrome DevTools Protocol.

**Web dashboard tab, on loopback.** Each button showed its prompt and copied it to the clipboard, and neither sent a request; the
only requests in the pass were the record reads from opening records. "Add a project" was absent from a project record. With
Privacy on, the prompt on screen showed "…" for the id while the copied prompt kept it. At window widths of 420, 700, 1042 and
1350 px the search field kept its own row with its placeholder whole, and both labels stayed on one line.

**Hermes Desktop**, a packaged Linux build of a local Hermes source checkout (v0.23.0+128), with scratch Electron user data, no
model provider configured, and the Desktop half enabled through `hermes.desktop.pluginDecisions.v2`. "Add a client" opened a new
chat with its prompt in the composer, and "Add a project" on a client record did the same with that client's id, replacing the
earlier draft. Neither was sent: the composer held the text and the session list still read "No sessions yet". "Add a project" was
absent from a project record. With Privacy on, the page hid the name and id while the drafted prompt kept the id, as "Start a
chat" does. At window widths from 800 to 1440 px the search field kept its own row with its placeholder whole and every label
stayed on one line; from 1100 px up the two record actions sat on the name's line, and at 1000 px and below they wrapped to the
next line together.

Not checked for D13: sending a drafted prompt and the agent's reply (no model was configured), the copy fallback when a draft
cannot be placed, a stock Desktop release build, and Desktop on Windows or macOS.

## v1 status (2026-11-06)

SPEC 8 says v1 is done when Phases 0 to 4 pass, the acceptance scenario is recorded here with
the surfaces named, and an independent review of this branch is clean. This is the audit of those criteria at
the `1.0.0-rc1` packaging head on branch `p4/package`. "Open" means no evidence exists yet; a
scoped pass does not silently upgrade an untested step.

| SPEC 8 criterion | Status | Evidence |
|---|---|---|
| Phase 0: runtime spike, nine seams, per surface | **Done** 2026-11-04 | "Phase 0: runtime spike"; nine seam rows, five with amendments; classic CLI one-shot, classic CLI interactive, TUI |
| Phase 1: core library, tests first | **Done** | Core modules under `engagements/` import no Hermes module; the suite covers the parsing contract, discovery boundaries, every doctor case, every op, rebase versus conflict, locks, creates, collisions, history and cwd matching. At this head: 2208 passed, 3 skipped on Python 3.14.7 and 2208 passed, 3 skipped on Python 3.11.15 |
| Phase 2: plugin wiring (tools, hook, commands, CLI, settings) | **Done**, including the fixes made after the recorded runs | "Phase 2: plugin wiring"; 5 tools, 4 hooks, 2 commands and 1 CLI command; `hermes plugins doctor . --ci` exit 0 and `hermes plugins validate .` 15 checks passed |
| Phase 3D: Desktop | **Done, with the gaps below** | "Phase 3D: Hermes Desktop spike" and "Phase 3D acceptance on Hermes Desktop"; cwd binding enabled for `desktop`, `/client` uses the session's turn-verified surface |
| Phase 3: skills and acceptance on every matrix surface | **Run on the CLI, with the follow-up-limit criterion unresolved for one run; partial for the TUI** | Both skills written in that phase are in `skills/*/SKILL.md` and registered; "Phase 3 acceptance" records CLI steps 0 to 8 and TUI steps 4 and 7 |
| Phase 3V: dashboard (SPEC rev 6) | **Done for the exercised checks, with the gaps below** 2026-11-08 | "Phase 3V: Clients dashboard acceptance": web tab on the build host, Desktop on the test desktop; five defects found and fixed in the run. Not exercised there: a fresh-chat `No client` chip, an independently confirmed missing-folder copy, found-folder reveal, Desktop screenshots after run 2, the web dashboard on a non-loopback bind, and Desktop's own Plugins-settings switch |
| Phase 4: package | **Done** | `README.md` with the four experiences, generated examples, settings, surface matrix, honest limits and the local-files/no-network/cwd disclosure; `LICENSE` (MIT, Chris Gray); `plugin.yaml` and `pyproject.toml` at `1.0.0-rc1`; `tests/test_readme.py` keeps tool names, examples and metadata tied to the code |
| Acceptance scenario recorded with the surfaces named | **Recorded, with the gaps below** | "Phase 3 acceptance" (classic CLI, TUI) and "Phase 3D acceptance on Hermes Desktop" (Desktop) |
| Independent review | **Open** | A review of this branch is the gate; it has not reported at the time of writing |

Gaps carried in the record, each already stated in its section:

- **Classic CLI:** steps 0 to 8 were run and pass, except that the onboarding follow-up limit is
  unresolved for one run (below). The `.engagements/history/` directory was not listed at
  runtime after step 6, so history retention is covered by tests, not by a runtime claim.
- **CLI/TUI `/client` after a turn:** the turn-verified resolution added in Phase 3D is unit-tested
  only on the CLI and TUI; earlier CLI/TUI runs showed read-only stored status.
- **Obsidian:** the optional compatibility check was not run.
- **TUI:** steps 0, 1, 2, 3, 5 and 6 were not exercised. Steps 4 and 7 pass.
- **Desktop:** steps 0 to 4, 7 and 8 pass. Steps 5 and 6 (two profiles; an editor save between
  tool calls) were not exercised. SSH remote mode, moving an existing session to another
  workspace and the native folder picker dialogs were also not exercised.
- **Messaging gateway platforms:** not exercised, no support claim; the matrix marks cwd binding
  `cwd_unsupported` there.
- **Onboarding follow-up limit:** one Phase 3 run asked a fourth follow-up question. The skill
  text limits the agent to three and the extra question was model behavior, so that run's
  criterion stays unresolved even though the skill text is correct.
- **`plugins.isolation: host`:** the S1 to S3 probe and the commands pass in host mode; the
  `hermes engagements ...` subcommand is unavailable there by design (A9).
- **Windows:** native CI only (surface: GitHub Actions `windows-latest`, Python 3.11 and 3.14, the full
  pytest suite including `tests/test_windows_native.py`). That covers the handle-confined directory seam,
  reparse-point refusal (junctions and symlinks), held-directory rename refusal, `CREATE_NEW`, `MoveFileExW`
  replace and `msvcrt` locks between threads and processes. It is not a Hermes CLI, TUI or Desktop run on
  Windows, and no such runtime claim is made. The attended checklist for a real machine is
  `docs/WINDOWS-ACCEPTANCE.md`.
