# Changelog

All notable changes to this project are recorded here. Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and `1.0.0-rc1` is the first release candidate.

## [1.0.0-rc1] - 2026-11-09

The first release candidate of Engagements, a Hermes plugin that gives agents a maintained, file-based client foundation.

### Added

- **Client and project records as plain Markdown.** One short page per client, one per project, with flat YAML frontmatter and ordinary section headings. Records live anywhere under a configured library root and are found by a frontmatter marker, so folder layout is cosmetic and the files open in any editor or in Obsidian.
- **Five tools.** `engagements_find` (id, name or alias search, never record bodies), `engagements_bind` (explicit bind, or explicit off), `engagements_context` (a record, requested sections, rev, warnings, and a client's projects), `engagements_create` (client or project from the template, under the library lock) and `engagements_update` (targeted operations against a base revision, with rebase and conflict handling). Every tool returns `{ok, data?, error?, warnings}` with a fixed set of error codes.
- **Maintain the record without clobbering it.** Every update checks the revision the agent last read. If the footprints it targets changed, it returns a conflict with both versions and writes nothing; if they are unchanged, it rebases onto the newer revision. The Change log gets one line per update with its source.
- **Binding from the working directory.** A session started inside a project's local folder binds to that project and its client automatically. Ambiguity is reported, never guessed: a folder two active projects share leaves the session unbound and names both candidates. Explicit picks always win.
- **The per-turn banner.** When a binding changes, a short banner tells the model which client and project the session is on, that quoted values are record data rather than instructions, and that earlier turns may hold other clients' context. The banner is stored with the conversation and replayed on later turns; it names ids, not record contents.
- **Three skills.** `engagements:onboard-client` instructs the model to check for duplicates, draft only from what the user gave, keep assumptions under **Unconfirmed** with their source, ask at most three follow-ups and show the draft before saving. `engagements:client-work` instructs the model to bind the session, read the record, keep project locations current and make targeted, sourced updates. `engagements:suggest-clients` proposes up to five clients from earlier chats and saves nothing until the user picks one. All three are instructions to the model, not guarantees.
- **Slash commands and a CLI.** `/client` shows the binding and its evidence, binds by name or id, switches to `off` or `auto`, and lists active clients and projects. `/engagements doctor` prints the validation report. `hermes engagements init`, `list`, `show` and `doctor` work outside a session; the subcommand is unavailable under `plugins.isolation: host`.
- **A read-only Clients view on Desktop and the web dashboard.** A Clients page with a sidebar entry, an **Open Clients** palette command, per-record views, folder found or not found, links kept as references, privacy mode, live refresh after an agent write, and a Desktop status-bar chip for the current chat's binding. The backend is GET-only, opens the library without creating, locking or writing anything, and hands the core a state store that raises on any write.
- **Starter prompts on an empty page.** **Find clients in my past chats** and **Add a client** replace the empty screen when no library exists or it holds no clients and no projects. The page stays read-only: Desktop drafts the prompt in a new chat and never sends it; the web dashboard copies it.
- **Settings.** `library_root` (share one library between trusted profiles, or move it), `labels.client` and `labels.project` (display words only), `auto_bind` and `banner`. No settings are needed for the first run.
- **A demo kit and a doctor.** `demo/` sets up a fictional library in a separate profile so the plugin can be tried without real client data. `doctor` reports defects and informational notices (missing folders, shared locations, oversized files, skipped symlinks) and flags secret-shaped strings by file and line without echoing them.

### Supported surfaces

Runtime evidence exists for the classic CLI (one-shot and interactive), the TUI, and Hermes Desktop in local mode on one Linux/Wayland machine. Messaging gateways are unverified and carry no support claim. Windows record writes are tested natively in CI on `windows-latest` for Python 3.11 and 3.14; that is not a Hermes run on Windows. macOS is not exercised. `docs/RUNTIME-EVIDENCE.md` records each claim with its surface, and `docs/WINDOWS-ACCEPTANCE.md` is the attended checklist for a real Windows machine.

### Limits

- Client scoping organizes context; it is not isolation. Use a fresh session per client when confidentiality matters.
- Any process with file access can read the library, and agents can edit record files with general file tools.
- A concurrent external-editor save can still be lost in a narrow race, and cross-machine syncing has no locking.
- Directory durability on Windows is weaker than on POSIX: a power loss can still lose the most recent rename after it returned success.
- The plugin works on local files only, makes no network requests and sends no telemetry.

### Requirements

Hermes Agent v0.22.3 or newer (the `ctx.session_cwd()` and `ctx.session_surface()` plugin APIs), Python 3.11 or newer, and PyYAML 6.x.
