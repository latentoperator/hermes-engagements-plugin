"""The documents a stranger reads first, held to what the repository can support.

This plugin is published, so the root files and the guides have to work for
someone who has never seen the project: a quick start whose binding advice is
the rule the code applies, contributor commands that run from this checkout,
Desktop claims that say which surface was actually run, no internal board or
process vocabulary, no personal deployment detail standing in for a decision,
and project files a public repository is expected to carry.

Recorded output is deliberately exempt. Receipts under ``docs/evidence/``
reproduce what a run printed, including the fixture library's own change-log
author, and are not rewritten for house style; the index README of each
evidence folder is authored text and is checked. Generated example blocks in
the README are exempt for the same reason: ``scripts/readme_examples.py``
produces them from the fixtures.
"""

import re
from pathlib import Path

from engagements.binding import match_projects

ROOT = Path(__file__).resolve().parent.parent

README = ROOT / "README.md"
AUTHORED = [
    README,
    ROOT / "docs" / "USER-GUIDE.md",
    ROOT / "docs" / "SPEC.md",
    ROOT / "docs" / "TOOLS.md",
    ROOT / "docs" / "RUNTIME-EVIDENCE.md",
    ROOT / "docs" / "WINDOWS-ACCEPTANCE.md",
    ROOT / "demo" / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
]
EVIDENCE_INDEXES = sorted((ROOT / "docs" / "evidence").glob("*/README.md"))

EXAMPLE_BLOCK = re.compile(r"<!-- example:[a-z0-9-]+ -->\n```(?:json|text)\n.*?\n```", re.S)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

INTERNAL = {
    "an internal agent name": re.compile(r"\b(dante|virgil|wren)\b", re.I),
    "a board or process term": re.compile(r"\b(kanban|dogfood|ledger)\b|review gate", re.I),
    "a card id": re.compile(r"\bt_[0-9a-f]{8}\b"),
}
# A review-round sequence ("first review", "first fix round") is process detail a
# public reader cannot use; the label should say what was run or changed instead.
REVIEW_ROUND = re.compile(
    r"\b(?:first|second|third|fourth|final|next|last|earlier)[- ](?:review|fix)[- ](?:round|pass|head)\b"
    r"|\b(?:first|second|third|fourth)[- ]review\b"
    r"|\breview[- ]round\b"
    r"|\bfix[- ]round\b"
    r"|\breview (?:number )?(?:one|two|three|four|1|2|3|4)\b",
    re.I,
)
# A decision states the rule the product follows. A sentence about one person's own
# machine — what they use, what they chose, where their library lives — is deployment
# detail, and it is not a rule wherever in the row it appears.
PERSONAL_SETUP = re.compile(
    r"\b(?:does not use|do not use|i use|we use|my own|our own|in my setup|in our setup"
    r"|(?:we|i) (?:chose|choose|picked|pick|decided|prefer)"
    r"|(?:my|our) (?:deployment|setup|machine|vault))\b",
    re.I,
)
# A deployment choice made by one person ("chose a vault folder") describes their own
# setup, which is not a source a public decision can cite.
PERSONAL_CHOICE = re.compile(
    r"\b(?:chose|chooses|chosen|picked|picks|selected|selects|decided on)\b"
    r"|\b(?:my|our|his|her|their) own\b",
    re.I,
)
DECISION_ROW = re.compile(r"^\|\s*(D\d+)\s*\|(.*)\|(.*)\|\s*$")
PERSON = re.compile(r"\bChris\b")
ATTRIBUTION = re.compile(r"^Chris, \d{4}-\d{2}-\d{2}$")
# The quick start's automatic-binding sentence, clause for clause as SPEC 6.4 gives it:
# exactly one matching project, an active one, a local folder, one that exists here.
BINDING_RULE = (
    "A session binds automatically only when exactly one active project lists a "
    "local folder that exists on this machine and that folder contains the "
    "session's working directory"
)


def authored_text(path):
    text = path.read_text(encoding="utf-8")
    return EXAMPLE_BLOCK.sub("", text) if path == README else text


def readme_line(prefix):
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"README has no line starting {prefix!r}")


def quick_start():
    text = README.read_text(encoding="utf-8")
    return text[text.index("## Quick start") : text.index("## Requirements")]


def offenders(files, pattern):
    if isinstance(pattern, dict):
        found = []
        for label, single in pattern.items():
            found.extend(f"{where}: {label}" for where in offenders(files, single))
        return found
    found = []
    for path in files:
        if not path.exists():
            continue
        text = authored_text(path)
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    return found


def test_readme_opens_with_a_quick_start_that_leads_somewhere():
    text = README.read_text(encoding="utf-8")
    assert "## Quick start" in text
    assert text.index("## Quick start") < text.index("## Requirements")
    quick = quick_start()
    assert "hermes plugins install <git-url> --enable" in quick
    assert "Add a client:" in quick
    assert "docs/USER-GUIDE.md" in quick
    assert "demo/README.md" in quick


def test_quick_start_states_what_binding_needs_and_what_the_agent_gets():
    quick = quick_start()
    # The binding sentence is the rule the code applies, clause for clause (SPEC 6.4).
    assert BINDING_RULE in quick, "the quick start must state every binding prerequisite"
    assert "`locations`" in quick, "the quick start must name a project's locations"
    assert "reference, not a binding route" in quick, "the client's own folder must not bind"
    # The hook sends the banner, not the record body: the agent is told which client the
    # session is on and reads the record itself when it needs the detail.
    assert "engagements_context" in quick
    assert "already in hand" not in quick, "the quick start promised the record without a read"
    assert "the agent starts with the record" not in quick
    # Naming a client in a message is a request to the model, not a plugin action; the
    # plugin binds through its tool or the explicit command, which names its argument.
    assert "instructs the agent" in quick, "the message route must read as an instruction"
    assert "`/client <name-or-id>`" in quick, "the explicit command must name its argument"
    assert "Naming the client in a message or with `/client` binds" not in quick


def test_quick_start_binding_requires_an_active_project():
    # An inactive, paused, former or historical project matches nothing (SPEC 6.4, step 3).
    quick = quick_start()
    assert "exactly one active project" in quick, quick
    assert re.search(r"\b(?:inactive|paused|former|historical)\s+project", quick) is None, quick


def test_quick_start_binding_requires_a_local_folder():
    # Only a local path can name a folder on this machine; a URL or a share cannot.
    quick = quick_start()
    assert "lists a local folder" in quick, quick
    assert re.search(r"\b(?:remote|network|unc|cloud|shared)\s+folder", quick, re.I) is None, quick


def test_quick_start_binding_requires_the_folder_to_exist_here():
    quick = quick_start()
    assert (
        "exists on this machine and that folder contains the session's working directory" in quick
    ), quick
    assert re.search(r"\b(?:need not|may not|does not|might not)\s+exist", quick) is None, quick


def test_quick_start_binding_advice_is_the_rule_the_code_applies():
    # The document above is only right while the binding rule still says the same thing.
    home = "/home/ann"
    cwd = "/home/ann/clients/harbor"
    everywhere = lambda _path: True  # noqa: E731 - an injectable filesystem probe
    keep = lambda path: path  # noqa: E731 - an injectable realpath
    project = {
        "id": "harbor-signage",
        "kind": "project",
        "client": "harbor-print-shop",
        "name": "Harbor Signage",
        "status": "active",
        "locations": [cwd],
        "valid": True,
    }
    options = {"home": home, "os_name": "posix", "exists": everywhere, "realpath": keep}
    assert match_projects(cwd, [], surface="cli", **options).state == "unbound"
    bound = match_projects(cwd, [project], surface="cli", **options)
    assert (bound.state, bound.client) == ("bound", "harbor-print-shop")
    # Only the surfaces that carry a working directory take part.
    assert match_projects(cwd, [project], surface="local", **options).state == "cwd_unsupported"
    # Each prerequisite the quick start states is one the resolver applies.
    inactive = dict(project, status="historical")
    assert match_projects(cwd, [inactive], surface="cli", **options).state == "unbound"
    remote = dict(project, locations=["//server/share/harbor"])
    assert match_projects(cwd, [remote], surface="cli", **options).state == "unbound"
    absent = dict(project, locations=["/home/ann/clients/gone"])
    assert (
        match_projects(cwd, [absent], surface="cli", home=home, os_name="posix", realpath=keep,
                       exists=lambda path: path == cwd).state == "unbound"
    )
    # Two projects of the same client sharing the folder are ambiguous, never a binding.
    twin = dict(project, id="harbor-web", name="Harbor Web")
    ambiguous = match_projects(cwd, [project, twin], surface="cli", **options)
    assert (ambiguous.state, ambiguous.reason) == ("ambiguous", "ambiguous")


def test_docs_carry_no_internal_board_or_agent_vocabulary():
    assert offenders(AUTHORED + EVIDENCE_INDEXES, INTERNAL) == []


def test_docs_describe_runs_without_review_round_numbering():
    assert offenders(AUTHORED + EVIDENCE_INDEXES, REVIEW_ROUND) == []


def decision_rows(spec_text):
    """``(decision id, whole row, source cell)`` for each row of the spec's decision table."""
    rows = []
    for line in spec_text.splitlines():
        match = DECISION_ROW.match(line)
        if match:
            rows.append((match.group(1), line, match.group(3).strip()))
    return rows


def personal_setup_in_decision_rows(spec_text):
    """Decision rows that state someone's own setup or an internal process, in any cell.

    The whole row is checked, not only the Source column: a deployment sentence is
    just as much one person's setup when it sits in the Decision text, and a row
    that names board or review process detail is no more usable a decision.
    """
    return [
        f"{decision}: {row}"
        for decision, row, _source in decision_rows(spec_text)
        if PERSONAL_SETUP.search(row)
        or any(pattern.search(row) for pattern in INTERNAL.values())
        or REVIEW_ROUND.search(row)
    ]


def test_spec_decisions_state_the_rule_not_a_personal_setup():
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    rows = decision_rows(spec)
    assert len(rows) >= 10, "the decision table moved; this check needs updating"
    # Every cell of a row states the rule the product follows, so a personal-setup or
    # internal sentence restored to the Decision text fails exactly as it does in the
    # Source column.
    assert personal_setup_in_decision_rows(spec) == []
    # A source says where a decision came from: an attribution, a named requirement or a
    # rationale. A choice made for one person's machine is not one.
    assert [
        f"{decision}: {source}"
        for decision, _row, source in rows
        if PERSONAL_CHOICE.search(source)
    ] == []
    # Where the source names a person it is an attribution: the name and a date, nothing else.
    assert [
        f"{decision}: {source}"
        for decision, _row, source in rows
        if PERSON.search(source) and not ATTRIBUTION.match(source)
    ] == []


def test_decision_guard_rejects_a_setup_sentence_restored_to_a_decision_cell():
    # The row guard only earns its place while it rejects the class it replaced a
    # Source-only check for: a first-person deployment clause put back into a Decision
    # cell, with the public rule and the attribution left as they are.
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines() if line.startswith("| D10 |"))
    anchor = "app-specific syntax."
    assert anchor in row, "the decision row moved; this check needs updating"
    restored = row.replace(
        anchor, anchor + " In our setup the library lives inside a vault.", 1
    )
    offenders = personal_setup_in_decision_rows(spec.replace(row, restored))
    assert [offender for offender in offenders if offender.startswith("D10: ")] == [
        f"D10: {restored}"
    ], offenders


def test_onboarding_behaviour_is_written_as_instructions():
    guide = (ROOT / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")
    assert "Engagements asks the agent to do these things" in guide
    assert "The agent then:" not in guide
    # The corrected sentence says what the model is told; the former wording stated the
    # outcome as a guarantee.
    unconfirmed = [line for line in guide.splitlines() if "treat it as fact" in line]
    assert len(unconfirmed) == 1, unconfirmed
    assert "is told" in unconfirmed[0], unconfirmed[0]
    assert "The agent will" not in guide
    assert "The agent does not" not in guide
    readme = README.read_text(encoding="utf-8")
    assert "instructs the model to ask at most three follow-ups" in readme
    assert "instructions to the model, not guarantees" in readme


def test_desktop_claims_name_the_surface_that_was_run():
    evidence = (ROOT / "docs" / "RUNTIME-EVIDENCE.md").read_text(encoding="utf-8").lower()
    # The claims below are only honest while the evidence still records the gaps.
    assert "the empty-page starter prompts" in evidence
    assert "not runtime-exercised" in evidence
    assert "the fresh-chat state was not observed" in evidence
    starter = readme_line("- **Starter prompts on an empty page.**")
    assert "front-end tests" in starter
    assert "neither was clicked through" in starter, starter
    chat = readme_line("- **Start a chat** (Desktop)")
    assert "front-end tests" in chat
    assert "was not" in chat, chat
    desktop = readme_line("| Desktop |")
    assert "front-end tests only" in desktop, desktop


def test_spike_is_presented_as_history_not_as_a_starting_point():
    about_spike = readme_line("**About `spike/`.**")
    assert "historical evidence" in about_spike
    assert "not part of the plugin" in about_spike
    assert "does not load it" in about_spike, about_spike
    # A checkout install clones the tracked folder, so it is not excluded from installation.
    assert "not installed" not in about_spike
    evidence = (ROOT / "docs" / "RUNTIME-EVIDENCE.md").read_text(encoding="utf-8")
    assert "historical harness" in evidence


def test_changelog_lists_every_bundled_skill():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    skills = sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md"))
    assert skills, "no bundled skills found; this check needs updating"
    assert [name for name in skills if f"engagements:{name}" not in changelog] == []
    # Any count the changelog does state has to be the number that ships.
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    stated = re.findall(r"\b(one|two|three|four|five|\d+) skills\b", changelog, re.I)
    assert [
        w for w in stated if (int(w) if w.isdigit() else words[w.lower()]) != len(skills)
    ] == []


def test_guard_covers_every_authored_document():
    # A document the guards skip is a document a reversal can hide in, so the inventory is
    # checked against the files a reader can land on rather than trusted as a list.
    published = [
        README,
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        ROOT / "CHANGELOG.md",
        *(ROOT / "docs").glob("*.md"),
        ROOT / "demo" / "README.md",
    ]
    assert [str(path.relative_to(ROOT)) for path in published if path not in AUTHORED] == []


def test_contributor_instructions_do_not_need_a_file_this_repository_lacks():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    # A command naming a configuration outside the checkout cannot be reproduced by a
    # contributor who cloned this repository.
    assert "ruff.toml" not in contributing
    assert "<workspace>" not in contributing
    assert "README's Development section" in contributing
    assert "## Development" in README.read_text(encoding="utf-8")
    # The test command it does give is the one this repository runs.
    assert "python -m pytest -q -p no:cacheprovider" in contributing


def test_project_files_say_how_to_report_and_contribute():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "fictional data only" in contributing.lower()
    assert "--python 3.14" in contributing and "--python 3.11" in contributing
    assert "pyyaml>=6,<7" in contributing
    assert "private vulnerability reporting" in security.lower()
    assert "not an isolation boundary" in security.lower()
    assert "local-only" in security.lower()
    assert EMAIL.search(security) is None, "the security policy must not publish an email address"
    # The operative instruction, not just any surviving private-reporting phrase: a policy
    # telling readers to open a public issue must not pass on leftover wording.
    assert "## Report a vulnerability privately" in security
    reporting = security.split("## Report a vulnerability privately", 1)[1].split(
        "## What is in scope", 1
    )[0]
    assert "Use GitHub's private vulnerability reporting" in reporting
    assert "do not open a public issue" in reporting
    assert "1.0.0-rc1" in changelog
    # The release states the requirements and the no-network behaviour it ships with.
    assert "v0.22.3" in changelog and "3.11" in changelog and "PyYAML 6" in changelog
    assert "no network requests" in changelog
    assert "sends no telemetry" in changelog
