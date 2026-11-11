"""/client and /engagements: text in, text out, never print()."""

import pytest
from plugin_support import project_home, registered
from tools_support import binding_key_for, copy_library


def setup(monkeypatch, tmp_path, **kwargs):
    root = copy_library(tmp_path)
    plugin, ctx = registered(monkeypatch, tmp_path, library=root, **kwargs)
    return root, plugin, ctx


def client(ctx, raw="", session_id="s1", **kwargs):
    return ctx.command("client", raw, session_id=session_id, session_key="key", platform="local", **kwargs)


# --- /client status (spec 6.3) -------------------------------------------------------------------


def test_status_for_an_unbound_session_says_so_with_evidence(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    text = client(ctx)
    assert "Mode: auto" in text and "Not bound." in text and "Evidence: auto:" in text
    assert "/client <name-or-id>" in text


def test_status_after_binding_shows_the_binding_and_its_explicit_evidence(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    client(ctx, "acme-bakery")
    text = client(ctx)
    assert "Mode: explicit" in text and "Client: Acme Bakery (acme-bakery)" in text
    assert "Evidence: explicit 14:02" in text  # the injected clock (tools_support.CLOCK_TIME)


def test_status_for_an_automatic_binding_shows_the_cwd_evidence(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"))
    text = client(ctx)
    assert "Mode: auto" in text and "Project: Recall Campaign (riverbend-recall-campaign)" in text
    assert "Evidence: auto: cwd ~/clients/riverbend/recall matched project riverbend-recall-campaign" in text


def test_status_for_an_ambiguous_folder_lists_the_candidates(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "shared"))
    text = client(ctx)
    assert "more than one project" in text and "acme-website-2026" in text and "riverbend-brand-assets" in text


def test_status_for_an_ambiguous_folder_uses_the_configured_project_label(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "shared"),
                      settings={"labels": {"project": "Matter"}})
    text = client(ctx)
    assert "more than one matter" in text and "more than one project" not in text


def test_status_off_the_cli_explains_that_cwd_is_not_used(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    _, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"), surface="telegram")
    text = client(ctx)
    assert "automatic binding from the folder is not available on this surface" in text
    assert "Bound" not in text and "riverbend-recall-campaign" not in text


def test_status_reports_an_invalid_binding(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    client(ctx, "acme-bakery")
    record = root / "Acme Bakery" / "Client.md"
    record.write_text(record.read_text(encoding="utf-8").replace("status: active", "status: former", 1),
                      encoding="utf-8")
    text = client(ctx)
    assert "no longer valid" in text and "record_inactive" in text


# --- /client <name-or-id>, off, auto, list ------------------------------------------------------------


@pytest.mark.parametrize("ref", ["acme-bakery", "Acme Bakery", "acme bakery"])
def test_client_binds_by_id_or_name(monkeypatch, tmp_path, ref):
    root, _, ctx = setup(monkeypatch, tmp_path)
    text = client(ctx, ref)
    assert text.startswith("Binding updated.") and "Client: Acme Bakery (acme-bakery)" in text
    assert ctx.state.values[binding_key_for(root, "s1")]["client"] == "acme-bakery"


def test_client_falls_back_to_a_project_name_or_id(monkeypatch, tmp_path):
    # SPEC-QUESTION: a ref that is not a client is tried as a project, which implies its client.
    _, _, ctx = setup(monkeypatch, tmp_path)
    text = client(ctx, "riverbend-recall-campaign")
    assert "Client: Riverbend Dental (riverbend-dental)" in text and "Project: Recall Campaign" in text


def test_client_that_matches_nothing_says_so_and_binds_nothing(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    text = client(ctx, "nobody-here")
    assert text.startswith("Could not change the binding:") and "No client or project matches" in text
    assert binding_key_for(root, "s1") not in ctx.state.values


def test_client_off_suppresses_auto_and_auto_returns(monkeypatch, tmp_path):
    home = project_home(monkeypatch, tmp_path)
    root, _, ctx = setup(monkeypatch, tmp_path, cwd=str(home / "clients" / "riverbend" / "recall"))
    off = client(ctx, "off")
    assert "Mode: off" in off and "Not bound." in off
    assert ctx.state.values[binding_key_for(root, "s1")] == {"mode": "off"}
    assert "Mode: off" in client(ctx)
    auto = client(ctx, "AUTO")
    assert "Mode: auto" in auto and "Project: Recall Campaign" in auto


def test_client_list_shows_active_clients_with_their_projects_and_no_session_is_needed(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    text = ctx.command("client", "list")  # no session kwargs at all
    assert "- Acme Bakery (acme-bakery)" in text and "    - Website 2026 (acme-website-2026)" in text
    assert "- Riverbend Dental (riverbend-dental)" in text and "Recall Campaign (riverbend-recall-campaign)" in text
    assert "Maple" not in text  # former client and its historical project are hidden


def test_labels_change_the_words_the_commands_use(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path, settings={"labels": {"client": "Account", "project": "Campaign"}})
    text = client(ctx, "riverbend-recall-campaign")
    assert "Account: Riverbend Dental" in text and "Campaign: Recall Campaign" in text
    assert text.startswith("Binding updated.")
    assert "Active accounts and campaigns:" in ctx.command("client", "list")


def test_bad_labels_fall_back_to_the_defaults(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path, settings={"labels": {"client": "   ", "project": "x" * 99}})
    assert "Client: Riverbend Dental" in client(ctx, "riverbend-dental")


def test_client_without_a_session_id_explains_and_changes_nothing(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    for raw in ("", "acme-bakery", "off"):
        assert "no session id" in ctx.command("client", raw).lower()
    assert ctx.state.values == {}


def test_a_command_without_a_session_recommends_only_what_works_without_one(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    text = ctx.command("client", "")
    assert "no session id" in text.lower()
    assert "engagements_bind" not in text
    assert "engagements_context" in text and "engagements_find" in text


def test_client_with_no_library_yet_points_to_the_zero_config_path(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path)
    for raw in ("", "list"):
        text = client(ctx, raw)
        assert "No library exists yet" in text and "hermes engagements init" in text
    assert not (tmp_path / "hermes-home" / "plugin-data" / "engagements" / "library").exists()


def test_client_with_an_unusable_root_says_to_configure_one(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path, settings={"library_root": "relative/dir"})
    assert "No library location is configured" in client(ctx, "list")
    assert "No library location is configured" in client(ctx, "acme-bakery")


def test_client_never_raises_and_never_shows_a_traceback(monkeypatch, tmp_path):
    root, plugin, ctx = setup(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise ZeroDivisionError("secret detail")

    monkeypatch.setattr(plugin.runtime, "call_tool", boom)
    text = client(ctx, "acme-bakery")
    assert text == "Engagements could not complete that command."


def test_state_failure_in_client_status_is_a_plain_sentence(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    ctx.state.fail = RuntimeError("Cannot parse plugin state /secret/state.json")
    text = client(ctx)
    assert "Traceback" not in text and "secret" not in text


def test_commands_never_print(monkeypatch, tmp_path, capsys):
    _, _, ctx = setup(monkeypatch, tmp_path)
    for raw in ("", "acme-bakery", "off", "auto", "list", "nope"):
        client(ctx, raw)
    ctx.command("engagements", "doctor")
    ctx.command("engagements", "")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_slash_command_takes_session_context_from_its_declared_keywords(monkeypatch, tmp_path):
    # Hermes passes only the keywords the handler declares; both the narrow and the wide call must work.
    import inspect

    _, _, ctx = setup(monkeypatch, tmp_path)
    parameters = inspect.signature(ctx.commands["client"]["handler"]).parameters
    assert "session_id" in parameters and "kwargs" in parameters
    assert "Binding updated." in ctx.commands["client"]["handler"]("acme-bakery", session_id="only-id")


# --- /engagements doctor ----------------------------------------------------------------------------


def test_engagements_doctor_returns_the_report_text(monkeypatch, tmp_path):
    _, _, ctx = setup(monkeypatch, tmp_path)
    text = ctx.command("engagements", "doctor")
    assert text.startswith("Engagements doctor: .") and "records checked" in text


def test_engagements_doctor_reports_a_problem_without_values(monkeypatch, tmp_path):
    root, _, ctx = setup(monkeypatch, tmp_path)
    record = root / "Acme Bakery" / "Client.md"
    record.write_text(record.read_text(encoding="utf-8") + "\napi_key = sk_live_0123456789abcdef0123456789\n",
                      encoding="utf-8")
    text = ctx.command("engagements", "doctor")
    assert "secret" in text.lower() and "sk_live_0123456789abcdef0123456789" not in text


def test_engagements_doctor_is_capped_for_a_chat_reply(monkeypatch, tmp_path):
    root, plugin, ctx = setup(monkeypatch, tmp_path)
    for index in range(80):
        (root / f"junk-{index}.md").write_text(f"---\nengagements: client\nid: bad id {index}\n---\n", encoding="utf-8")
    lines = ctx.command("engagements", "doctor").splitlines()
    assert len(lines) <= 60 and "hermes engagements doctor" in lines[-1]


@pytest.mark.parametrize("raw", ["", "help", "doctor now", "list"])
def test_engagements_without_a_known_subcommand_shows_usage(monkeypatch, tmp_path, raw):
    _, _, ctx = setup(monkeypatch, tmp_path)
    assert ctx.command("engagements", raw) == "Usage: /engagements doctor"


def test_engagements_doctor_on_a_missing_library_reports_it(monkeypatch, tmp_path):
    _, ctx = registered(monkeypatch, tmp_path, library=tmp_path / "absent")
    assert "library" in ctx.command("engagements", "doctor").lower()
