"""The five tool handlers as registered on the fake ctx: session, settings, library and envelope behavior."""

import json

import pytest
from plugin_support import FakeCtx, install_plugin_storage, project_home, registered
from tools_support import binding_key_for, copy_library, library_id


def default_library(tmp_path):
    return tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library"


# --- zero-config first run (spec 6.1, D10, A7) -------------------------------------------------


def test_first_call_creates_the_default_library_under_plugin_data_dir(monkeypatch, tmp_path):
    plugin, ctx = registered(monkeypatch, tmp_path)
    result = ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    root = default_library(tmp_path)
    assert result["ok"] is True
    assert [w["code"] for w in result["warnings"]] == ["library_created"]
    assert (root / ".engagements" / "library.yaml").is_file()
    again = ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert again["warnings"] == []  # created once


def test_the_default_root_is_asked_for_by_the_spec_name(monkeypatch, tmp_path):
    from plugin_support import load_plugin

    calls = install_plugin_storage(monkeypatch, tmp_path / "h")
    ctx = FakeCtx()
    load_plugin().register(ctx)
    ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert calls == ["engagements"]


def test_onboarding_works_in_chat_with_no_setting_at_all(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    created = ctx.call_tool("engagements_create", {"kind": "client", "name": "Acme Bakery"}, session_id="s1")
    assert created["ok"] is True and created["data"]["id"] == "acme-bakery"
    assert created["warnings"][0]["code"] == "library_created"
    found = ctx.call_tool("engagements_find", {"query": "acme"}, session_id="s1")
    assert [r["id"] for r in found["data"]["results"]] == ["acme-bakery"]
    bound = ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    assert bound["ok"] is True and bound["data"]["client"]["name"] == "Acme Bakery"
    context = ctx.call_tool("engagements_context", {}, session_id="s1")
    assert context["ok"] is True and context["data"]["id"] == "acme-bakery"


def test_a_configured_root_is_never_created_implicitly(monkeypatch, tmp_path):
    missing = tmp_path / "absent"
    _, ctx = registered(monkeypatch, tmp_path, library=missing)
    result = ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert result["ok"] is False and result["error"]["code"] == "library_missing"
    assert not missing.exists()
    assert not default_library(tmp_path).exists()


# --- settings precedence (spec 6.1) ---------------------------------------------------------------


def test_a_configured_library_root_beats_the_default(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    result = ctx.call_tool("engagements_find", {"query": "riverbend", "kind": "client"}, session_id="s1")
    assert [r["id"] for r in result["data"]["results"]] == ["riverbend-dental"]
    assert not default_library(tmp_path).exists()


def test_a_blank_library_root_means_the_default(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path, settings={"library_root": "   "})
    assert ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")["ok"] is True
    assert default_library(tmp_path).is_dir()


def test_a_tilde_library_root_expands_against_home(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    copy_library(home, "studio")  # becomes <home>/library
    _, ctx = registered(monkeypatch, tmp_path, settings={"library_root": "~/library"})
    result = ctx.call_tool("engagements_find", {"query": "acme"}, session_id="s1")
    assert result["ok"] is True and result["data"]["results"]


@pytest.mark.parametrize("value", ["relative/path", "./library", 42, ["x"]])
def test_an_unusable_library_root_is_library_missing_and_touches_nothing(monkeypatch, tmp_path, value):
    # SPEC-QUESTION: a relative root would resolve against the shared gateway's cwd (A1), so it is refused.
    monkeypatch.chdir(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, settings={"library_root": value})
    result = ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert result["ok"] is False and result["error"]["code"] == "library_missing"
    assert not (tmp_path / "relative").exists() and not default_library(tmp_path).exists()


def test_the_auto_bind_setting_is_honored_and_accepts_boolean_words(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    cwd = str(home / "clients" / "riverbend" / "recall")
    for setting, expected in ((True, "bound"), ("true", "bound"), (False, "unbound"), ("off", "unbound"),
                              ("garbage", "bound")):
        _, ctx = registered(monkeypatch, tmp_path, library=root, cwd=cwd, settings={"auto_bind": setting})
        result = ctx.call_tool("engagements_bind", {"mode": "auto"}, session_id=f"s-{setting}")
        assert result["ok"] is True, result
        assert result["data"]["state"] == expected, (setting, result["data"])


def test_the_banner_setting_turns_the_hook_off(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    for setting, expected in ((True, True), ("no", False), (False, False)):
        _, ctx = registered(monkeypatch, tmp_path, library=root, settings={"banner": setting})
        ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id=f"s-{setting}")
        banner = ctx.hook("pre_llm_call", session_id=f"s-{setting}")
        assert (banner is not None) is expected


# --- session identity from kwargs only (spec A1) -------------------------------------------------


def test_the_binding_is_one_key_per_session_in_plugin_state(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="sess-A", task_id="sess-A")
    ctx.call_tool("engagements_bind", {"client": "riverbend-dental"}, session_id="sess-B", task_id="sess-B")
    assert set(ctx.state.values) == {binding_key_for(root, "sess-A"), binding_key_for(root, "sess-B")}
    assert ctx.state.values[binding_key_for(root, "sess-A")]["client"] == "acme-bakery"
    assert ctx.state.values[binding_key_for(root, "sess-B")]["client"] == "riverbend-dental"
    assert ctx.call_tool("engagements_context", {}, session_id="sess-A")["data"]["id"] == "acme-bakery"
    assert ctx.call_tool("engagements_context", {}, session_id="sess-B")["data"]["id"] == "riverbend-dental"


def test_two_contexts_on_one_library_never_cross_sessions(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, first = registered(monkeypatch, tmp_path, library=root)
    _, second = registered(monkeypatch, tmp_path, library=root)
    first.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="same-id")
    assert second.call_tool("engagements_context", {}, session_id="same-id")["error"]["code"] == "unbound"


def test_a_call_without_a_session_id_is_no_session_for_bind_and_stateless_for_ids(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    bound = ctx.call_tool("engagements_bind", {"client": "acme-bakery"})
    assert bound["ok"] is False and bound["error"]["code"] == "no_session"
    by_id = ctx.call_tool("engagements_context", {"id": "acme-bakery"})
    assert by_id["ok"] is True and by_id["data"]["id"] == "acme-bakery"
    assert ctx.state.values == {}


def test_the_task_id_is_not_a_session_id(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    result = ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, task_id="task-only")
    assert result["error"]["code"] == "no_session"


def test_an_explicit_binding_is_stamped_with_an_aware_clock(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    stamp = ctx.state.values[binding_key_for(root, "s1")]["at"]
    from datetime import datetime

    assert datetime.fromisoformat(stamp).utcoffset() is not None


@pytest.mark.parametrize("session_id", ["x" * 120, "has space", "dots..here", "slash/in/id"])
def test_a_session_id_hermes_would_refuse_as_a_state_key_still_binds(monkeypatch, tmp_path, session_id):
    # SPEC-QUESTION: spec A10's key can exceed Hermes' 128-character key rule; it maps to a stable digest.
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    bound = ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id=session_id)
    assert bound["ok"] is True, bound
    assert ctx.call_tool("engagements_context", {}, session_id=session_id)["data"]["id"] == "acme-bakery"
    assert ctx.call_tool("engagements_context", {}, session_id=session_id + "-other")["error"]["code"] == "unbound"


# --- surface gating (spec 6.4) -----------------------------------------------------------------


@pytest.mark.parametrize("surface", ["telegram", "discord", "local", "acp", ""])
def test_cwd_is_unsupported_off_the_cli_and_tui_but_explicit_binding_works(monkeypatch, tmp_path, surface):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    cwd = str(home / "clients" / "riverbend" / "recall")
    _, ctx = registered(monkeypatch, tmp_path, library=root, cwd=cwd, surface=surface)
    unbound = ctx.call_tool("engagements_context", {}, session_id="s1")
    assert unbound["ok"] is False
    assert unbound["error"]["details"]["auto"]["reason"] == "cwd_unsupported"
    explicit = ctx.call_tool("engagements_bind", {"client": "riverbend-dental"}, session_id="s1")
    assert explicit["ok"] is True
    assert ctx.call_tool("engagements_context", {}, session_id="s1")["data"]["id"] == "riverbend-dental"


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_cwd_auto_binds_on_the_cli_and_tui(monkeypatch, tmp_path, surface):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    cwd = str(home / "clients" / "riverbend" / "recall")
    _, ctx = registered(monkeypatch, tmp_path, library=root, cwd=cwd, surface=surface)
    context = ctx.call_tool("engagements_context", {"kind": "project"}, session_id="s1")
    assert context["ok"] is True and context["data"]["id"] == "riverbend-recall-campaign"


def test_the_shared_folder_is_ambiguous_and_the_parent_is_unbound(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root, cwd=str(home / "clients" / "shared"))
    shared = ctx.call_tool("engagements_context", {}, session_id="s1")
    assert shared["error"]["details"]["auto"]["state"] == "ambiguous"
    ctx.cwd = str(home / "clients")
    parent = ctx.call_tool("engagements_context", {}, session_id="s2")
    assert parent["error"]["details"]["auto"]["state"] == "unbound"


def test_the_session_cwd_comes_from_ctx_not_the_process(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    root = copy_library(tmp_path)
    monkeypatch.chdir(home / "clients" / "acme" / "website-2026")  # the shared gateway's own cwd must not matter
    _, ctx = registered(monkeypatch, tmp_path, library=root, cwd=str(home / "clients"))
    assert ctx.call_tool("engagements_context", {}, session_id="s1")["error"]["details"]["auto"]["state"] == "unbound"


# --- the envelope, errors, and no traceback -----------------------------------------------------


def test_every_result_is_a_json_string_envelope(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    raw = ctx.tools["engagements_find"]["handler"]({"query": "acme"}, session_id="s1")
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert set(parsed) == {"ok", "data", "warnings"}


@pytest.mark.parametrize("args", [None, [], "text", 3, {"query": 5}, {"nope": 1}, {}])
def test_bad_arguments_are_invalid_op_envelopes_not_exceptions(monkeypatch, tmp_path, args):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    raw = ctx.tools["engagements_find"]["handler"](args, session_id="s1")
    parsed = json.loads(raw)
    assert parsed["ok"] is False and parsed["error"]["code"] == "invalid_op"
    assert "Traceback" not in raw


def test_a_failing_state_store_is_an_io_error_envelope_without_a_traceback(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    ctx.state.fail = RuntimeError("Cannot parse plugin state /home/secret/state.json: boom")
    raw = ctx.tools["engagements_bind"]["handler"]({"client": "acme-bakery"}, session_id="s1")
    parsed = json.loads(raw)
    assert parsed["ok"] is False and parsed["error"]["code"] == "io_error"
    assert "Traceback" not in raw and "secret" not in raw and "boom" not in raw


def test_an_unexpected_exception_becomes_the_generic_io_error(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root)

    def boom(*args, **kwargs):
        raise ZeroDivisionError("secret detail")

    monkeypatch.setattr(plugin.runtime, "call_tool", boom)
    raw = ctx.tools["engagements_find"]["handler"]({"query": "x"}, session_id="s1")
    parsed = json.loads(raw)
    assert parsed == {"ok": False, "error": {"code": "io_error", "message": "Filesystem operation failed.",
                                             "details": {}}, "warnings": []}
    assert "secret" not in raw


def test_an_unreadable_library_setting_fails_closed(monkeypatch, tmp_path):
    # An unreadable setting is not an absent one. It must not choose the
    # default location, because the first create would then write into a library no one configured.
    plugin, _ = registered(monkeypatch, tmp_path)
    ctx = FakeCtx()

    def broken(key, default=None):
        raise ValueError("config unreadable")

    ctx.get_config = broken
    plugin.register(ctx)
    result = ctx.call_tool("engagements_find", {"query": ""}, session_id="s1")
    assert result["ok"] is False and result["error"]["code"] == "library_missing"
    assert result["error"]["details"]["reason"] == "no_library_root"
    assert not (tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library").exists()


def test_a_conflict_still_shows_both_versions_through_the_wrapper(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    first = ctx.call_tool("engagements_context", {"id": "acme-bakery"}, session_id="s1")
    rev = first["data"]["rev"]
    record = next(root.rglob("Acme Bakery/Client.md"))
    record.write_text(record.read_text(encoding="utf-8").replace("Weekly status by email on Fridays",
                                                                  "Status by phone"), encoding="utf-8")
    ops = [{"op": "replace_section", "section": "Communication preferences", "body": "- Slack only"}]
    result = ctx.call_tool("engagements_update", {"id": "acme-bakery", "base_rev": rev, "source": "test",
                                                   "ops": ops}, session_id="s1")
    assert result["ok"] is False and result["error"]["code"] == "conflict"
    texts = json.dumps(result["error"]["details"])
    assert "Status by phone" in texts and "Weekly status by email" in texts


def test_library_id_is_part_of_the_key(monkeypatch, tmp_path):
    root = copy_library(tmp_path)
    _, ctx = registered(monkeypatch, tmp_path, library=root)
    ctx.call_tool("engagements_bind", {"client": "acme-bakery"}, session_id="s1")
    assert f"binding:{library_id(root)}:s1" in ctx.state.values
