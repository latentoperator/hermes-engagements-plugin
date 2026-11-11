"""Parsing cost grows with the size of a record, not with the square of it.

Discovery parses every record in full, so a single record whose body is many
headings or whose frontmatter is many keys used to slow every find, create and
bind. These tests time the parser itself; they are written against growth so a
slow machine still passes.
"""

import time

from engagements import records

FRONTMATTER = (b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nupdated: 2026-01-01\n"
               b"---\n\n# Acme Bakery\n\n")


def record_with_headings(count):
    body = b"".join(b"## Section %d\n" % index for index in range(count))
    return FRONTMATTER + body + b"## Change log\n"


def parse_seconds(data, runs=3):
    """The best of a few parses: a single measurement can catch an unlucky pause."""
    quickest = None
    record = None
    for _ in range(runs):
        start = time.perf_counter()
        record = records.parse(data)
        elapsed = time.perf_counter() - start
        quickest = elapsed if quickest is None else min(quickest, elapsed)
    return quickest, record


def test_every_heading_is_still_a_section_and_a_repeat_is_still_a_duplicate():
    record = records.parse(FRONTMATTER + b"## Summary\n\n## Summary\n\n## Notes\n")

    assert [section.key for section in record.sections] == ["summary", "summary", "notes"]
    assert record.section_defects == {"summary": "duplicate_section"}


def test_heading_parsing_grows_with_the_record_not_with_its_square():
    # Growth rather than a wall-clock ceiling, so a slow CI runner stays green.
    # Four times the headings costs about four times the work when the section
    # keys are counted in one pass, and about sixteen times when every heading
    # recounts every other heading.
    parse_seconds(record_with_headings(100))  # warm up before the first measurement
    small, small_record = parse_seconds(record_with_headings(5000))
    large, large_record = parse_seconds(record_with_headings(20000))

    assert len(small_record.sections) == 5001
    assert len(large_record.sections) == 20001
    assert large_record.section_defects == {}
    assert small > 0
    assert large / small < 10


def test_twenty_thousand_headings_parse_within_a_generous_ceiling():
    # A second, absolute guard on the same fix: the quadratic parser needed about
    # eleven seconds for this file, and the linear one a few hundredths.
    elapsed, record = parse_seconds(record_with_headings(20000))

    assert len(record.sections) == 20001
    assert elapsed < 5.0


def test_frontmatter_keys_on_one_file_line_are_all_unsupported():
    # Two properties whose spans share a file line are both unreadable: rewriting
    # either could reach the other. The check no longer compares every key with
    # every other key.
    data = (b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n"
            b"aliases: [Acme]\xe2\x80\xa8status: paused\n---\n\n## Change log\n")
    record = records.parse(data)

    assert record.properties is not None
    assert {"aliases", "status"} <= record.unsupported_keys
    assert record.property_defects == {"aliases": "unsupported_yaml", "status": "unsupported_yaml"}
    assert record.property_spans["aliases"][0] == record.property_spans["status"][0]


def test_neighbouring_frontmatter_keys_stay_supported_and_keep_their_own_spans():
    data = (b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n"
            b"aliases: [Acme]\nstatus: paused\n---\n\n## Change log\n")
    record = records.parse(data)

    assert record.unsupported_keys == set()
    assert record.property_defects == {}
    first = record.property_spans["aliases"][0]
    second = record.property_spans["status"][0]
    assert first[1] <= second[0]


def record_with_frontmatter_keys(count):
    front = (b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n"
             + b"".join(b"k%04d:\n" % index for index in range(count)))
    return front + b"---\n\n## Summary\n"


def test_the_frontmatter_span_scan_grows_with_the_keys_not_with_their_square():
    # The span overlap check compares each property with the ones around it, not with
    # every other property, so restoring the pairwise scan is the regression this
    # guards. The frontmatter window caps the key count, so the sizes are 1,000 and
    # 9,000: linear work costs about nine times as much, the pairwise scan more than
    # twenty times. Growth, not a wall-clock ceiling, so a slow runner stays green.
    parse_seconds(record_with_frontmatter_keys(100))  # warm up before measuring
    small, small_record = parse_seconds(record_with_frontmatter_keys(1000), runs=2)
    large, large_record = parse_seconds(record_with_frontmatter_keys(9000), runs=2)

    assert len(small_record.properties) == 1003
    assert len(large_record.properties) == 9003
    assert small > 0
    assert large / small < 14
