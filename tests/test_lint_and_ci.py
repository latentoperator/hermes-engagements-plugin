"""The checkout and its CI say what they do: a clone lints itself, and a provisioned runtime is demanded.

A stranger who clones the repository gets the lint rules from ``pyproject.toml`` rather than from a
directory above it. The workflow both runs those rules and installs the runtimes the suite needs,
so a job that is supposed to exercise Node or an installed Hermes host fails when either is missing
instead of passing with those tests skipped. These tests guard both halves of that claim.
"""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"
# The override that keeps another configuration file in step with this repository's import block.
OVERRIDE = "--config 'src = []'"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
VERSION = re.compile(r"#\s*v\d+\.\d+\.\d+$")


def project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps_of(job):
    return job.get("steps", [])


def every_step(document):
    return [step for job in document["jobs"].values() for step in steps_of(job)]


def test_the_repository_carries_its_own_ruff_configuration():
    """Nothing above a clone configures lint, so the rules have to travel with the repository."""
    ruff = project()["tool"]["ruff"]
    assert ruff["target-version"] == "py311"
    assert ruff["line-length"] == 88
    # First-party detection is deliberately off: this tree sorts its own packages among
    # third-party ones, so detecting them would ask for insertions in dozens of files.
    assert ruff["src"] == []
    assert "known-first-party" not in (ruff["lint"].get("isort") or {})
    assert {"E", "F", "W", "I", "B", "UP", "C4", "SIM"} <= set(ruff["lint"]["select"])
    assert set(ruff["lint"]["ignore"]) == {"E501", "B008", "SIM108"}


def test_a_job_lints_the_checkout_with_its_own_configuration():
    """A job lints the checkout with no configuration but the one the repository carries."""
    commands = [step.get("run", "").strip() for step in every_step(workflow()) if "ruff check" in step.get("run", "")]
    assert commands, "no job lints the checkout"
    for command in commands:
        words = command.split()
        assert words[:2] == ["uvx", "ruff"], command
        assert not {word for word in words if word.startswith("-")} - {"--no-cache"}, (
            f"the lint job changes how Ruff reads this checkout: {command}"
        )
        assert words[-1] == ".", f"the lint job does not lint the checkout: {command}"


def test_the_documented_lint_command_needs_nothing_outside_the_checkout():
    """The command a reader runs carries no configuration but the one in this repository.

    A configuration file handed to Ruff with ``--config`` replaces the file the repository
    carries, and Ruff then takes the working directory as the project root: ``engagements``
    is read as a first-party package and the single import block this tree keeps is reported
    as unsorted in most files. The README therefore names the bare command, and the override
    that makes another configuration file agree with this one beside it.
    """
    development = README.read_text(encoding="utf-8").split("## Development", 1)[1]
    commands = [
        line.strip() for line in development.splitlines() if line.strip().startswith("uvx ruff")
    ]
    assert commands == ["uvx ruff check ."], commands
    assert OVERRIDE in development, "the README must name the override for another configuration"


def test_every_action_is_pinned_to_a_commit_with_its_version():
    text = WORKFLOW.read_text(encoding="utf-8")
    used = [line.split("uses:", 1)[1].strip() for line in text.splitlines() if "uses:" in line]
    assert used, "the workflow uses no actions at all"
    for reference in used:
        pinned, _, version = reference.partition("#")
        assert PINNED.match(pinned.strip()), reference
        assert VERSION.match("#" + version), reference


def test_the_test_job_provisions_node_and_demands_it():
    job = workflow()["jobs"]["test"]
    assert job["env"]["ENGAGEMENTS_REQUIRE_NODE"] == "1"
    provisioned = [step for step in steps_of(job) if str(step.get("uses", "")).startswith("actions/setup-node@")]
    assert provisioned, "the test job does not provision Node"
    assert provisioned[0]["with"]["node-version"], "Node is provisioned without a version"


def test_the_host_job_runs_against_an_installed_host_and_demands_it():
    job = workflow()["jobs"]["host"]
    assert job["env"]["ENGAGEMENTS_REQUIRE_HERMES_HOST"] == "1"
    runs = [step.get("run", "") for step in steps_of(job)]
    installed = [run for run in runs if "hermes-agent" in run]
    assert installed, "the host job never fetches a host to run against"
    assert any("uv sync" in run or "pip install" in run for run in installed), installed
    assert any("tests/test_dashboard_readonly.py" in run and "/.venv/" in run for run in runs), (
        "the host tests do not run under the installed host's interpreter"
    )


def test_permissions_stay_read_only_and_checkouts_keep_no_credentials():
    document = workflow()
    assert document["permissions"] == {"contents": "read"}
    for step in every_step(document):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


def test_the_uv_cache_is_keyed_on_a_file_in_the_repository():
    """A dependency glob that matches nothing warns on every run and caches nothing."""
    for step in every_step(workflow()):
        if not str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
            continue
        settings = step.get("with", {})
        if settings.get("enable-cache") is False:
            continue
        glob = settings.get("cache-dependency-glob")
        assert glob, "the uv cache is enabled with no dependency file to key on"
        assert not any(char in glob for char in "*?["), glob
        assert (ROOT / glob).exists(), glob
