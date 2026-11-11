from engagements.records import footprint, parse


def test_spans_bom_crlf_fences_and_nested_headings():
    data = (b'\xef\xbb\xbf---\r\nengagements: client\r\nid: acme-bakery\r\n'
            b'name: Acme Bakery\r\naliases:\r\n  - Acme\r\n---\r\n'
            b'\r\n## Contacts\r\n```\r\n## Not a heading\r\n```\r\n'
            b'### Nested\r\nText\r\n\r\n## Notes\r\nEnd')
    record = parse(data)
    assert record.bom and record.eol == b'\r\n' and not record.final_newline
    assert [s.title for s in record.sections] == ['Contacts', 'Notes']
    assert data[slice(*record.property_spans['aliases'][0])] == b'aliases:\r\n  - Acme\r\n'
    assert footprint(data, {'op': 'replace_section', 'section': 'contacts'}) == (
        '## Contacts\n```\n## Not a heading\n```\n### Nested\nText')


def test_targeted_unsupported_and_duplicates():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme\n'
            b'aliases: [One]\naliases: [Two]\ncustom: {nested: x}\n---\n'
            b'## Summary\nfirst\n## summary\nsecond\n')
    record = parse(data)
    assert 'aliases' in record.duplicate_keys
    assert 'aliases' not in record.properties
    assert 'custom' in record.unsupported_keys
    assert [s.key for s in record.sections] == ['summary', 'summary']


def _heading_with(suffix):
    data = ("---\nengagements: client\nid: headings\nname: Headings\n---\n\n"
            f"## Notes ###{suffix}\nx\n").encode()
    return parse(data).sections[0]


def test_heading_closing_sequence_is_delimited_by_ascii_space_or_tab_only():
    """TOOLS.md "Fences and headings": only ASCII space and tab close a heading's hashes.

    CommonMark's closing sequence is preceded by a space or tab. Unicode
    whitespace is part of the title, so the hashes stay and doctor keeps warning
    about the heading.
    """
    space = _heading_with(" ")
    tab = _heading_with("\t")
    nbsp = _heading_with("\u00a0")
    em_space = _heading_with("\u2003")

    assert (space.title, space.key) == ("Notes", "notes")
    assert (tab.title, tab.key) == ("Notes", "notes")
    assert (nbsp.title, nbsp.key) == ("Notes ###", "notes ###")
    assert (em_space.title, em_space.key) == ("Notes ###", "notes ###")


def test_read_invalid_encoding_and_mixed_eol():
    bad = parse(b'---\nname: \xff\n---\n')
    assert 'encoding' in bad.defects
    mixed = parse(b'---\r\nname: Acme\n---\r\n')
    assert 'mixed_eol' in mixed.defects
