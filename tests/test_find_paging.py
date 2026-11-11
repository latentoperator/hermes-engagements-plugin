"""``engagements_find`` pages through its ordered matches with ``limit`` and ``offset``.

The order is the contract's sort key (tier, then clients before projects, then
normalized name, id and path), so consecutive pages of one query are consecutive
windows of the same list. These tests read the whole list that way and assert
that pages neither overlap nor skip, and that ``total``, ``truncated`` and
``next_offset`` describe the whole match set rather than the page.
"""

import json

import pytest
from conftest import record_bytes, write
from engagements import library as library_module
from engagements.library import open_library
from tools_support import call, make_session


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(library_module, "_SCAN_CACHE", library_module._ScanCache())


@pytest.fixture
def root(tmp_path):
    lib = open_library(tmp_path / "library", create_default=True)
    for index in range(5):
        write(lib.root, f"Client {index}/Client.md",
              record_bytes("client", f"client-{index}", f"Client {index}"))
    return lib.root


def session(root):
    return make_session(root)[0]


def find(root, **arguments):
    arguments.setdefault("query", "")
    result = call("engagements_find", arguments, session(root))
    assert result["ok"] is True, result
    return result["data"]


def test_pages_walk_the_ordered_matches_once(root):
    first = find(root, limit=2)
    assert [row["id"] for row in first["results"]] == ["client-0", "client-1"]
    assert (first["total"], first["truncated"], first["next_offset"]) == (5, True, 2)
    second = find(root, limit=2, offset=first["next_offset"])
    assert [row["id"] for row in second["results"]] == ["client-2", "client-3"]
    assert (second["total"], second["truncated"], second["next_offset"]) == (5, True, 4)
    last = find(root, limit=2, offset=second["next_offset"])
    assert [row["id"] for row in last["results"]] == ["client-4"]
    assert (last["total"], last["truncated"], last["next_offset"]) == (5, False, None)
    walked = [row["id"] for page in (first, second, last) for row in page["results"]]
    assert walked == [row["id"] for row in find(root, limit=100)["results"]]


def test_total_counts_every_match_and_not_the_page(root):
    for offset in (0, 2, 4):
        page = find(root, limit=2, offset=offset)
        assert page["total"] == 5


def test_an_offset_at_or_past_the_end_returns_an_empty_page(root):
    page = find(root, limit=2, offset=5)
    assert page["results"] == [] and page["truncated"] is False and page["next_offset"] is None
    assert page["total"] == 5


def test_two_identical_queries_order_their_matches_the_same_way(root):
    assert find(root, limit=100) == find(root, limit=100)


@pytest.mark.parametrize("offset", [-1, "1", 1.0, True, None], ids=["negative", "text", "float", "bool", "null"])
def test_an_offset_that_is_not_a_number_at_or_above_zero_is_invalid(offset):
    code = {"query": "", "offset": offset}
    result = call("engagements_find", code, session("unused"))
    assert result["ok"] is False, result
    assert result["error"]["code"] == "invalid_op"
    assert result["error"]["details"] == {"field": "offset", "reason": "invalid_value"}


def test_a_page_of_one_steps_through_the_whole_list(root):
    seen = []
    offset = 0
    while offset is not None:
        page = find(root, limit=1, offset=offset)
        seen.extend(row["id"] for row in page["results"])
        offset = page["next_offset"]
    assert seen == [f"client-{index}" for index in range(5)]


def test_the_tool_result_is_json_with_the_new_field(root):
    payload = call("engagements_find", {"query": "client-1"}, session(root))
    data = json.loads(json.dumps(payload))["data"]
    assert data["next_offset"] is None
    assert set(data) == {"results", "total", "truncated", "next_offset"}
