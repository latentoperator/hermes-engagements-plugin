# Contributing

Thanks for taking a look. This is a small Hermes plugin: a file-based client foundation, with a pure Python core, a tool layer, and two read-only Clients front ends. The rules below are the ones that keep it reviewable.

## Read first

- `docs/SPEC.md` is the build contract.
- `docs/TOOLS.md` is the normative tool contract. Tool schemas, result envelopes and error codes are tested against it, so change the document and the code together.
- `docs/RUNTIME-EVIDENCE.md` records what has actually been run and on which surface.
- `AGENTS.md` holds the working rules in the maintainer's words. This file restates them for a human contributor.

## Run the tests and lint

The suite needs Python 3.11 or newer and PyYAML 6.x. No project install is required: `uv` resolves the two test dependencies into a throwaway environment.

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --python 3.14 \
  --with pytest --with 'pyyaml>=6,<7' python -m pytest -q -p no:cacheprovider
```

Run the same command with `--python 3.11` as well. CI runs both versions on Ubuntu and Windows (`.github/workflows/ci.yml`), so a change that only passes on one of them is not ready.

Lint is not restated here. The README's Development section carries the lint command and names the configuration it needs, and that copy is the one kept in step with this repository; use it rather than a copy taken from elsewhere.

Before opening a pull request, both plugin checks should exit 0:

```bash
hermes plugins validate .
hermes plugins doctor . --ci
```

## What the tests are for

- **A bug fix comes with a regression test** that fails before the fix and passes after it. Name it by the behaviour, not by the ticket or the review that found it.
- **Tool descriptions are pinned.** `tests/test_readme.py` fails if a tool name or a README example drifts from the code, so regenerate the examples with `scripts/readme_examples.py` instead of editing them by hand.
- **Runtime claims are separate from tests.** A unit test is not a runtime result. Anything about a real surface (CLI, TUI, Desktop, gateway) belongs in `docs/RUNTIME-EVIDENCE.md`, with the surface named, or it is not claimed.
- **Read-only means read-only.** The dashboard is covered by tests that inventory the filesystem before and after; keep them passing rather than loosening them.

## Rules in plain words

- **Fictional data only.** Fixtures, tests, docs and demos use invented clients (Acme Bakery, Riverbend Dental, Harbor Print Shop, Maple Street Florist) and `.example` domains. No real client names, contacts, host names, user names, home paths or screenshots.
- **Stock Hermes is the baseline.** Every v1 experience works with nothing but stock Hermes and this plugin: no Obsidian requirement, no vault, no extra profiles, no CLI setup step, no external service. Obsidian compatibility is a target, not a dependency.
- **Public plugin APIs only.** No Hermes core patches, and no imports of Hermes private modules.
- **The core stays Hermes-free.** Modules under `engagements/` must not import Hermes, so they stay unit-testable. This must print nothing:

  ```bash
  git grep -n -E "^(from|import) (hermes|agent|gateway|tools)\b" -- engagements/
  ```

- **Document behaviour in the same change.** If a change alters documented behaviour, amend `docs/SPEC.md`, `docs/TOOLS.md` or `docs/RUNTIME-EVIDENCE.md` in that change rather than after it.
- **Say why in the commit message.** Explain the reason for the change, not just the files touched. No emojis in code, docs or commit messages.
- **`spike/` is history, not the product.** It is the Phase 0 harness kept as evidence for the runtime record. Do not build new work on it, and do not delete it: the evidence references it.

## Pull requests

Keep a pull request to one kind of change and describe what you checked. If a change touches a write path, say which platform you exercised it on; Windows behaviour is covered by native CI, not by a run on a Linux machine. Releases, tags and catalog submissions are the maintainer's to make.

## Licence

By contributing you agree that your contribution is licensed under the repository's MIT licence.
