"""Spec 6.4 steps 1 to 3: canonicalization and cwd matching (pure functions)."""

import itertools
import os
import subprocess
import sys
import warnings

import pytest
from engagements import binding
from engagements.binding import canonicalize, match_projects

HOME = "/home/ann"


def everywhere(_path):
    return True


def keep(path):
    return path


def proj(pid, client, locations, *, status="active", valid=True, name=None):
    return {
        "id": pid,
        "kind": "project",
        "client": client,
        "name": name or pid,
        "status": status,
        "locations": list(locations),
        "valid": valid,
    }


def match(cwd, projects, *, surface="cli", **kw):
    kw.setdefault("home", HOME)
    kw.setdefault("os_name", "posix")
    kw.setdefault("exists", everywhere)
    kw.setdefault("realpath", keep)
    return match_projects(cwd, projects, surface=surface, **kw)


# --- canonicalize ----------------------------------------------------------


def test_canonicalize_normalizes_posix_without_touching_the_filesystem():
    got = canonicalize("/a/b/../b/c/", home=HOME, os_name="posix", exists=lambda p: False)
    assert got == "/a/b/c"


def test_canonicalize_expands_tilde_against_the_injected_home():
    kw = {"home": HOME, "os_name": "posix", "exists": lambda p: False}
    assert canonicalize("~/clients/acme", **kw) == "/home/ann/clients/acme"
    assert canonicalize("~", **kw) == "/home/ann"
    assert canonicalize("~/", **kw) == "/home/ann"
    other = canonicalize("~/x", home="/srv/bob", os_name="posix", exists=lambda p: False)
    assert other == "/srv/bob/x"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "relative/dir",
        "./dir",
        "../dir",
        "~other/dir",
        "https://drive.example/acme-brand",
        "ftp://files.example/x",
        "file:///srv/acme",
        "mailto:jane@acme-bakery.example",
        "//host.example/share",
        "/a/b\x00c",
    ],
)
def test_canonicalize_rejects_non_local_absolute_forms(path):
    assert canonicalize(path, home=HOME, os_name="posix", exists=everywhere) is None


def test_canonicalize_needs_a_home_to_expand_tilde():
    assert canonicalize("~/x", home=None, os_name="posix", exists=everywhere) is None
    assert canonicalize("~/x", home="relative", os_name="posix", exists=everywhere) is None


def test_canonicalize_uses_realpath_only_when_the_path_exists():
    seen = []

    def real(path):
        seen.append(path)
        return "/resolved" + path

    got = canonicalize("/a/b", home=HOME, os_name="posix", exists=lambda p: True, realpath=real)
    assert got == "/resolved/a/b"
    seen.clear()
    got = canonicalize("/a/b", home=HOME, os_name="posix", exists=lambda p: False, realpath=real)
    assert got == "/a/b"
    assert seen == []


def test_canonicalize_follows_real_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    want = os.path.normcase(os.path.realpath(real))
    assert canonicalize(str(link), home=HOME, os_name=os.name) == want
    assert canonicalize(str(link) + "/", home=HOME, os_name=os.name) == want
    assert canonicalize(str(real), home=HOME, os_name=os.name) == want


def test_canonicalize_never_raises_on_filesystem_trouble():
    def boom(_path):
        raise OSError("denied")

    got = canonicalize("/a/b", home=HOME, os_name="posix", exists=boom)
    assert got == "/a/b"
    got = canonicalize("/a/b", home=HOME, os_name="posix", exists=everywhere, realpath=boom)
    assert got == "/a/b"


# --- Windows through ntpath --------------------------------------------------


def test_tilde_with_repeated_separators_keeps_the_injected_home():
    kw = {"home": HOME, "os_name": "posix", "exists": lambda p: False}
    assert canonicalize("~//work/acme", **kw) == "/home/ann/work/acme"
    assert canonicalize("~///work//acme/", **kw) == "/home/ann/work/acme"
    assert canonicalize("~//", **kw) == "/home/ann"
    root_home = canonicalize("~//work", home="/", os_name="posix", exists=lambda p: False)
    assert root_home == "/work"


def test_windows_tilde_with_repeated_or_mixed_separators_keeps_the_injected_home():
    kw = {"home": "C:\\Users\\Ann", "os_name": "nt", "exists": lambda p: False}
    for path in ("~\\\\Work", "~//Work", "~\\/Work", "~/\\Work", "~\\\\\\Work\\\\"):
        assert canonicalize(path, **kw) == "c:\\users\\ann\\work", path
    # A drive-qualified remainder must not replace the home either.
    assert canonicalize("~/D:/Work", **kw).startswith("c:\\users\\ann\\")


def test_a_tilde_location_with_doubled_separator_does_not_match_outside_home():
    projects = [proj("p", "c", ["~//work/shared"])]
    got = match("/work/shared", projects, home="/nonexistent-home")
    assert got.state == "unbound"
    assert match("/nonexistent-home/work/shared", projects, home="/nonexistent-home").state == "bound"


def test_windows_paths_compare_case_insensitively_through_ntpath():
    kw = {"home": "C:\\Users\\Ann", "os_name": "nt", "exists": lambda p: False}
    a = canonicalize("C:/Users/Ann/Work/", **kw)
    b = canonicalize("c:\\USERS\\ann\\work", **kw)
    assert a == b == "c:\\users\\ann\\work"


def test_windows_tilde_expands_against_home_with_either_separator():
    kw = {"home": "C:\\Users\\Ann", "os_name": "nt", "exists": lambda p: False}
    assert canonicalize("~\\Work", **kw) == "c:\\users\\ann\\work"
    assert canonicalize("~/Work", **kw) == "c:\\users\\ann\\work"
    assert canonicalize("~", **kw) == "c:\\users\\ann"


@pytest.mark.parametrize(
    "path",
    [
        "\\\\srv\\share\\x",
        "//srv/share/x",
        "\\\\?\\C:\\x",
        "C:relative",
        "/rooted/without/drive",
        "\\rooted",
        "http://drive.example/x",
        "work\\dir",
    ],
)
def test_windows_rejects_unc_remote_and_non_absolute_forms(path):
    got = canonicalize(path, home="C:\\Users\\Ann", os_name="nt", exists=everywhere)
    assert got is None


def test_windows_matching_is_case_insensitive_and_boundary_safe():
    kw = {"os_name": "nt", "home": "C:\\Users\\Ann"}
    projects = [proj("acme-site", "acme-bakery", ["C:\\Clients\\Acme"])]
    hit = match("c:/clients/ACME/site", projects, **kw)
    assert hit.state == "bound" and hit.project["id"] == "acme-site"
    miss = match("c:/clients/acme-two", projects, **kw)
    assert miss.state == "unbound"


# --- matching ----------------------------------------------------------------


def note_windows(what):
    """Name the case in the Windows CI log: ``pytest -q`` never prints a passing test id.

    The warnings summary does list a test that ran to the end, so on a Windows job a warned
    test that passed is the printable evidence that this case ran there.
    """
    if os.name == "nt":
        warnings.warn(f"{what}, on Windows paths", UserWarning, stacklevel=2)



def test_a_location_on_a_mapped_network_drive_does_not_match():
    # A drive letter is a local-looking name for a share: the resolved path is what
    # decides whether the folder is on this host.
    resolved = {"Z:\\Clients\\Acme": "\\\\files.example\\projects\\acme",
                "Z:\\Clients\\Acme\\site": "\\\\files.example\\projects\\acme\\site"}
    projects = [proj("acme-site", "acme-bakery", ["Z:\\Clients\\Acme"])]
    got = match("Z:\\Clients\\Acme\\site", projects, os_name="nt", home="C:\\Users\\Ann",
                realpath=lambda path: resolved.get(path, path))
    assert got.state == "unbound"
    assert got.client is None and got.project is None
    note_windows("a location on a mapped network drive does not match")


def test_a_junction_that_resolves_to_a_share_does_not_match():
    resolved = {"C:\\Work\\Acme": "\\\\files.example\\projects\\acme",
                "C:\\Work\\Acme\\site": "\\\\files.example\\projects\\acme\\site"}
    projects = [proj("acme-site", "acme-bakery", ["C:\\Work\\Acme"])]
    got = match("C:\\Work\\Acme\\site", projects, os_name="nt", home="C:\\Users\\Ann",
                realpath=lambda path: resolved.get(path, path))
    assert got.state == "unbound"
    note_windows("a junction that resolves to a share does not match")


def test_a_cwd_that_resolves_to_a_share_is_not_a_local_folder():
    resolved = {"C:\\Work\\Acme\\site": "\\\\files.example\\projects\\acme\\site"}
    projects = [proj("acme-site", "acme-bakery", ["C:\\Work\\Acme"])]
    got = match("C:\\Work\\Acme\\site", projects, os_name="nt", home="C:\\Users\\Ann",
                realpath=lambda path: resolved.get(path, path))
    assert got.state == "unbound"
    assert got.reason == "no_cwd"
    note_windows("a cwd that resolves to a share is not a local folder")


def test_a_local_alias_is_not_canonical_when_it_resolves_to_a_share():
    assert canonicalize("Z:\\Clients\\Acme", home="C:\\Users\\Ann", os_name="nt", exists=everywhere,
                        realpath=lambda path: "\\\\files.example\\projects\\acme") is None
    assert canonicalize("\\\\files.example\\projects\\acme", home="C:\\Users\\Ann", os_name="nt",
                        exists=everywhere) is None
    note_windows("a local alias is not canonical when it resolves to a share")


def test_an_alias_that_resolves_to_a_local_folder_still_matches():
    resolved = {"C:\\Work\\Acme": "C:\\Projects\\Acme", "C:\\Work\\Acme\\site": "C:\\Projects\\Acme\\site"}
    projects = [proj("acme-site", "acme-bakery", ["C:\\Work\\Acme"])]
    got = match("C:\\Work\\Acme\\site", projects, os_name="nt", home="C:\\Users\\Ann",
                realpath=lambda path: resolved.get(path, path))
    assert got.state == "bound"
    assert got.cwd == "c:\\projects\\acme\\site"
    note_windows("an alias that resolves to a local folder still matches")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_a_real_local_junction_still_canonicalizes_and_matches(tmp_path):
    real = tmp_path / "projects" / "acme"
    real.mkdir(parents=True)
    alias = tmp_path / "acme-alias"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(real)],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"this host cannot create a junction: {made.stderr.strip()}")
    want = os.path.normcase(os.path.realpath(real))
    assert canonicalize(str(alias), home=str(tmp_path), os_name="nt") == want
    projects = [proj("acme-site", "acme-bakery", [str(alias)])]
    got = match_projects(str(real / "site"), projects, surface="cli", home=str(tmp_path), os_name="nt")
    assert got.state == "bound"
    note_windows("a real local junction still canonicalizes and matches")


def test_location_contains_cwd_on_a_path_component_boundary():
    projects = [proj("p", "c", ["/a/b"])]
    assert match("/a/b/c", projects).state == "bound"
    assert match("/a/b", projects).state == "bound"
    assert match("/a/bc", projects).state == "unbound"
    assert match("/a", projects).state == "unbound"
    assert match("/a/b/", projects).state == "bound"


def test_filesystem_root_location_contains_everything():
    assert match("/any/where", [proj("p", "c", ["/"])]).state == "bound"


def test_single_match_binds_client_and_project():
    got = match("/work/acme/site", [proj("acme-site", "acme-bakery", ["/work/acme"])])
    assert got.state == "bound"
    assert got.client == "acme-bakery"
    assert got.project["id"] == "acme-site"
    assert got.cwd == "/work/acme/site"
    assert got.candidates == ()


def test_zero_matches_is_unbound_with_a_reason():
    got = match("/work/other", [proj("acme-site", "acme-bakery", ["/work/acme"])])
    assert got.state == "unbound"
    assert got.reason == "no_match"
    assert got.client is None and got.project is None


def test_nested_projects_both_match_and_are_ambiguous():
    projects = [
        proj("outer", "acme-bakery", ["/work/acme"]),
        proj("inner", "acme-bakery", ["/work/acme/site"]),
    ]
    got = match("/work/acme/site/src", projects)
    assert got.state == "ambiguous"
    assert [c["id"] for c in got.candidates] == ["inner", "outer"]
    assert got.client is None and got.project is None


def test_two_projects_of_the_same_client_sharing_a_folder_are_ambiguous():
    projects = [
        proj("acme-site", "acme-bakery", ["/work/shared"]),
        proj("acme-menu", "acme-bakery", ["/work/shared"]),
    ]
    got = match("/work/shared", projects)
    assert got.state == "ambiguous"
    assert {c["client"] for c in got.candidates} == {"acme-bakery"}
    assert len(got.candidates) == 2


def test_one_project_listing_the_folder_twice_is_not_ambiguous():
    projects = [proj("acme-site", "acme-bakery", ["/work/a", "/work/a/", "/work/a/./"])]
    assert match("/work/a/x", projects).state == "bound"


def test_the_same_project_id_supplied_twice_is_deduplicated():
    one = proj("acme-site", "acme-bakery", ["/work/a"])
    assert match("/work/a", [one, dict(one)]).state == "bound"


def test_duplicate_ids_that_disagree_never_bind_silently():
    a = proj("acme-site", "acme-bakery", ["/work/a"])
    b = proj("acme-site", "riverbend-dental", ["/work/a"])
    got = match("/work/a", [a, b])
    assert got.state == "bound"
    assert got.project["valid"] is False


def test_inactive_projects_do_not_take_part():
    projects = [
        proj("old", "acme-bakery", ["/work/a"], status="historical"),
        proj("paused", "acme-bakery", ["/work/a"], status="paused"),
        proj("live", "riverbend-dental", ["/work/a"]),
    ]
    got = match("/work/a", projects)
    assert got.state == "bound" and got.project["id"] == "live"


def _orders(*items):
    return [list(p) for p in itertools.permutations(items)]


def test_an_inactive_duplicate_never_hides_an_active_match_in_any_order():
    hist = proj("acme-website", "acme-bakery", ["/work/shared"], status="historical", valid=False)
    live = proj("acme-website", "acme-bakery", ["/work/shared"], valid=False)
    other = proj("riverbend-brand", "riverbend-dental", ["/work/shared"])
    for projects in _orders(hist, live, other):
        got = match("/work/shared", projects)
        assert got.state == "ambiguous", [p["status"] for p in projects]
        assert [c["id"] for c in got.candidates] == ["acme-website", "riverbend-brand"]


def test_duplicates_differing_only_in_status_are_invalid_in_any_order():
    hist = proj("acme-website", "acme-bakery", ["/work/a"], status="historical")
    live = proj("acme-website", "acme-bakery", ["/work/a"])
    for projects in _orders(hist, live):
        got = match("/work/a", projects)
        assert got.state == "bound"
        assert got.project["valid"] is False
        assert got.project["status"] == "active"


def test_only_active_duplicates_contribute_locations_in_any_order():
    hist = proj("acme-website", "acme-bakery", ["/work/old"], status="historical")
    live = proj("acme-website", "acme-bakery", ["/work/new"])
    for projects in _orders(hist, live):
        assert match("/work/old", projects).state == "unbound"
        assert match("/work/new", projects).state == "bound"


def test_all_inactive_duplicates_take_no_part_in_any_order():
    a = proj("acme-website", "acme-bakery", ["/work/a"], status="historical")
    b = proj("acme-website", "riverbend-dental", ["/work/a"], status="paused")
    for projects in _orders(a, b):
        assert match("/work/a", projects).state == "unbound"


def test_a_malformed_project_still_counts_so_a_shared_folder_stays_ambiguous():
    projects = [
        proj("good", "acme-bakery", ["/work/a"]),
        proj("broken", "acme-bakery", ["/work/a"], valid=False),
    ]
    assert match("/work/a", projects).state == "ambiguous"


def test_missing_local_locations_are_skipped():
    projects = [proj("p", "c", ["/gone/dir"]), proj("q", "d", ["/here"])]
    got = match("/gone/dir/x", projects, exists=lambda p: p == "/here")
    assert got.state == "unbound"


def test_urls_unc_and_relative_locations_are_skipped():
    locations = [
        "https://git.example/studio/acme-site",
        "//server.example/share",
        "relative/path",
        "~other/x",
        "file:///work/a",
    ]
    projects = [proj("p", "c", locations)]
    assert match("/work/a", projects).state == "unbound"
    assert match("/", projects).state == "unbound"


def test_unreadable_location_fields_are_ignored_not_fatal():
    odd = [
        {"id": "a", "client": "c", "locations": None},
        {"id": "b", "client": "c", "locations": "/work/a"},
        {"id": "c", "client": "c", "locations": [None, 3, "/work/a"]},
        {"id": "", "client": "c", "locations": ["/work/a"]},
        {"client": "c", "locations": ["/work/a"]},
    ]
    got = match("/work/a", odd)
    assert got.state == "bound" and got.project["id"] == "c"


def test_tilde_locations_expand_against_the_injected_home():
    projects = [proj("p", "c", ["~/clients/acme"])]
    got = match("/home/ann/clients/acme/site", projects)
    assert got.state == "bound"
    other = match("/home/ann/clients/acme/site", projects, home="/home/bob")
    assert other.state == "unbound"


def test_a_tilde_cwd_is_expanded_too():
    projects = [proj("p", "c", ["~/clients/acme"])]
    assert match("~/clients/acme/site", projects).state == "bound"


def test_cwd_display_collapses_home():
    got = match("/home/ann/clients/acme", [proj("p", "c", ["~/clients/acme"])])
    assert got.cwd_display == "~/clients/acme"
    far = match("/srv/x", [proj("p", "c", ["/srv/x"])])
    assert far.cwd_display == "/srv/x"
    sibling = match("/home/annie/x", [proj("p", "c", ["/home/annie"])])
    assert sibling.cwd_display == "/home/annie/x"


@pytest.mark.parametrize("surface", ["telegram", "discord", "gateway", "local", "acp", "", None, "CLI", "Desktop"])
def test_only_cli_tui_and_desktop_use_cwd_even_when_the_cwd_would_match(surface):
    projects = [proj("p", "c", ["/work/a"])]
    got = match("/work/a/x", projects, surface=surface)
    assert got.state == "cwd_unsupported"
    assert got.reason == "cwd_unsupported"
    assert got.client is None and got.project is None


@pytest.mark.parametrize("surface", ["cli", "tui", "desktop"])
def test_cli_tui_and_desktop_do_use_cwd(surface):
    projects = [proj("p", "c", ["/work/a"])]
    assert match("/work/a/x", projects, surface=surface).state == "bound"


@pytest.mark.parametrize("cwd", [None, "", "relative", "https://x.example/a"])
def test_missing_or_unusable_cwd_is_unbound(cwd):
    got = match(cwd, [proj("p", "c", ["/work/a"])])
    assert got.state == "unbound"
    assert got.reason == "no_cwd"


def test_symlinked_cwd_and_symlinked_location_both_canonicalize(tmp_path):
    real = tmp_path / "clients" / "acme"
    (real / "site").mkdir(parents=True)
    cwd_link = tmp_path / "cwd-link"
    cwd_link.symlink_to(real / "site", target_is_directory=True)
    loc_link = tmp_path / "loc-link"
    loc_link.symlink_to(real, target_is_directory=True)
    kw = {"home": str(tmp_path / "home"), "os_name": os.name}

    by_real_location = [proj("p", "c", [str(real)])]
    assert match_projects(str(cwd_link), by_real_location, surface="cli", **kw).state == "bound"

    by_link_location = [proj("p", "c", [str(loc_link)])]
    assert match_projects(str(real / "site"), by_link_location, surface="cli", **kw).state == "bound"
    assert match_projects(str(cwd_link), by_link_location, surface="cli", **kw).state == "bound"


def test_a_symlinked_location_that_points_outside_does_not_widen_the_match(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "jump").symlink_to(outside, target_is_directory=True)
    kw = {"home": str(tmp_path), "os_name": os.name}
    projects = [proj("p", "c", [str(inside / "jump")])]
    assert match_projects(str(inside), projects, surface="cli", **kw).state == "unbound"
    assert match_projects(str(outside), projects, surface="cli", **kw).state == "bound"


def test_real_filesystem_missing_location_is_skipped(tmp_path):
    kw = {"home": str(tmp_path), "os_name": os.name}
    projects = [proj("p", "c", [str(tmp_path / "never-created")])]
    assert match_projects(str(tmp_path / "never-created"), projects, surface="cli", **kw).state == "unbound"


def test_module_has_no_process_global_state():
    public = [n for n in vars(binding) if not n.startswith("_")]
    mutable = [n for n in public if isinstance(getattr(binding, n), (dict, list, set))]
    assert mutable == []
