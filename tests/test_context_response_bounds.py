"""One read response is bounded whatever the record holds.

``engagements_context`` and the pair ``engagements_bind`` returns are built from the
same record summaries, so both are bounded here.

The record keeps its 1 MiB limit and its 64 KiB frontmatter window, so every part of
a read is clipped: a heading at 200 characters, a property value at 2,000, a list
value at 32 items, a property key at 120 characters and the property map at 64 keys,
a related row's name and locations, the related rows at 50 and 8,000 serialized
bytes, and the diagnostics at 16 plus one count. What clipping cannot bound is added
up: the whole envelope the tool returns stays inside ``CONTEXT_RESPONSE_BYTES``, and
the body page gives up characters when the other parts need the room. Every clipped
or dropped part says so, and nothing on disk changes.
"""

import json

import pytest
from engagements import library as library_module
from engagements.library import open_library
from engagements.tools import (
    CONTEXT_PROJECTS_CAP,
    CONTEXT_RESPONSE_BYTES,
    CONTEXT_SECTIONS_CAP,
    CONTEXT_WARNINGS_CAP,
    PROPERTY_KEY_CAP,
    PROPERTY_LIST_CAP,
    PROPERTY_VALUE_CAP,
    RELATED_LOCATIONS_CAP,
    RELATED_NAME_CAP,
    SECTION_TITLE_CAP,
)
from tools_support import call, make_session

FRONTMATTER = ("---\nengagements: {kind}\nid: {id}\nname: {name}\nstatus: active\n{extra}"
               "updated: 2026-11-05\n---\n\n")


def record(kind, id_, name, *, extra="", body="## Summary\n\nFictional.\n"):
    return (FRONTMATTER.format(kind=kind, id=id_, name=name, extra=extra) + body).encode("utf-8")


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(library_module, "_SCAN_CACHE", library_module._ScanCache())


@pytest.fixture
def lib(tmp_path):
    return open_library(tmp_path / "library", create_default=True)


def write(lib, relpath, data):
    path = lib.root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def read(root, **arguments):
    result = call("engagements_context", arguments, make_session(root)[0])
    assert result["ok"] is True, result
    return result["data"], len(json.dumps(result))


def wire(result):
    """The bytes the transport writes for one result, which is what the bound covers."""
    return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def test_a_very_long_heading_is_clipped_and_marked(lib):
    """The reviewer's case: a 100,000-character heading used to arrive whole."""
    write(lib, "Huge/Client.md", record("client", "huge-heading", "Huge Heading",
                                        body="## " + "H" * 100000 + "\nbody\n"))
    data, size = read(lib.root, id="huge-heading")
    assert data["sections"][0]["title"] == "H" * 200
    assert data["sections"][0]["truncated"] is True
    assert len(data["sections"][0]["title"]) == SECTION_TITLE_CAP
    assert size < 64 * 1024, f"a {size}-byte response for one heading"


def test_a_very_long_property_value_is_clipped_and_listed(lib):
    write(lib, "Huge/Client.md", record("client", "huge-property", "Huge Property",
                                        extra="notes: " + "n" * 50000 + "\n"))
    data, size = read(lib.root, id="huge-property")
    assert data["properties"]["notes"] == "n" * PROPERTY_VALUE_CAP
    assert data["properties_truncated"] == ["notes"]
    assert size < 64 * 1024, f"a {size}-byte response for one property"


def test_every_item_of_a_long_list_property_is_clipped_and_the_count_kept(lib):
    aliases = [f'"{letter * 5000}"' for letter in "abc"]
    write(lib, "Huge/Client.md", record("client", "long-list", "Long List",
                                        extra="aliases: [" + ", ".join(aliases) + "]\n"))
    data, _size = read(lib.root, id="long-list")
    assert data["properties"]["aliases"] == ["a" * PROPERTY_VALUE_CAP, "b" * PROPERTY_VALUE_CAP,
                                             "c" * PROPERTY_VALUE_CAP]
    assert data["properties_truncated"] == ["aliases"]


def test_more_than_fifty_related_projects_are_clipped_and_counted(lib):
    write(lib, "Big/Client.md", record("client", "big-client", "Big Client"))
    for index in range(55):
        write(lib, f"Big/Projects/Project {index:02}.md",
              record("project", f"big-project-{index:02}", f"Project {index:02}", extra="client: big-client\n"))
    data, _size = read(lib.root, id="big-client")
    assert len(data["projects"]) == CONTEXT_PROJECTS_CAP
    assert data["projects_total"] == 55
    assert data["projects_truncated"] is True
    assert [row["id"] for row in data["projects"]] == [f"big-project-{index:02}"
                                                       for index in range(CONTEXT_PROJECTS_CAP)]


def test_fifty_related_projects_are_not_truncated(lib):
    write(lib, "Exact/Client.md", record("client", "exact-client", "Exact Client"))
    for index in range(CONTEXT_PROJECTS_CAP):
        write(lib, f"Exact/Projects/Project {index:02}.md",
              record("project", f"exact-project-{index:02}", f"Project {index:02}", extra="client: exact-client\n"))
    data, _size = read(lib.root, id="exact-client")
    assert len(data["projects"]) == CONTEXT_PROJECTS_CAP
    assert "projects_total" not in data and "projects_truncated" not in data


def test_a_short_record_carries_no_truncation_markers(lib):
    write(lib, "Plain/Client.md", record("client", "plain-client", "Plain Client",
                                         extra="aliases: [Plain]\n"))
    data, _size = read(lib.root, id="plain-client")
    assert "properties_truncated" not in data
    assert all("truncated" not in section for section in data["sections"])
    assert data["properties"]["aliases"] == ["Plain"]


def test_a_page_covering_thousands_of_headings_lists_four_hundred_and_counts_the_rest(lib):
    """Short headings are the cheap way to make one page hold thousands of index rows."""
    body = "".join(f"## s{index}\n" for index in range(4000))
    write(lib, "Dense/Client.md", record("client", "dense-headings", "Dense Headings", body=body))
    data, size = read(lib.root, id="dense-headings")
    assert len(data["sections"]) == CONTEXT_SECTIONS_CAP
    assert data["sections_truncated"] is True
    assert data["sections_total"] > CONTEXT_SECTIONS_CAP
    assert size < 64 * 1024, f"a {size}-byte response for one page of headings"


def test_a_page_of_four_hundred_headings_is_not_truncated(lib):
    body = "".join(f"## s{index}\n" for index in range(CONTEXT_SECTIONS_CAP))
    write(lib, "Exact/Dense.md", record("client", "exact-headings", "Exact Headings", body=body))
    data, _size = read(lib.root, id="exact-headings")
    assert len(data["sections"]) == CONTEXT_SECTIONS_CAP
    assert "sections_truncated" not in data and "sections_total" not in data


def test_the_worst_response_within_the_record_limit_stays_under_sixty_four_kibibytes(lib):
    """Long headings, a full page of them, and frontmatter full of long property values."""
    properties = "".join(f'key{index}: "{"v" * 4000}"\n' for index in range(16))
    body = "".join(f"## s{index}\n" for index in range(4000))
    write(lib, "Worst/Client.md", record("client", "worst-case", "Worst Case", extra=properties, body=body))
    data, size = read(lib.root, id="worst-case")
    assert len(data["sections"]) == CONTEXT_SECTIONS_CAP and data["sections_truncated"] is True
    assert data["properties_truncated"] and len(data["properties"]["key0"]) == PROPERTY_VALUE_CAP
    assert size < 64 * 1024, f"a {size}-byte response at the record limit"


def test_the_record_on_disk_keeps_every_character_and_the_rev_matches_it(lib):
    body = "## " + "H" * 100000 + "\nbody\n"
    path = write(lib, "Huge/Client.md", record("client", "huge-heading", "Huge Heading", body=body))
    before = path.read_bytes()
    data, _size = read(lib.root, id="huge-heading")
    assert path.read_bytes() == before
    assert data["rev"] == library_module.rev(before)
    assert len(before) > 100000


def test_a_list_of_twenty_thousand_short_items_is_cut_and_marked(lib):
    """A list is unbounded by its items' length: the count has to be bounded too."""
    write(lib, "List/Client.md", record("client", "list-items", "List Items",
                                        extra="notes: [" + ", ".join("a" for _ in range(20000)) + "]\n"))
    data, size = read(lib.root, id="list-items")
    assert len(data["properties"]["notes"]) == PROPERTY_LIST_CAP
    assert data["properties_truncated"] == ["notes"]
    assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte response for one list property"


def test_a_record_of_long_properties_keeps_only_what_the_budget_allows(lib):
    """Every value is inside the clip; their total is what has to be bounded.

    The reader is told how many keys it did not get, and a key it did get is whole.
    """
    values = "".join("k" + str(index) + ": " + "v" * 1900 + "\n" for index in range(33))
    write(lib, "Wide/Client.md", record("client", "wide-properties", "Wide Properties", extra=values,
                                        body="## Summary\n" + "x" * 11900 + "\n"))
    data, size = read(lib.root, id="wide-properties")
    assert len(data["properties"]) < 33
    assert data["properties"]["k0"] == "v" * 1900
    assert len(data["properties"]) + data["properties_omitted"] == 5 + 33  # identity keys too
    assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte response for 33 long properties"


def test_a_record_of_multibyte_properties_stays_inside_the_response_bound(lib):
    """Escaping multiplies a property, and the page has to give up room for it."""
    emoji = "\U0001f600"
    values = "".join("k" + str(index) + ": " + emoji * 1900 + "\n" for index in range(8))
    write(lib, "Wide/Client.md", record("client", "wide-multibyte", "Wide Multibyte", extra=values,
                                        body="## Summary\n" + emoji * 11900 + "\n"))
    data, size = read(lib.root, id="wide-multibyte")
    assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte response for multibyte properties"
    assert data["properties"]["k0"] == emoji * 1900
    assert data.get("properties_omitted") == 7
    assert data["next_offset"] is not None and data["next_offset"] > 0


def test_long_property_keys_are_omitted_and_counted(lib):
    """A key is repeated in the truncation marker, so its own length is bounded too."""
    extra = "".join("k" + str(index) + "Q" * 900 + ": " + "v" * 2100 + "\n" for index in range(20))
    write(lib, "Keys/Client.md", record("client", "long-keys", "Long Keys", extra=extra))
    data, size = read(lib.root, id="long-keys")
    assert data.get("properties_omitted") == 20
    assert all(len(key) <= PROPERTY_KEY_CAP for key in data["properties"])
    assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte response for twenty long keys"


def test_fifty_related_projects_with_long_text_stay_inside_the_bound(lib):
    """Fifty rows are not a byte bound: each row's name and locations are bounded too."""
    write(lib, "Big/Client.md", record("client", "big-client", "Big Client"))
    for index in range(CONTEXT_PROJECTS_CAP):
        write(lib, f"Big/Projects/Project {index:02}.md",
              record("project", f"big-project-{index:02}", "Project " + str(index) + " " + "N" * 5000,
                     extra="client: big-client\nlocations: [" + ", ".join("L" * 1900 for _ in range(25)) + "]\n"))
    data, size = read(lib.root, id="big-client")
    assert 0 < len(data["projects"]) < CONTEXT_PROJECTS_CAP
    assert data["projects_total"] == CONTEXT_PROJECTS_CAP and data["projects_truncated"] is True
    for row in data["projects"]:
        assert len(row["name"]) <= RELATED_NAME_CAP
        assert len(row["locations"]) <= RELATED_LOCATIONS_CAP
        assert row["truncated"] is True
    assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte response for fifty long rows"


def test_a_long_client_name_is_clipped_in_a_project_read(lib):
    """A project's client row is copied from another record, so its name is bounded here."""
    write(lib, "Long/Client.md", record("client", "long-client", "N" * 60000))
    write(lib, "Long/Projects/Site.md", record("project", "long-client-site", "Site",
                                               extra="client: long-client\n",
                                               body="## Summary\n" + "x" * 11900 + "\n"))
    data, size = read(lib.root, id="long-client-site")
    assert len(data["client"]["name"]) == RELATED_NAME_CAP
    assert data["client"]["truncated"] is True
    assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte response for one long client name"


def test_duplicated_long_headings_do_not_flood_the_diagnostics(lib):
    """Warnings echo a heading, so the echo and the count are both bounded."""
    body = "".join(("## " + f"{index:03}" + "H" * 1997 + "\nbody\n") * 2 for index in range(100))
    write(lib, "Noisy/Client.md", record("client", "noisy-headings", "Noisy Headings", body=body))
    result = call("engagements_context", {"id": "noisy-headings"}, make_session(lib.root)[0])
    assert result["ok"] is True, result
    warnings = result["warnings"]
    assert len(warnings) == CONTEXT_WARNINGS_CAP + 1
    assert warnings[-1]["code"] == "record_defects_omitted"
    assert "further record defects" in warnings[-1]["message"]
    assert all(len(warning["message"]) < 250 for warning in warnings)
    assert len(json.dumps(result)) <= CONTEXT_RESPONSE_BYTES


def test_a_bind_response_bounds_the_pair_and_its_related_rows(lib):
    """The bound pair is a summary of records: its names, locations and rows are bounded."""
    write(lib, "Big/Client.md", record("client", "big-client", "C" * 5000))
    for index in range(55):
        write(lib, f"Big/Projects/Project {index:02}.md",
              record("project", f"big-project-{index:02}", "P" * 3000,
                     extra="client: big-client\nlocations: [" + ", ".join("L" * 300 for _ in range(25)) + "]\n"))
    session, _store = make_session(lib.root)
    client_bound = call("engagements_bind", {"client": "big-client"}, session)
    assert client_bound["ok"] is True, client_bound
    data = client_bound["data"]
    assert len(data["client"]["name"]) == RELATED_NAME_CAP and data["client"]["truncated"] is True
    assert len(data["active_projects"]) == CONTEXT_PROJECTS_CAP
    assert data["active_projects_total"] == 55 and data["active_projects_truncated"] is True
    assert all(len(row["name"]) <= RELATED_NAME_CAP for row in data["active_projects"])
    assert len(json.dumps(client_bound)) <= CONTEXT_RESPONSE_BYTES

    project_bound = call("engagements_bind", {"project": "big-project-00"}, make_session(lib.root)[0])
    assert project_bound["ok"] is True, project_bound
    project = project_bound["data"]["project"]
    assert len(project["locations"]) == RELATED_LOCATIONS_CAP and project["truncated"] is True
    assert len(json.dumps(project_bound)) <= CONTEXT_RESPONSE_BYTES


def test_a_raw_page_is_bounded_and_still_advances(lib):
    """A record that is not valid UTF-8 comes back as text, and JSON escapes it.

    Fifteen thousand NUL code points are ninety thousand bytes however they are
    serialized, so a page carrying twelve thousand of them is over the bound before
    any section or property is added. The page has to be cut inside the text, and
    the continuation has to keep moving until the last code point has arrived.
    """
    blob = (b"---\nengagements: client\nid: acme-client\nname: Acme\nstatus: active\n---\n\n"
            b"## Summary\n" + b"\x00" * 15000 + b"\xff")
    path = write(lib, "Raw/Client.md", blob)
    session = make_session(lib.root)[0]
    arguments, pages = {"path": "Raw/Client.md"}, []
    while True:
        result = call("engagements_context", arguments, session)
        assert result["ok"] is True, result
        assert path.read_bytes() == blob
        data = result["data"]
        size = wire(result)
        assert size <= CONTEXT_RESPONSE_BYTES, f"a {size}-byte raw page at offset {data['offset']}"
        assert data["raw_text"], f"a raw page at offset {data['offset']} carried no text"
        pages.append(data)
        if data["next_offset"] is None:
            break
        assert data["next_offset"] > data["offset"]
        arguments.update(offset=data["next_offset"], rev=data["rev"])
    view = blob.decode("utf-8", errors="replace").replace("\r\n", "\n")
    assert "".join(page["raw_text"] for page in pages) == view
    assert [page["offset"] for page in pages] == sorted(page["offset"] for page in pages)


def test_every_requested_title_the_record_lacks_is_returned_when_it_fits(lib):
    """Fifty titles of two hundred characters are inside the request's own bound.

    Their escaped form is three times their written form, so measuring them as
    escapes would silently keep only half of them even though the response fits.
    """
    write(lib, "Plain/Client.md", record("client", "plain-client", "Plain Client", body="## Summary\nFictional.\n"))
    titles = [f"{index:02}" + "\U0001f600" * 198 for index in range(50)]
    result = call("engagements_context", {"id": "plain-client", "sections": titles}, make_session(lib.root)[0])
    assert result["ok"] is True, result
    data = result["data"]
    assert data["missing_sections"] == titles
    assert "missing_sections_omitted" not in data
    assert data["next_offset"] is None
    assert wire(result) <= CONTEXT_RESPONSE_BYTES, f"a {wire(result)}-byte response for fifty titles"


def test_titles_the_response_cannot_hold_are_counted_not_dropped(lib):
    """The titles a read gives up are a prefix, and the count of the rest is returned.

    Each requested title is echoed, so fifty of them can cost more than the whole
    bound once the record's own defects are quoted beside them. What does not fit is
    counted in ``missing_sections_omitted`` instead of disappearing.
    """
    body = "".join(("## " + f"{index:03}" + "\U0001f600" * 195 + "\nbody\n") * 2 for index in range(9))
    write(lib, "Noisy/Client.md", record("client", "noisy-client", "Noisy Client", body=body))
    titles = [f"{index:02}" + "\x00" * 198 for index in range(50)]
    result = call("engagements_context", {"id": "noisy-client", "sections": titles}, make_session(lib.root)[0])
    assert result["ok"] is True, result
    data = result["data"]
    assert wire(result) <= CONTEXT_RESPONSE_BYTES, f"a {wire(result)}-byte response for fifty titles"
    omitted = data.get("missing_sections_omitted", 0)
    assert omitted > 0, "a response that gives up titles has to say how many"
    assert data["missing_sections"] == titles[:len(data["missing_sections"])]
    assert len(data["missing_sections"]) + omitted == len(titles)


def test_a_response_that_gives_up_diagnostics_counts_them():
    """The base trimmer is the last place a diagnostic can disappear.

    A record's own text and its requested titles are clipped before this runs, so
    the trimmer only fires on a combination of them. Whatever it gives up there is
    folded into the count entry the list ends with, never dropped in silence.
    """
    from engagements.tools import _fit_base

    defects = [{"code": "record_invalid", "path": "Client.md",
                "message": "Record defect: duplicate_section on '" + "\U0001f600" * 120 + "'."}
               for _ in range(17)]
    # The trimmer sees only the envelope: a path this long leaves the diagnostics as
    # the one part it is allowed to give up.
    result = {"path": "d/" * 20000 + "Client.md", "missing_sections": [], "properties": {}}
    warnings = list(defects)
    _fit_base(result, warnings)
    listed = [item for item in warnings if item["code"] != "record_defects_omitted"]
    counted = sum(item["omitted"] for item in warnings if item["code"] == "record_defects_omitted")
    assert counted > 0, "a response that gives up diagnostics has to say how many"
    assert len(listed) + counted == len(defects)
    assert warnings[-1]["code"] == "record_defects_omitted"
    assert str(counted) in warnings[-1]["message"]
