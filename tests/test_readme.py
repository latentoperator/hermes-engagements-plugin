"""The README is packaging, so its claims must follow the code and the evidence.

These checks keep three things honest:
- every tool named in the README is a real tool, and every real tool is named;
- every example block is byte-for-byte what ``scripts/readme_examples.py`` prints
  from the real handlers against ``tests/fixtures/studio``;
- the honest-limits and disclosure sections stay present, and the manifest
  metadata matches the release candidate.
"""

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
GENERATOR = ROOT / "scripts" / "readme_examples.py"
BLOCK = re.compile(r"<!-- example:([a-z0-9-]+) -->\n```(?:json|text)\n(.*?)\n```", re.S)
TOOL = re.compile(r"\bengagements_[a-z_]+\b")


def readme_text():
    return README.read_text(encoding="utf-8")


def generated_blocks():
    result = subprocess.run(
        [sys.executable, str(GENERATOR)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    blocks = {}
    title = None
    lines = []
    for line in result.stdout.splitlines():
        if line.startswith("===== "):
            if title is not None:
                blocks[title] = "\n".join(lines).rstrip("\n")
            title = line[len("===== "):].strip()
            lines = []
        else:
            lines.append(line)
    if title is not None:
        blocks[title] = "\n".join(lines).rstrip("\n")
    return blocks


def test_readme_names_exactly_the_real_tools():
    from engagements.tools import TOOL_SCHEMAS

    named = set(TOOL.findall(readme_text()))
    assert named == set(TOOL_SCHEMAS), (named - set(TOOL_SCHEMAS), set(TOOL_SCHEMAS) - named)


def test_readme_examples_are_the_generator_output():
    documented = dict(BLOCK.findall(readme_text()))
    generated = generated_blocks()
    assert set(documented) == set(generated), set(generated) ^ set(documented)
    for name, body in documented.items():
        assert body == generated[name], name


def test_readme_keeps_the_honest_limits_and_disclosure():
    text = " ".join(readme_text().lower().split())
    for phrase in (
        "client scoping organizes context. it is not isolation",
        "any process with file access can read the library",
        "agents can edit record files directly with general file tools",
        "a concurrent external-editor save can be silently lost",
        "directory durability on windows is weaker than on posix",
        "windows has no directory fsync",
        "not crash evidence",
        "the binding banner is stored with the conversation",
        "only the surfaces named above are supported claims",
    ):
        assert phrase in text, phrase
    for phrase in ("local files only", "no network", "reads the session's working directory"):
        assert phrase in text, phrase


def test_readme_matches_the_proven_surface_matrix():
    text = readme_text()
    for phrase in (
        "Classic CLI, one-shot",
        "Classic CLI, interactive",
        "TUI",
        "Hermes Desktop",
        "Messaging gateway",
        "unverified",
        "natively in CI on `windows-latest`",
        "v0.22.3",
        "tools.tool_search.enabled: off",
    ):
        assert phrase in text, phrase


def test_readme_keeps_the_coverage_qualifications():
    text = " ".join(readme_text().lower().split())
    for phrase in (
        "covered by unit tests only and has not been re-run on a real cli or tui",
        "only steps 4 (automatic binding and the banner) and 7",
        "optional obsidian compatibility check was not run",
        "instructs the model to ask at most three follow-ups",
        "the model asked a fourth question",
        "rebases onto the newer revision instead",
        "v0.22.3 is the api floor",
    ):
        assert phrase in text, phrase
    assert "a stale revision produces a conflict with both versions" not in text


def test_release_metadata_is_consistent():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "engagements"
    assert manifest["version"] == "1.0.0-rc1"
    assert project["version"] == "1.0.0-rc1"
    assert dashboard["version"] == manifest["version"]
    assert manifest["license"] == "MIT"
    assert manifest["description"].strip()
    assert manifest["python_dependencies"] == ["pyyaml>=6,<7"]
    assert manifest["requires_hermes"] == ">=0.22.3"


def test_license_file_is_mit_for_chris_gray():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 Chris Gray" in text
    assert "Permission is hereby granted, free of charge" in text
