"""register(ctx) and plugin.yaml: what the plugin tells Hermes it provides, and nothing more."""

import ast
import shutil
from pathlib import Path

import pytest
import yaml
from plugin_support import (
    ROOT,
    FakeCtx,
    install_plugin_storage,
    load_plugin,
    registered,
)

TOOLS = ("engagements_find", "engagements_bind", "engagements_context", "engagements_create", "engagements_update")


@pytest.fixture
def manifest():
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def test_manifest_is_v2_and_declares_what_register_registers(monkeypatch, tmp_path, manifest):
    _, ctx = registered(monkeypatch, tmp_path)
    assert manifest["name"] == "engagements"
    assert manifest["manifest_version"] == 2
    assert manifest["python_dependencies"] == ["pyyaml>=6,<7"]
    assert tuple(manifest["provides_tools"]) == TOOLS
    assert set(manifest["provides_tools"]) == set(ctx.tools)
    assert set(manifest["provides_hooks"]) == set(ctx.hooks)


def test_manifest_settings_cover_spec_6_1(manifest):
    schema = manifest["config_schema"]
    assert set(schema) == {"library_root", "labels", "auto_bind", "banner"}
    assert schema["library_root"]["default"] == ""
    assert schema["labels"]["default"] == {"client": "Client", "project": "Project"}
    assert schema["auto_bind"] == {**schema["auto_bind"], "type": "bool", "default": True}
    assert schema["banner"] == {**schema["banner"], "type": "bool", "default": True}


def test_every_tool_is_registered_with_the_core_schema_and_one_toolset(monkeypatch, tmp_path):
    plugin, ctx = registered(monkeypatch, tmp_path)
    from_core = plugin.TOOL_SCHEMAS
    assert tuple(ctx.tools) == TOOLS
    for name, entry in ctx.tools.items():
        assert entry["toolset"] == "engagements"
        assert entry["schema"] == from_core[name]
        assert callable(entry["handler"])


def test_tool_descriptions_lead_with_search_words_within_sixty_characters(monkeypatch, tmp_path):
    # spec 6.2 / A3: the first sentence is what Tool Search shows the model.
    _, ctx = registered(monkeypatch, tmp_path)
    for entry in ctx.tools.values():
        first = entry["schema"]["description"].split(". ")[0]
        assert len(first) <= 60
        assert any(word in first.lower() for word in ("client", "project", "engagement"))


def test_hooks_commands_and_cli_are_registered(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    assert set(ctx.hooks) == {"pre_llm_call", "post_compaction", "on_session_reset", "on_session_delete"}
    assert set(ctx.commands) == {"client", "engagements"}
    assert set(ctx.cli) == {"engagements"}
    assert ctx.cli["engagements"]["handler_fn"] is not None
    assert all(entry["description"] for entry in ctx.commands.values())


SKILL_NAMES = ("onboard-client", "client-work", "suggest-clients")


def _skill_text(name):
    return (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_both_bundled_skills_register_with_a_description(monkeypatch, tmp_path):
    # A8: without an explicit description a plugin skill is listed with none (Phase 0, S7).
    _, ctx = registered(monkeypatch, tmp_path)
    assert set(ctx.skills) == set(SKILL_NAMES)
    for name, entry in ctx.skills.items():
        assert entry["path"] == ROOT / "skills" / name / "SKILL.md"
        assert entry["path"].is_file()
        assert entry["description"].strip()


def test_skill_frontmatter_matches_the_registered_name_and_description(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    for name in SKILL_NAMES:
        block = _skill_text(name).split("---\n")[1]
        meta = yaml.safe_load(block)
        assert meta["name"] == name
        assert meta["description"] == ctx.skills[name]["description"]


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skills_say_how_to_reach_deferred_tools(name):
    # A3: stock Hermes hides plugin tools behind Tool Search, so the skill must say how to reach them.
    text = _skill_text(name)
    assert "tool_search" in text and "tool_call" in text


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("onboard-client", ("engagements_find", "engagements_create", "Unconfirmed", "at most three", "allow_similar")),
        (
            "client-work",
            ("engagements_bind", "engagements_context", "engagements_update", "ambiguous", "invalid_binding",
             "partial", "replace_section", "conflict", "fresh session"),
        ),
        # What the suggest skill must never lose (decision D12). Each phrase is a rule of that skill.
        (
            "suggest-clients",
            ("session_search", "past chats", "Do not crawl", "engagements_find", "guesses from old conversations",
             "API keys", "Create nothing until the user picks", "onboard-client", "plain onboarding"),
        ),
    ],
)
def test_skills_carry_the_spec_6_5_rules(name, required):
    text = _skill_text(name)
    for needle in required:
        assert needle in text, needle


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skills_only_name_real_tools(name):
    import re

    named = set(re.findall(r"\bengagements_[a-z_]+\b", _skill_text(name)))
    assert named <= set(TOOLS), named - set(TOOLS)


def test_skills_are_registered_with_a_description_when_the_files_exist(monkeypatch, tmp_path):
    copy = tmp_path / "plugin"
    shutil.copytree(ROOT / "engagements", copy / "engagements", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "__init__.py", copy / "__init__.py")
    for name in SKILL_NAMES:
        (copy / "skills" / name).mkdir(parents=True)
        (copy / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody\n", encoding="utf-8")
    install_plugin_storage(monkeypatch, tmp_path / "home")
    plugin = load_plugin("hermes_plugins_engagements_with_skills", copy)
    ctx = FakeCtx()
    plugin.register(ctx)
    assert set(ctx.skills) == set(SKILL_NAMES)  # A8: names are bare; Hermes prefixes engagements:
    assert all(entry["description"] for entry in ctx.skills.values())
    assert all(entry["path"].is_file() for entry in ctx.skills.values())


def test_the_starter_prompts_name_the_skill_the_host_lists():
    """``ctx.register_skill`` qualifies a plugin skill as ``<plugin name>:<name>`` and
    ``_plugin_skill_prompt_rows`` puts that exact string in ``<available_skills>``, which is what
    ``skill_view`` resolves. Both front ends must therefore name ``engagements:suggest-clients``."""
    for path in (ROOT / "desktop" / "plugin.js", ROOT / "dashboard" / "dist" / "index.js"):
        source = path.read_text(encoding="utf-8")
        assert "engagements:suggest-clients" in source, path.name
        assert "engagements:onboard-client" in source, path.name


def test_register_twice_keeps_no_state_between_contexts(monkeypatch, tmp_path):
    # A1: nothing process-global. Two contexts register independently and never see each other's state.
    plugin, first = registered(monkeypatch, tmp_path)
    second = FakeCtx(profile="studio-ops")
    plugin.register(second)
    first.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert second.state.values == {} and second.state.writes == []


def test_core_modules_never_import_hermes():
    banned = {"hermes", "hermes_cli", "hermes_constants", "agent", "gateway", "plugins", "tools"}
    for path in sorted((ROOT / "engagements").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            assert not [n for n in names if n.split(".")[0] in banned], (path.name, names)


def test_the_root_module_imports_only_one_hermes_module_lazily():
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    top_level = {node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))}
    for node in top_level:
        module = getattr(node, "module", None) or ""
        assert getattr(node, "level", 0) > 0 or module.split(".")[0] in {"__future__", "datetime", "pathlib"}, module
    lazy = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0
            and (n.module or "").startswith("plugins")]
    assert [n.module for n in lazy] == ["plugins.plugin_storage"]  # public plugin API only (D9)


def test_the_spike_is_not_imported_or_shipped(monkeypatch, tmp_path):
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "spike" not in source and "engprobe" not in source
    assert Path(ROOT / "spike").is_dir()  # kept as Phase 0 evidence; the plugin never loads from it
