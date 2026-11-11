# Security policy

## Report a vulnerability privately

Use GitHub's private vulnerability reporting on this repository: **Security** tab, then **Report a vulnerability**. That opens a private advisory visible only to you and the maintainer. Please do not open a public issue for a security problem, and do not include live secrets or real client data in a report.

There is no email address for security reports. Private vulnerability reporting is the channel.

## What is in scope

- The plugin's tool handlers and core library under `engagements/`.
- The plugin wiring: hooks, slash commands, the `hermes engagements` CLI, and settings handling.
- The read-only Clients backend and the two front ends under `dashboard/` and `desktop/`.
- The filesystem boundary: writes confined to the library root, reparse-point and symlink handling, lock behaviour, and the Windows NT write path.
- Redaction of secret-shaped strings in diagnostics.
- Anything that lets a record be read or written outside the configured library root, or that makes a write land somewhere the plugin did not resolve.

## What is out of scope

- **The plugin is not an isolation boundary, and does not claim to be.** Client scoping organizes context; it does not seal one client's information away from another. Earlier conversation, compaction summaries and memory recall can still contain other clients' details. Any process with file access can read the library, and an agent can edit record files with general file tools. Those are documented limits, not vulnerabilities: see "Honest limits" in the README.
- **The plugin is local-only.** It works on local files, makes no network requests and sends no telemetry. What an agent reads from a record becomes part of the conversation and therefore reaches whichever model provider your Hermes uses; that is Hermes behaviour, not this plugin's.
- Vulnerabilities in Hermes itself. Report those to the Hermes project.
- Issues that require a secret to be stored in a record first. Keep secrets out of records; the plugin flags secret-shaped strings by file and line but does not stop them being written.
- A concurrent external-editor save being lost. The plugin narrows that window by re-hashing before it replaces a file; it does not claim to close it, and cross-machine syncing has no locking at all.
- Findings from a profile or configuration the plugin does not support, such as `plugins.isolation: host` paths that are already documented as unavailable, or messaging-gateway surfaces that are documented as unverified.

## Supported versions

Fixes land on the current release line. `1.0.0-rc1` is the release candidate this policy covers.

## What to expect

The maintainer will acknowledge a private report, ask for anything needed to reproduce it, and tell you when a fix is available. Please give a reasonable window before public disclosure.
