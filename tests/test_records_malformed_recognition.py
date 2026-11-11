"""Malformed-record recognition: CRLF, explicit tags, dates, closing hashes, non-ASCII
whitespace and tagged markers."""

from engagements.records import parse


def test_malformed_crlf_recognition():
    data = b'---\r\nengagements: client\r\nid: acme-bakery\r\nname: Acme Bakery\r\nx: [\r\n---\r\n'
    record = parse(data)
    assert record.is_record and record.kind == 'client'
    assert record.id == 'acme-bakery' and record.name == 'Acme Bakery'
    assert record.global_defects == {'yaml_parse'}


def test_bad_explicit_scalar_tag_is_targeted():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nx: !!bool nope\n---\n'
    record = parse(data)
    assert record.is_record and record.property_defects['x'] == 'unsupported_yaml'
    assert 'yaml_parse' not in record.global_defects


def test_invalid_date_construction_does_not_hide_composed_marker():
    data = b'---\n"engagements": client\nid: acme-bakery\nname: Acme Bakery\nupdated: 2020-99-99\n---\n'
    record = parse(data)
    assert record.is_record and record.kind == 'client'
    assert 'unsupported_yaml' in record.global_defects
    assert 'yaml_parse' not in record.global_defects


def test_mapping_unsupported_value_conversion_cannot_masquerade_as_sentinel():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            b'x: {unsupported_yaml: fake}\n---\n')
    assert parse(data).properties['x'] == {'unsupported_yaml': 'dict'}


def test_closing_hashes_empty_heading():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n## ###\n'
    assert parse(data).sections[0].title == ''


def test_non_ascii_whitespace_does_not_close_fence():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n'
            b'## One\n```\n```\xc2\xa0\n## Hidden\n```\n## Two\n')
    assert [s.title for s in parse(data).sections] == ['One', 'Two']


def test_tagged_marker_is_recognized_and_escalated():
    data = (b'---\nengagements: !custom client\nid: acme-bakery\n'
            b'name: Acme Bakery\n---\n')
    record = parse(data)
    assert record.is_record and record.kind == 'client'
    assert record.properties['engagements'] is None
    assert 'unsupported_yaml' in record.global_defects
