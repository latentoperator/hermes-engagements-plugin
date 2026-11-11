"""engagements_context pagination: 12,000-code-point pages over the section view."""

import json
from pathlib import Path

from engagements.fsutil import section_key
from engagements.records import parse
from engagements.tools import CONTEXT_RESPONSE_BYTES, CONTEXT_SECTIONS_CAP
from tools_support import call, copy_library, make_session, rev_of

ACME = "Acme Bakery/Client.md"
PAGE = 12000
NOTES_LINES = 1500


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _record(identifier, name, body):
    return ("---\nengagements: client\n"
            f"id: {identifier}\nname: {name}\nstatus: active\nupdated: 2026-11-05\n---\n\n"
            f"# {name}\n\n{body}")


def big_client(root):
    """The Acme client with a Notes section far larger than one page."""
    path = Path(root, ACME)
    notes = "\n".join(f"- note {index:04d}: caf\u00e9 na\u00efve r\u00e9sum\u00e9 \u2014 \u00fcn\u00efcode"
                      for index in range(1, NOTES_LINES + 1))
    edit(path, "## Change log", f"## Notes\n{notes}\n\n## Change log")
    return path


def build_view(path, titles):
    """The documented view: each selected span in file order, CRLF to LF, one final LF."""
    data = path.read_bytes()
    record = parse(data)
    wanted = {section_key(title) for title in titles}
    parts, spans, position = [], [], 0
    for section in record.sections:
        if section.key not in wanted:
            continue
        raw = data[section.start:section.end].decode("utf-8").replace("\r\n", "\n")
        heading = data[section.start:section.body_start].decode("utf-8").replace("\r\n", "\n")
        parts.append(raw)
        spans.append({"title": section.title, "start": position, "body_start": position + len(heading),
                      "end": position + len(raw)})
        position += len(raw)
    view = "".join(parts)
    if view and not view.endswith("\n"):
        view += "\n"
    return view, spans


def paginate(session, args):
    pages, offset, rev = [], 0, None
    for _ in range(40):
        request = dict(args, offset=offset)
        if offset:
            request["rev"] = rev
        result = call("engagements_context", request, session)
        assert result["ok"], result
        data = result["data"]
        rev = rev or data["rev"]
        pages.append(data)
        if data["next_offset"] is None:
            return pages
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    raise AssertionError("pagination did not reach the end of the view")


def assert_snapshot(root, path, rev):
    snapshot = Path(root) / ".engagements" / "snapshots" / f"{rev}.md"
    assert snapshot.read_bytes() == path.read_bytes()


def test_pages_cover_the_view(tmp_path):
    root = copy_library(tmp_path)
    path = big_client(root)
    session, _ = make_session(root)
    view, spans = build_view(path, ["Summary", "Notes"])
    assert len(view) > PAGE
    assert len(view.encode("utf-8")) > len(view)  # code points, not bytes

    pages = paginate(session, {"id": "acme-bakery", "sections": ["Summary", "Notes"]})
    assert len(pages) >= 2
    assert pages[0]["offset"] == 0
    assert pages[-1]["next_offset"] is None
    assert pages[-1]["offset"] < len(view)

    seen = {}
    for index, data in enumerate(pages):
        offset = data["offset"]
        end = len(view) if data["next_offset"] is None else data["next_offset"]
        assert data["id"] == "acme-bakery" and data["kind"] == "client"
        assert data["view_length"] == len(view)
        assert 0 <= end - offset <= PAGE
        if data["next_offset"] is not None:
            assert view[offset:end].endswith("\n")
        if index + 1 < len(pages):
            assert data["next_offset"] == pages[index + 1]["offset"]

        matching = [span for span in spans if span["start"] < end and offset < span["end"]]
        assert [entry["title"] for entry in data["sections"]] == [span["title"] for span in matching]
        for entry, span in zip(data["sections"], matching, strict=True):
            assert entry["text"] == view[max(offset, span["body_start"]):min(end, span["end"])]
            assert entry["partial"] == (not (offset <= span["start"] and span["end"] <= end))
            seen.setdefault(span["title"], []).append(entry["text"])
        assert_snapshot(root, path, data["rev"])

    for span in spans:
        assert "".join(seen[span["title"]]) == view[span["body_start"]:span["end"]]
    summary = next(entry for entry in pages[0]["sections"] if entry["title"] == "Summary")
    assert summary["partial"] is False
    notes = [entry["partial"] for page in pages for entry in page["sections"] if entry["title"] == "Notes"]
    assert notes and all(notes)


def test_pages_after_the_first_require_the_rev(tmp_path):
    root = copy_library(tmp_path)
    big_client(root)
    session, _ = make_session(root)
    first = call("engagements_context", {"id": "acme-bakery", "sections": ["Notes"]}, session)
    assert first["ok"], first
    offset = first["data"]["next_offset"]
    assert offset
    without = call("engagements_context", {"id": "acme-bakery", "sections": ["Notes"], "offset": offset},
                   session)
    assert without["error"]["code"] == "invalid_op"
    assert without["error"]["details"] == {"field": "rev", "reason": "rev_required"}
    with_rev = call("engagements_context",
                    {"id": "acme-bakery", "sections": ["Notes"], "offset": offset,
                     "rev": first["data"]["rev"]}, session)
    assert with_rev["ok"], with_rev
    assert with_rev["data"]["offset"] == offset


def test_a_change_mid_pagination_is_a_rev_conflict(tmp_path):
    root = copy_library(tmp_path)
    path = big_client(root)
    session, _ = make_session(root)
    first = call("engagements_context", {"id": "acme-bakery", "sections": ["Notes"]}, session)
    assert first["ok"], first
    offset = first["data"]["next_offset"]
    assert offset
    edit(path, "\n\n## Change log", "\n- note 9999: changed mid-read\n\n## Change log")
    second = call("engagements_context",
                  {"id": "acme-bakery", "sections": ["Notes"], "offset": offset,
                   "rev": first["data"]["rev"]}, session)
    assert second["ok"] is False
    assert second["error"]["code"] == "conflict"
    assert second["error"]["details"]["reason"] == "rev_changed"
    assert second["error"]["details"]["current_rev"] == rev_of(path)
    assert second["error"]["details"]["footprints"] == []


def test_a_single_line_longer_than_a_page_is_cut_at_12000(tmp_path):
    root = copy_library(tmp_path)
    path = root / "Long Client" / "Client.md"
    path.parent.mkdir()
    line = "x" * 15000
    path.write_text(_record("long-client", "Long Client",
                            f"## Long\n{line}\n\n## Change log\n- 2026-11-05 owner: created\n"),
                    encoding="utf-8")
    session, _ = make_session(root)

    first = call("engagements_context", {"id": "long-client", "sections": ["Long"]}, session)
    assert first["ok"], first
    assert first["data"]["view_length"] == len("## Long\n") + len(line) + 2
    assert first["data"]["next_offset"] == len("## Long\n")
    assert first["data"]["sections"][0]["text"] == ""
    assert first["data"]["sections"][0]["partial"] is True

    second = call("engagements_context",
                  {"id": "long-client", "sections": ["Long"], "offset": first["data"]["next_offset"],
                   "rev": first["data"]["rev"]}, session)
    assert second["ok"], second
    assert second["data"]["next_offset"] == len("## Long\n") + PAGE
    assert second["data"]["sections"][0]["text"] == line[:PAGE]
    assert second["data"]["sections"][0]["partial"] is True

    last = call("engagements_context",
                {"id": "long-client", "sections": ["Long"], "offset": second["data"]["next_offset"],
                 "rev": first["data"]["rev"]}, session)
    assert last["ok"], last
    assert last["data"]["next_offset"] is None
    assert last["data"]["sections"][0]["text"] == line[PAGE:] + "\n\n"


def test_crlf_records_are_returned_with_lf(tmp_path):
    root = copy_library(tmp_path)
    path = root / "Crlf Client" / "Client.md"
    path.parent.mkdir()
    text = _record("crlf-client", "Crlf Client",
                   "## Summary\nLine one\nLine two\n\n## Change log\n- 2026-11-05 owner: created\n")
    path.write_bytes(text.replace("\n", "\r\n").encode())
    session, _ = make_session(root)
    result = call("engagements_context", {"id": "crlf-client", "sections": ["Summary"]}, session)
    assert result["ok"], result
    assert result["data"]["sections"][0]["text"] == "Line one\nLine two\n\n"
    assert "\r" not in result["data"]["sections"][0]["text"]
    assert_snapshot(root, path, result["data"]["rev"])


def test_the_sections_filter_limits_the_view(tmp_path):
    root = copy_library(tmp_path)
    path = Path(root, ACME)
    session, _ = make_session(root)
    summary_view, _ = build_view(path, ["Summary"])

    one = call("engagements_context", {"id": "acme-bakery", "sections": ["Summary"]}, session)
    assert one["ok"], one
    assert one["data"]["view_length"] == len(summary_view)
    assert [entry["title"] for entry in one["data"]["sections"]] == ["Summary"]
    assert one["data"]["missing_sections"] == []

    filtered = call("engagements_context", {"id": "acme-bakery", "sections": ["Summary", "No Such Section"]},
                    session)
    assert [entry["title"] for entry in filtered["data"]["sections"]] == ["Summary"]
    assert filtered["data"]["missing_sections"] == ["No Such Section"]

    repeated = call("engagements_context", {"id": "acme-bakery", "sections": ["Summary", "summary"]}, session)
    assert [entry["title"] for entry in repeated["data"]["sections"]] == ["Summary"]
    assert repeated["data"]["missing_sections"] == []


def test_an_empty_view_at_offset_zero_returns_nothing(tmp_path):
    root = copy_library(tmp_path)
    session, _ = make_session(root)
    result = call("engagements_context", {"id": "acme-bakery", "sections": []}, session)
    assert result["ok"], result
    data = result["data"]
    assert data["sections"] == []
    assert data["view_length"] == 0
    assert data["next_offset"] is None
    assert data["offset"] == 0
    assert data["missing_sections"] == []


def test_offset_out_of_range(tmp_path):
    root = copy_library(tmp_path)
    path = Path(root, ACME)
    session, _ = make_session(root)
    view, _ = build_view(path, ["Summary"])
    rev = rev_of(path)
    for offset in (len(view), len(view) + 1):
        result = call("engagements_context",
                      {"id": "acme-bakery", "sections": ["Summary"], "offset": offset, "rev": rev}, session)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_op"
        assert result["error"]["details"] == {"field": "offset", "reason": "offset_out_of_range"}


def test_duplicate_headings_are_returned_in_file_order_with_a_warning(tmp_path):
    root = copy_library(tmp_path)
    path = Path(root, ACME)
    edit(path, "## Communication preferences",
         "## Contacts\n- Sam Ortiz | manager | sam@acme-bakery.example\n\n## Communication preferences")
    session, _ = make_session(root)
    result = call("engagements_context", {"id": "acme-bakery", "sections": ["Contacts"]}, session)
    assert result["ok"], result
    entries = result["data"]["sections"]
    assert [entry["title"] for entry in entries] == ["Contacts", "Contacts"]
    assert "Jane Rivera" in entries[0]["text"]
    assert "Sam Ortiz" in entries[1]["text"]
    assert result["data"]["valid"] is False
    codes = [warning["code"] for warning in result["warnings"]]
    assert codes.count("duplicate_section") == 1
    warning = next(warning for warning in result["warnings"] if warning["code"] == "duplicate_section")
    assert warning["path"] == ACME


def test_every_read_snapshots_the_full_bytes(tmp_path):
    root = copy_library(tmp_path)
    path = Path(root, ACME)
    session, _ = make_session(root)
    for args in ({"id": "acme-bakery"}, {"id": "acme-bakery", "sections": ["Summary"]},
                 {"id": "acme-bakery", "sections": []}):
        result = call("engagements_context", args, session)
        assert result["ok"], result
        assert result["data"]["rev"] == rev_of(path)
        assert_snapshot(root, path, result["data"]["rev"])


def write_dense_client(root, sections=600, body_line=lambda index: f"body{index:04}\n"):
    """A record whose whole view fits one page but whose section index is long."""
    path = Path(root, "Dense/Client.md")
    path.parent.mkdir()
    parts = ["---\nengagements: client\nid: dense-client\nname: Dense Client\nstatus: active\n"
             "updated: 2026-11-05\n---\n\n"]
    for index in range(sections):
        parts.append(f"## s{index:04}\n{body_line(index)}")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def test_following_next_offset_returns_every_section_body_once(tmp_path):
    """Six hundred sections fit one view; the index cap must not end the read.

    The page ends at the first section it did not list and ``next_offset`` starts
    the next page there, so a reader that follows the continuation reads every body
    exactly once instead of being told the record ended.
    """
    root = copy_library(tmp_path)
    write_dense_client(root)
    session, _ = make_session(root)
    pages = paginate(session, {"id": "dense-client"})
    texts = [entry["text"] for page in pages for entry in page["sections"]]
    assert texts == [f"body{index:04}\n" for index in range(600)]
    first = pages[0]
    assert len(first["sections"]) == CONTEXT_SECTIONS_CAP
    assert first["sections_truncated"] is True and first["sections_total"] == 600
    assert first["next_offset"] is not None and first["next_offset"] > 0
    assert pages[-1]["next_offset"] is None


def test_a_page_cut_by_the_response_bound_continues_at_a_section_boundary(tmp_path):
    """Multibyte bodies make the page too big for the bound; whole sections are given up.

    The guard stops at a section boundary, so the continuation returns whole bodies
    and every one of them arrives once.
    """
    root = copy_library(tmp_path)
    write_dense_client(root, sections=120, body_line=lambda index: "\U0001f600" * 200 + "\n")
    session, _ = make_session(root)
    first = call("engagements_context", {"id": "dense-client"}, session)
    assert first["ok"], first
    assert len(json.dumps(first)) <= CONTEXT_RESPONSE_BYTES
    assert first["data"]["next_offset"] is not None
    assert len(first["data"]["sections"]) < 120
    pages = paginate(session, {"id": "dense-client"})
    texts = [entry["text"] for page in pages for entry in page["sections"]]
    assert texts == ["\U0001f600" * 200 + "\n"] * 120
    assert all(entry["partial"] is False for page in pages for entry in page["sections"])
