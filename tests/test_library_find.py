"""engagements_find matching and resolve (TOOLS.md T3)."""

import pytest
from conftest import record_bytes, write
from engagements.errors import EngagementsError


@pytest.fixture
def lib(opened_lib):
    return opened_lib


def err(excinfo):
    return excinfo.value


def result_ids(result):
    return [r["id"] for r in result.results]


def test_tools_md_success_example_against_the_fixture_library(lib):
    result = lib.find("acme")
    assert result.data() == {
        "results": [
            {"id": "acme-bakery", "kind": "client", "name": "Acme Bakery", "status": "active", "client": None,
             "path": "Acme Bakery/Client.md", "valid": True, "match": {"field": "id", "value": "acme-bakery"}},
            {"id": "acme-website-2026", "kind": "project", "name": "Website 2026", "status": "active",
             "client": "acme-bakery", "path": "Acme Bakery/Projects/Website 2026.md", "valid": True,
             "match": {"field": "id", "value": "acme-website-2026"}},
        ],
        "total": 2,
        "truncated": False,
        "next_offset": None,
    }
    assert result.warnings == []


def test_results_never_contain_record_bodies(lib, library_root):
    write(library_root, "Zed/Client.md", record_bytes("client", "zed-co", "Zed Co", extra="TOPSECRET-BODY-TEXT"))
    assert "TOPSECRET-BODY-TEXT" not in repr(lib.find("zed").data())


def test_empty_query_lists_everything_with_null_match(lib):
    result = lib.find("")
    assert result.total == 5
    assert all(r["match"] is None for r in result.results)
    # clients before projects, then normalized name
    assert result_ids(result) == ["acme-bakery", "riverbend-dental", "riverbend-brand-assets",
                                  "riverbend-recall-campaign", "acme-website-2026"]
    assert [r["kind"] for r in result.results] == ["client", "client", "project", "project", "project"]


def test_punctuation_only_query_behaves_like_the_empty_query(lib):
    assert lib.find("---").data() == lib.find("").data()


def test_match_tiers_exact_over_prefix_over_substring(lib, library_root):
    write(library_root, "A/Client.md", record_bytes("client", "bake-shop", "Bake"))        # exact name
    write(library_root, "B/Client.md", record_bytes("client", "bakery-two", "Bakery Two"))  # prefix
    write(library_root, "C/Client.md", record_bytes("client", "cake-bake-co", "Cake Bake Co"))  # substring
    result = lib.find("bake")
    order = result_ids(result)
    assert order.index("bake-shop") < order.index("bakery-two") < order.index("cake-bake-co")
    by_id = {r["id"]: r for r in result.results}
    assert by_id["bake-shop"]["match"] == {"field": "name", "value": "Bake"}  # id 'bake shop' is only a prefix


def test_match_field_prefers_id_then_name_then_alias_on_ties(lib, library_root):
    write(library_root, "A/Client.md", record_bytes("client", "dental-one", "Dental One", aliases=["Dental One"]))
    result = lib.find("dental one")
    row = next(r for r in result.results if r["id"] == "dental-one")
    assert row["match"] == {"field": "id", "value": "dental-one"}
    write(library_root, "B/Client.md", record_bytes("client", "other-slug", "Zebra Wash", aliases=["Zebra Wash"]))
    row = next(r for r in lib.find("zebra wash").results)
    assert row["match"] == {"field": "name", "value": "Zebra Wash"}
    write(library_root, "C/Client.md", record_bytes("client", "plain-slug", "Plain Name", aliases=["Nickname", "Handle"]))
    row = next(r for r in lib.find("handle").results)
    assert row["match"] == {"field": "alias", "value": "Handle"}


def test_alias_is_reported_as_stored(lib, library_root):
    write(library_root, "A/Client.md", record_bytes("client", "zed-co", "Zed Co", aliases=["ZED Ltd."]))
    row = lib.find("zed ltd").results[0]
    assert row["match"] == {"field": "alias", "value": "ZED Ltd."}


def test_query_is_normalized_like_names(lib):
    for query in ("ACME", "  Acme   Bakery ", "acme-bakery", "ACME_bakery!"):
        assert "acme-bakery" in result_ids(lib.find(query)), query


def test_kind_filter(lib):
    assert {r["kind"] for r in lib.find("", kind="project").results} == {"project"}
    assert {r["kind"] for r in lib.find("", kind="client").results} == {"client"}


def test_inactive_records_are_hidden_unless_requested(lib, library_root):
    write(library_root, "Old/Client.md", record_bytes("client", "old-co", "Old Co", status="former"))
    write(library_root, "Old/Projects/Hist.md", record_bytes("project", "old-project", "Hist", client="old-co", status="historical"))
    write(library_root, "Old/Projects/Pause.md", record_bytes("client", "paused-co", "Paused Co", status="paused"))
    assert not {"old-co", "old-project", "paused-co"} & set(result_ids(lib.find("")))
    shown = {r["id"]: r for r in lib.find("", include_inactive=True).results}
    assert {"old-co", "old-project", "paused-co"} <= set(shown)
    assert shown["old-co"]["status"] == "former"


def test_limit_total_and_truncated(lib):
    result = lib.find("", limit=2)
    assert len(result.results) == 2 and result.total == 5 and result.truncated is True
    assert lib.find("", limit=5).truncated is False


def test_ordering_ties_are_deterministic(lib, library_root):
    # Same tier, same kind, same normalized name: id decides, then path.
    write(library_root, "A1/Client.md", record_bytes("client", "twin-b", "Twin"))
    write(library_root, "A2/Client.md", record_bytes("client", "twin-a", "Twin"))
    write(library_root, "A3/Client.md", record_bytes("client", "twin-c", "twin"))
    runs = [result_ids(lib.find("twin")) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2] == ["twin-a", "twin-b", "twin-c"]


def test_same_id_same_name_ties_break_by_path(lib, library_root):
    write(library_root, "Z/Client.md", record_bytes("client", "dup-co", "Dup"))
    write(library_root, "A/Client.md", record_bytes("client", "dup-co", "Dup"))
    paths = [r["path"] for r in lib.find("dup").results]
    assert paths == ["A/Client.md", "Z/Client.md"]


def test_malformed_record_with_identity_is_listed_as_invalid(lib, library_root):
    write(library_root, "Broken/Client.md", b"---\nengagements: client\nid: broken-co\nname: Broken Co\naliases: [Oops\n---\n")
    result = lib.find("broken")
    row = result.results[0]
    assert row["id"] == "broken-co" and row["valid"] is False and row["kind"] == "client"
    assert result.malformed_skipped == 0


def test_malformed_records_without_identity_are_counted_not_listed(lib, library_root):
    write(library_root, "B1/Client.md", b"---\nengagements: client\naliases: [Oops\n---\n")
    write(library_root, "B2/Client.md", b"---\nengagements: client\nid: only-id\naliases: [Oops\n---\n")
    write(library_root, "B3/Client.md", b"---\nengagements: client\nid: x\nname: C\nengagements: project\n---\n")
    result = lib.find("")
    assert result.malformed_skipped == 3
    assert result.total == 5
    warning = next(w for w in result.warnings if w["code"] == "malformed_skipped")
    assert "3" in warning["message"]


def test_duplicate_id_records_are_both_listed_invalid(lib, library_root):
    write(library_root, "Copy/Client.md", record_bytes("client", "acme-bakery", "Acme Copy"))
    rows = [r for r in lib.find("acme").results if r["id"] == "acme-bakery"]
    assert len(rows) == 2 and all(not r["valid"] for r in rows)


def test_an_unreadable_status_is_not_hidden(lib, library_root):
    # SPEC-QUESTION: TOOLS.md defines 'active' only for parsed status values. A record whose YAML
    # does not parse has no readable status; the conservative reading shows it rather than hiding it.
    write(library_root, "Broken/Client.md", b"---\nengagements: client\nid: broken-co\nname: Broken Co\naliases: [Oops\n---\n")
    assert "broken-co" in result_ids(lib.find(""))


def test_find_does_not_return_paths_with_backslashes_or_absolute_roots(lib):
    for row in lib.find("").results:
        assert "\\" not in row["path"] and not row["path"].startswith("/")


@pytest.mark.parametrize("kwargs", [
    {"query": 5}, {"query": "x" * 201}, {"query": "a", "kind": "vendor"}, {"query": "a", "limit": 0},
    {"query": "a", "limit": 101}, {"query": "a", "limit": True}, {"query": "a", "limit": "5"},
    {"query": "a", "include_inactive": "yes"},
])
def test_find_argument_validation(lib, kwargs):
    with pytest.raises(EngagementsError) as e:
        lib.find(**kwargs)
    assert e.value.code == "invalid_op" and "field" in e.value.details


def test_library_created_warning_is_included_on_first_use(tmp_path):
    from engagements.library import open_library

    lib = open_library(tmp_path / "fresh", create_default=True)
    assert [w["code"] for w in lib.find("").warnings] == ["library_created"]


# --- resolve -----------------------------------------------------------------


def test_resolve_by_id(lib):
    assert lib.resolve("acme-bakery").relpath == "Acme Bakery/Client.md"


def test_resolve_by_exact_normalized_name_and_alias(lib, library_root):
    write(library_root, "Zed/Client.md", record_bytes("client", "zed-co", "Zed Co", aliases=["The Z Shop"]))
    assert lib.resolve("ZED  co").id == "zed-co"
    assert lib.resolve("the z shop").id == "zed-co"


def test_resolve_does_not_substring_match(lib):
    with pytest.raises(EngagementsError) as e:
        lib.resolve("acme")
    assert e.value.code == "not_found" and e.value.details["reason"] == "no_match"


def test_resolve_exact_id_wins_over_a_name_collision(lib, library_root):
    write(library_root, "Sneaky/Client.md", record_bytes("client", "sneaky-co", "acme-bakery"))
    assert lib.resolve("acme-bakery").id == "acme-bakery"


def test_resolve_ambiguous_when_an_alias_is_shared(lib, library_root):
    write(library_root, "A/Client.md", record_bytes("client", "a-co", "A Co", aliases=["Shared"]))
    write(library_root, "B/Client.md", record_bytes("client", "b-co", "B Co", aliases=["shared"]))
    with pytest.raises(EngagementsError) as e:
        lib.resolve("Shared")
    assert e.value.code == "ambiguous"
    assert e.value.details["candidates"] == [
        {"id": "a-co", "kind": "client", "name": "A Co", "client": None, "path": "A/Client.md"},
        {"id": "b-co", "kind": "client", "name": "B Co", "client": None, "path": "B/Client.md"},
    ]


def test_resolve_ambiguous_for_a_duplicated_id_with_paths(lib, library_root):
    write(library_root, "Copy/Client.md", record_bytes("client", "acme-bakery", "Acme Copy"))
    with pytest.raises(EngagementsError) as e:
        lib.resolve("acme-bakery")
    assert e.value.code == "ambiguous"
    assert [c["path"] for c in e.value.details["candidates"]] == ["Acme Bakery/Client.md", "Copy/Client.md"]


def test_resolve_kind_filter(lib):
    assert lib.resolve("Website 2026", kind="project").id == "acme-website-2026"
    with pytest.raises(EngagementsError) as e:
        lib.resolve("Website 2026", kind="client")
    assert e.value.code == "not_found"
    with pytest.raises(EngagementsError) as e:
        lib.resolve("acme-bakery", kind="project")
    assert e.value.code == "not_found"


def test_resolve_project_name_within_a_given_client(lib, library_root):
    write(library_root, "Riverbend Dental/Projects/Website 2026.md",
          record_bytes("project", "riverbend-website-2026", "Website 2026", client="riverbend-dental"))
    with pytest.raises(EngagementsError) as e:
        lib.resolve("Website 2026", kind="project")
    assert e.value.code == "ambiguous"
    assert lib.resolve("Website 2026", kind="project", client="acme-bakery").id == "acme-website-2026"
    assert lib.resolve("Website 2026", kind="project", client="riverbend-dental").id == "riverbend-website-2026"


def test_resolve_includes_inactive_records(lib, library_root):
    write(library_root, "Old/Client.md", record_bytes("client", "old-co", "Old Co", status="former"))
    assert lib.resolve("old-co").status == "former"


def test_resolve_never_returns_a_record_with_null_kind(lib, library_root):
    write(library_root, "C/Client.md", b"---\nengagements: client\nid: conflict-one\nname: C\nengagements: project\n---\n")
    with pytest.raises(EngagementsError) as e:
        lib.resolve("conflict-one")
    assert e.value.code == "not_found"


@pytest.mark.parametrize("ref", ["", "   ", 5, "x" * 201, "a\nb"])
def test_resolve_argument_validation(lib, ref):
    with pytest.raises(EngagementsError) as e:
        lib.resolve(ref)
    assert e.value.code == "invalid_op"
