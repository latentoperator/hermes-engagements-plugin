"""pre_llm_call, post_compaction, on_session_reset and on_session_delete as registered on the fake ctx."""

import pytest
from plugin_support import project_home, registered
from tools_support import banner_key_for, binding_key_for, copy_library

TOOLS_NAMED = ("engagements_context", "engagements_update")


def turn(ctx, session_id="s1"):
    """One user turn: the banner text the model would receive, or None."""
    result = ctx.hook("pre_llm_call", session_id=session_id, user_message="hi", conversation_history=[],
                      is_first_turn=False, model="stub", platform=ctx.surface)
    if result is None:
        return None
    assert set(result) == {"context"}
    return result["context"]


def setup(monkeypatch, tmp_path, **kwargs):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root, **kwargs)
    return root, plugin, ctx


def bind(ctx, client="acme-bakery", session_id="s1", **extra):
    result = ctx.call_tool("engagements_bind", {"client": client, **extra}, session_id=session_id)
    assert result["ok"] is True, result
    return result


# --- pre_llm_call: the banner, on state change only (A4) -----------------------------------------


def test_the_banner_is_emitted_on_the_first_bound_turn_and_names_the_tools(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    text = turn(ctx)
    assert text.startswith("[Engagements]") and 'Client: "Acme Bakery" (acme-bakery)' in text
    assert len(text) < 400
    assert all(name in text for name in TOOLS_NAMED)


def test_an_unchanged_binding_is_not_repeated_on_later_turns(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    assert turn(ctx) is not None
    writes = len(ctx.state.writes)
    assert [turn(ctx) for _ in range(5)] == [None] * 5
    assert len(ctx.state.writes) == writes  # silent turns write nothing


def test_a_changed_binding_emits_again_and_says_what_changed(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    turn(ctx)
    bind(ctx, "riverbend-dental")
    text = turn(ctx)
    assert '"Riverbend Dental" (riverbend-dental)' in text and "Binding changed" in text and "acme-bakery" in text
    assert turn(ctx) is None


def test_a_session_that_was_never_bound_gets_no_banner(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    assert [turn(ctx) for _ in range(3)] == [None] * 3
    # An unbound auto session stores no binding. The only key is the turn-verified surface that lets a
    # `local` slash command act on this session (Phase 3D).
    assert ctx.state.values == {"surface:s1": "cli"}


def test_unbinding_retires_the_earlier_banner_once(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    turn(ctx)
    assert ctx.call_tool("engagements_bind", {}, session_id="s1")["ok"] is True  # explicit off
    text = turn(ctx)
    assert "No client is bound now" in text and "mode off" in text and len(text) < 400
    assert turn(ctx) is None


def test_sessions_keep_independent_banner_state(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, "acme-bakery", "A")
    bind(ctx, "riverbend-dental", "B")
    assert "acme-bakery" in turn(ctx, "A") and "riverbend-dental" in turn(ctx, "B")
    assert turn(ctx, "A") is None and turn(ctx, "B") is None
    assert banner_key_for("A") in ctx.state.values and banner_key_for("B") in ctx.state.values


def test_an_automatic_binding_is_announced_once_and_remembered(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    root, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"))
    text = turn(ctx)
    assert 'Project: "Recall Campaign"' in text and "Bound automatically from cwd" in text and "~" in text
    assert ctx.state.values[binding_key_for(root, "s1")] == {
        "mode": "auto", "client": "riverbend-dental", "project": "riverbend-recall-campaign"}
    assert turn(ctx) is None


def test_a_changed_automatic_binding_recommends_a_fresh_session(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"))
    turn(ctx)
    ctx.cwd = str(home / "clients" / "acme" / "website-2026")
    text = turn(ctx)
    assert 'Project: "Website 2026"' in text and "fresh session" in text and len(text) < 400


def test_an_ambiguous_folder_is_announced_once_naming_the_candidates(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "shared"))
    text = turn(ctx)
    assert "ambiguous" in text and "acme-website-2026" in text and "riverbend-brand-assets" in text
    assert "engagements_bind" in text
    assert turn(ctx) is None


def test_an_invalid_binding_is_announced_once(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    turn(ctx)
    record = root / "Acme Bakery" / "Client.md"
    record.write_text(record.read_text(encoding="utf-8").replace("status: active", "status: former", 1),
                      encoding="utf-8")
    text = turn(ctx)
    assert "Binding is invalid" in text and "acme-bakery" in text and "inactive" in text
    assert turn(ctx) is None
    assert ctx.call_tool("engagements_context", {}, session_id="s1")["error"]["details"]["state"] == "invalid_binding"


@pytest.mark.parametrize("surface", ["telegram", "acp"])
def test_off_the_cli_and_tui_an_explicit_binding_is_announced_but_cwd_is_not_used(monkeypatch, tmp_path, surface):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"), surface=surface)
    assert turn(ctx) is None  # cwd_unsupported: nothing bound, nothing to say
    bind(ctx, "acme-bakery")
    assert "acme-bakery" in turn(ctx)


def test_the_banner_never_creates_a_library(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    assert turn(ctx) is None
    assert not (tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library").exists()
    assert ctx.state.values == {}


def test_a_hook_call_without_a_session_id_does_nothing(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    assert ctx.hook("pre_llm_call", session_id="", user_message="x") is None
    assert ctx.hook("pre_llm_call", user_message="x") is None
    assert ctx.state.values == {}


# --- post_compaction (A5) ------------------------------------------------------------------------


def test_compaction_in_place_marks_a_banner_re_emit(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    assert turn(ctx) is not None and turn(ctx) is None
    ctx.hook("post_compaction", session_id="s1", old_session_id="", kind="summarize", reason="manual")
    assert "acme-bakery" in turn(ctx)
    assert turn(ctx) is None


def test_compaction_that_rotates_the_id_migrates_the_binding_and_re_emits(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="old")
    turn(ctx, "old")
    ctx.hook("post_compaction", session_id="new", old_session_id="old", kind="summarize", reason="pre_api")
    assert ctx.state.values[binding_key_for(root, "new")] == ctx.state.values[binding_key_for(root, "old")]
    assert "acme-bakery" in turn(ctx, "new")
    assert turn(ctx, "new") is None


def test_compaction_does_not_overwrite_a_binding_the_new_id_already_has(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, "acme-bakery", "old")
    bind(ctx, "riverbend-dental", "new")
    ctx.hook("post_compaction", session_id="new", old_session_id="old", kind="summarize", reason="manual")
    assert ctx.state.values[binding_key_for(root, "new")]["client"] == "riverbend-dental"


@pytest.mark.parametrize("old", ["", None, "same"])
def test_compaction_without_a_different_old_id_migrates_nothing(monkeypatch, tmp_path, old):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="same")
    before = {k: v for k, v in ctx.state.values.items() if k.startswith("binding:")}
    ctx.hook("post_compaction", session_id="same", old_session_id=old, kind="summarize", reason="manual")
    assert {k: v for k, v in ctx.state.values.items() if k.startswith("binding:")} == before


def test_compaction_uses_kwargs_only_and_survives_an_unreliable_session_cwd(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="old")

    def unreliable():
        raise RuntimeError("no session context here")

    ctx.session_cwd = unreliable
    ctx.session_surface = unreliable
    ctx.hook("post_compaction", session_id="new", old_session_id="old", kind="summarize", reason="manual")
    assert binding_key_for(root, "new") in ctx.state.values


def test_compaction_with_the_banner_off_still_migrates_but_marks_nothing(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path, settings={"banner": False})
    bind(ctx, session_id="old")
    ctx.hook("post_compaction", session_id="new", old_session_id="old", kind="summarize", reason="manual")
    assert binding_key_for(root, "new") in ctx.state.values
    assert banner_key_for("new") not in ctx.state.values


def test_compaction_without_a_library_is_a_no_op(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    ctx.hook("post_compaction", session_id="new", old_session_id="old", kind="summarize", reason="manual")
    assert ctx.state.values == {}


# --- on_session_reset / on_session_delete (A10) -----------------------------------------------


def test_a_reset_session_starts_in_auto_with_no_banner_history(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="fresh")
    turn(ctx, "fresh")
    ctx.hook("on_session_reset", session_id="fresh", platform="cli", reason="new")
    assert ctx.state.values[binding_key_for(root, "fresh")] is None
    assert ctx.state.values[banner_key_for("fresh")] is None
    assert ctx.call_tool("engagements_context", {}, session_id="fresh")["error"]["details"]["mode"] == "auto"
    bind(ctx, session_id="fresh")
    assert turn(ctx, "fresh") is not None  # a banner after reset is a first banner


def test_a_reset_leaves_other_sessions_and_unset_keys_alone(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="other")
    ctx.hook("on_session_reset", session_id="never-bound", platform="tui")
    assert ctx.state.values[binding_key_for(root, "other")]["client"] == "acme-bakery"
    assert binding_key_for(root, "never-bound") not in ctx.state.values


def test_deleting_a_session_sets_its_binding_key_to_null(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="gone")
    bind(ctx, "riverbend-dental", "kept")
    turn(ctx, "gone")
    ctx.hook("on_session_delete", session_id="gone", reason="delete", profile="studio-dev")
    assert ctx.state.values[binding_key_for(root, "gone")] is None
    assert ctx.state.values[banner_key_for("gone")] is None
    assert ctx.state.values[binding_key_for(root, "kept")]["client"] == "riverbend-dental"
    assert ctx.call_tool("engagements_context", {}, session_id="gone")["error"]["details"]["mode"] == "auto"


def test_a_bulk_delete_clears_every_listed_session(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    for name in ("a", "b", "c"):
        bind(ctx, session_id=name)
    ctx.hook("on_session_delete", session_id="a", reason="prune", profile=None, deleted_ids=["a", "b", "c", ""])
    assert all(ctx.state.values[binding_key_for(root, n)] is None for n in "abc")


def test_a_delete_that_lists_only_the_removed_ids_still_prunes_them(monkeypatch, tmp_path):
    """The delete payload names the removed ids in ``deleted_ids``; the hook reads the list, not one field."""
    root, _, ctx = setup(monkeypatch, tmp_path)
    for session_id in ("removed-a", "removed-b"):
        bind(ctx, session_id=session_id)
    ctx.hook("on_session_delete", session_id=None, deleted_ids=["removed-a", "removed-b"], reason="delete")
    assert all(ctx.state.values[binding_key_for(root, name)] is None for name in ("removed-a", "removed-b"))


def test_a_reset_payload_with_no_session_id_writes_nothing(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    ctx.hook("on_session_reset", session_id=None, old_session_id="", new_session_id=None, reason="new_session")
    assert ctx.state.writes == []


def test_deleting_a_session_that_has_no_binding_writes_nothing(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    ctx.hook("on_session_delete", session_id="nobody", reason="delete", profile="default")
    assert ctx.state.writes == []


def test_a_reset_with_a_null_session_id_clears_the_session_it_names(monkeypatch, tmp_path):
    """The session reset payload Hermes records: session_id null, old_session_id set, new_session_id empty."""
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="reset-away")
    turn(ctx, "reset-away")
    ctx.hook("on_session_reset", session_id=None, old_session_id="reset-away", new_session_id="",
             platform="local", reason="new_session")
    assert ctx.state.values[binding_key_for(root, "reset-away")] is None
    assert ctx.state.values[banner_key_for("reset-away")] is None
    assert ctx.state.values["surface:reset-away"] is None


def test_a_reset_clears_every_session_id_its_payload_names(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx, session_id="before")
    bind(ctx, "riverbend-dental", "after")
    ctx.hook("on_session_reset", session_id=None, old_session_id="before", new_session_id="after",
             platform="local", reason="new_session")
    assert ctx.state.values[binding_key_for(root, "before")] is None
    assert ctx.state.values[binding_key_for(root, "after")] is None


def test_a_library_that_disappeared_retires_the_replayed_banner_once(monkeypatch, tmp_path):
    import shutil

    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    assert "acme-bakery" in turn(ctx)
    shutil.rmtree(root)
    text = turn(ctx)
    assert text.count("[Engagements]") == 1 and "No client is bound now" in text
    assert "could not be read" in text and len(text) < 400
    assert turn(ctx) is None


def test_a_reset_with_a_missing_library_still_clears_the_banner_record(monkeypatch, tmp_path):
    import shutil

    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    turn(ctx)
    shutil.rmtree(root)
    ctx.hook("on_session_reset", session_id="s1", platform="cli", reason="new")
    assert ctx.state.values[banner_key_for("s1")] is None


def test_switching_the_configured_library_retires_the_earlier_banner_once(monkeypatch, tmp_path):
    from engagements.library import open_library

    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    assert "acme-bakery" in turn(ctx)
    other = tmp_path / "second-library"
    open_library(other, create_default=True)
    ctx.settings["library_root"] = str(other)
    text = turn(ctx)
    assert "No client is bound now" in text and "mode auto" in text
    assert turn(ctx) is None


def test_the_bound_banner_returns_when_the_library_can_be_read_again(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    assert "acme-bakery" in turn(ctx)
    ctx.settings["library_root"] = str(tmp_path / "absent")
    assert "No client is bound now" in turn(ctx)
    ctx.settings["library_root"] = str(root)
    text = turn(ctx)
    assert 'Client: "Acme Bakery" (acme-bakery)' in text
    assert "changed" not in text.lower()
    assert turn(ctx) is None


def test_the_banner_uses_the_configured_labels(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path,
                      settings={"labels": {"client": "Account", "project": "Matter"}})
    bind(ctx, "acme-bakery", project="acme-website-2026")
    text = turn(ctx)
    assert text.startswith('[Engagements] Account: "Acme Bakery" (acme-bakery), Matter: "Website 2026".')
    assert "Client:" not in text and "Project:" not in text


def test_configured_labels_reach_the_caveat_of_a_bound_banner(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path, settings={"labels": {"client": "Account"}})
    bind(ctx)
    text = turn(ctx)
    assert text, "a bound turn emits the banner"
    assert text.endswith("Earlier turns may contain other Account context."), text
    assert "clients'" not in text


def test_a_settings_label_that_expands_when_escaped_keeps_the_banner_useful(monkeypatch, tmp_path):
    """Settings accept forty label characters, and escaping can double every one of them."""
    root, _, ctx = setup(monkeypatch, tmp_path, settings={"labels": {"client": "[" * 40}})
    bind(ctx)
    assert turn(ctx)
    record = root / "Acme Bakery" / "Client.md"
    record.write_text(record.read_text().replace("status: active", "status: inactive"))
    text = turn(ctx)
    assert text, "the changed record invalidates the binding, so the banner is emitted"
    assert text.count("[Engagements]") == 1, text
    assert len(text) < 400, (len(text), text)
    assert '"acme-bakery" is inactive' in text, text


def test_another_library_with_the_same_record_ids_announces_its_client_once(monkeypatch, tmp_path):
    """Two libraries may hold the same local record ids; the banner follows the library."""
    import shutil

    import yaml

    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    first = turn(ctx)
    assert first and 'Client: "Acme Bakery" (acme-bakery)' in first
    other = tmp_path / "west-library"
    shutil.copytree(root, other)
    manifest = other / ".engagements" / "library.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["library_id"] = "33333333-3333-4333-8333-333333333333"
    manifest.write_text(yaml.safe_dump(document))
    client = other / "Acme Bakery" / "Client.md"
    client.write_text(client.read_text().replace("name: Acme Bakery", "name: Acme Bakery West"))
    ctx.settings["library_root"] = str(other)
    bind(ctx)
    switched = turn(ctx)
    assert switched and '"Acme Bakery West" (acme-bakery)' in switched
    assert "changed" not in switched.lower()  # this library announced nothing before
    assert turn(ctx) is None
    ctx.settings["library_root"] = str(root)
    back = turn(ctx)
    assert back and 'Client: "Acme Bakery" (acme-bakery)' in back
    assert "changed" not in back.lower()
    assert turn(ctx) is None


def test_configured_labels_reach_the_retirement_notice(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path, settings={"labels": {"client": "Account"}})
    bind(ctx)
    first = turn(ctx)
    assert first and "Account" in first
    ctx.call_tool("engagements_bind", {"mode": "off"}, session_id="s1")
    text = turn(ctx)
    assert text and "No Account is bound now" in text and "Account context" in text
    assert "client" not in text.lower()


def test_configured_labels_reach_the_unavailable_notice(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path, settings={"labels": {"client": "Account"}})
    bind(ctx)
    first = turn(ctx)
    assert first and "Account" in first
    ctx.settings["library_root"] = str(tmp_path / "absent")
    text = turn(ctx)
    assert text and "No Account is bound now" in text and "Account context" in text
    assert "client" not in text.lower()


def test_the_unavailable_notice_names_no_action_that_needs_the_missing_library(monkeypatch, tmp_path):
    """Each record tool answers ``library_missing`` here, so naming one would advise a dead end."""
    _, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    assert turn(ctx)
    ctx.settings["library_root"] = str(tmp_path / "absent")
    text = turn(ctx)
    assert text and "No client is bound now" in text and "could not be read" in text
    update = {"id": "acme-bakery", "base_rev": "0123456789abcdef", "source": "synthetic probe",
              "ops": [{"op": "set_property", "key": "status", "value": "active"}]}
    for tool, args in (("engagements_context", {"id": "acme-bakery"}), ("engagements_update", update)):
        assert ctx.call_tool(tool, args, session_id="s1")["error"]["code"] == "library_missing"
        assert tool not in text, text
    assert "library_root" in text
    assert "does not exist" in ctx.command("engagements", "doctor").lower()


def test_reset_and_delete_never_create_a_library(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    ctx.hook("on_session_reset", session_id="x", platform="cli")
    ctx.hook("on_session_delete", session_id="x", reason="delete", profile=None)
    assert not (tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library").exists()


# --- hooks never raise into the host --------------------------------------------------------------


@pytest.mark.parametrize("hook, kwargs", [
    ("pre_llm_call", {"session_id": "s1"}),
    ("post_compaction", {"session_id": "n", "old_session_id": "s1"}),
    ("on_session_reset", {"session_id": "s1"}),
    ("on_session_delete", {"session_id": "s1", "deleted_ids": ["s1", "s2"]}),
])
def test_a_failing_state_store_never_raises_out_of_a_hook(monkeypatch, tmp_path, hook, kwargs):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    ctx.state.fail = RuntimeError("Cannot parse plugin state /secret/state.json")
    assert ctx.hook(hook, **kwargs) is None


def test_a_missing_library_directory_never_raises_out_of_a_hook(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    bind(ctx)
    import shutil

    shutil.rmtree(root)
    assert turn(ctx) is None
    for hook in ("on_session_reset", "on_session_delete"):
        assert ctx.hook(hook, session_id="s1") is None
